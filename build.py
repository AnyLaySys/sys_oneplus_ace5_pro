#!/usr/bin/env python3
import argparse
import datetime
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tomllib
import urllib.request
import uuid
import zlib


ROOT = Path(__file__).resolve().parent
BUILD = ROOT / 'build'
SOURCE = ROOT / 'src'
OUTPUT = BUILD / 'out'
ROOTFS_CACHE = BUILD / 'ubuntu-20261011130817'
sys.dont_write_bytecode = True

ROOTFS_FILES = {
    'etc/gdm3/custom.conf': '[daemon]\nInitialSetupEnable=true\n',
    'etc/systemd/journald.conf.d/ace5pro.conf': '[Journal]\nStorage=persistent\nSystemMaxUse=128M\n',
    'etc/systemd/system.conf.d/ace5pro.conf': '[Manager]\nRuntimeWatchdogSec=0\nRebootWatchdogSec=0\n',
    'usr/lib/ace5pro/audio-ucm.batch': 'set _verb HiFi\nset _enadev Speaker\n',
    'usr/lib/ace5pro/preflight.sh': 'set -eu\ntrap \'result=$?; if [ "$result" -ne 0 ]; then printf "FAIL exit=%s\\n" "$result" > /run/userspace.status; fi\' EXIT\n. /etc/os-release\ntest "$ID" = ubuntu\ntest "$VERSION_ID" = 26.10\ntest "$(dpkg --print-architecture)" = arm64\ntest "$(uname -m)" = aarch64\ntest "$(uname -r)" = "$(cat /etc/ace5pro-kernel-release)"\ntest -x /usr/lib/systemd/systemd\ntest "$(cat /sys/module/ufshcd_core/parameters/use_mcq_mode)" = N\ntest "$(findmnt -n -o FSTYPE /)" = ext4\ncase ",$(findmnt -n -o OPTIONS /)," in *,rw,*) ;; *) exit 1 ;; esac\ngrep -qx \'WRITES=PERSISTENT\' /run/root-mode.txt\nstate=/var/lib/ace5pro\nmkdir -p "$state"\nprevious=none\nif test -f "$state/last-boot"; then previous=$(cat "$state/last-boot"); fi\ncurrent=$(cat /proc/sys/kernel/random/boot_id)\nprintf \'%s\\n\' "$current" > "$state/last-boot.new"\nsync -f "$state/last-boot.new"\ntest "$(cat "$state/last-boot.new")" = "$current"\nmv "$state/last-boot.new" "$state/last-boot"\nsync -f "$state"\nprintf \'PREVIOUS_BOOT=%s\\nCURRENT_BOOT=%s\\n\' "$previous" "$current" > /run/boot-persistence.txt\n{\n    printf \'UBUNTU_ID=%s\\nVERSION_ID=%s\\nCODENAME=%s\\n\' "$ID" "$VERSION_ID" "$VERSION_CODENAME"\n    printf \'ARCH=%s\\nKERNEL=%s\\n\' "$(dpkg --print-architecture)" "$(uname -r)"\n    printf \'UFS_WRITE_SYNC_READ=PASS\\nROOT_MODE=EXT4_RW\\n\'\n} > /run/ubuntu-info.txt\nprintf \'PASS\\n\' > /run/userspace.status\n',
}

WIFI_FIRMWARE = {
    'amss.bin': 'amss20.bin',
    'm3.bin': 'phy_ucode20.elf',
    'aux_ucode.bin': 'aux_ucode20.elf',
    'board.bin': 'bdwlan.elf',
    'regdb.bin': 'regdb.bin',
    'tmel.bin': 'tmel_peach_20.elf',
    'qdss_trace_config.bin': 'qdss_trace_config_v2.cfg',
}


def run(*command, **kwargs):
    return subprocess.run([str(value) for value in command], check=True, **kwargs)


def write(root, name, text, mode=0o644):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.parent.resolve().is_relative_to(root):
        raise ValueError(path)
    if path.is_symlink():
        path.unlink()
    path.write_text(text)
    path.chmod(mode)


def stage_wifi_firmware(root):
    source = SOURCE / 'wifi-wcn-reference' / 'firmware'
    target = root / 'lib/firmware/ath12k/PEACH/hw2.0'
    target.mkdir(parents=True, exist_ok=True)
    for name, source_name in WIFI_FIRMWARE.items():
        path = source / source_name
        if not path.is_file():
            raise FileNotFoundError(path)
        shutil.copy2(path, target / name)


def stage_adsp_firmware(root):
    source = SOURCE / 'qcom-adsp-firmware'
    target = root / 'lib/firmware'
    files = sorted(path for path in source.glob('adsp*') if path.is_file())
    if not files:
        raise FileNotFoundError(source)
    target.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.copy2(path, target / path.name)


def stage_gpu_firmware(root):
    shutil.copytree(SOURCE / 'qcom-gpu-firmware/qcom', root / 'lib/firmware/qcom',
                    dirs_exist_ok=True)


def stage_audio_topology(root):
    source = SOURCE / 'qcom-sm8750-audio'
    target = root / 'lib/firmware/qcom/sm8750'
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / 'SM8750-MTP-tplg.bin', target / 'OnePlus Ace 5 Pro-tplg.bin')
    firmware = root / 'lib/firmware'
    shutil.copy2(source / 'sipa.bin', firmware / 'sipa.bin')
    base = root / 'usr/share/alsa/ucm2/Qualcomm/sm8750/MTP'
    if not (base / 'SM8750-MTP.conf').is_file():
        raise FileNotFoundError('SM8750 MTP UCM profile is missing')
    profile = root / 'usr/share/alsa/ucm2/Qualcomm/sm8750/OnePlus-Ace5Pro'
    card = (base / 'SM8750-MTP.conf').read_text()
    card = card.replace('/Qualcomm/sm8750/MTP/HiFi.conf',
                        '/Qualcomm/sm8750/OnePlus-Ace5Pro/HiFi.conf')
    card = re.sub(r'(?ms)^BootSequence \[.*?^\]\n', '', card)
    hifi = (base / 'HiFi.conf').read_text()
    hifi = hifi.replace('WSA_CODEC_DMA_RX_0 Audio Mixer MultiMedia2',
                        'SEC_MI2S_RX Audio Mixer MultiMedia2')
    hifi = re.sub(r'(?ms)^SectionDevice\."Mic" \{.*?^\}', '''SectionDevice."Mic" {
	Comment "Internal microphones"

	EnableSequence [
		cset "name='ADC1 MUX' CH1_AMIC1"
		cset "name='ADC1 Switch' 1"
		cset "name='ADC1_MIXER Switch' 1"
		cset "name='ADC1 Volume' 6"
		cset "name='ADC2 MUX' CH2_AMIC3"
		cset "name='ADC2 Switch' 1"
		cset "name='ADC2_MIXER Switch' 1"
		cset "name='ADC2 Volume' 6"
		cset "name='ADC4 MUX' CH4_AMIC5"
		cset "name='ADC4 Switch' 1"
		cset "name='ADC4_MIXER Switch' 1"
		cset "name='ADC4 Volume' 6"
		cset "name='TX0 MODE' ADC_LP"
		cset "name='TX1 MODE' ADC_LP"
		cset "name='TX2 MODE' ADC_NORMAL"
		cset "name='TX DEC0 MUX' SWR_MIC"
		cset "name='TX DEC1 MUX' SWR_MIC"
		cset "name='TX DEC2 MUX' SWR_MIC"
		cset "name='TX SMIC MUX0' SWR_MIC0"
		cset "name='TX SMIC MUX1' SWR_MIC1"
		cset "name='TX SMIC MUX2' SWR_MIC3"
		cset "name='TX_AIF1_CAP Mixer DEC0' 1"
		cset "name='TX_AIF1_CAP Mixer DEC1' 1"
		cset "name='TX_AIF1_CAP Mixer DEC2' 1"
		cset "name='DEC0 MODE' ADC_DEFAULT"
		cset "name='DEC1 MODE' ADC_DEFAULT"
		cset "name='DEC2 MODE' ADC_DEFAULT"
		cset "name='TX_DEC0 Volume' 84"
		cset "name='TX_DEC1 Volume' 84"
		cset "name='TX_DEC2 Volume' 84"
	]
	DisableSequence [
		cset "name='MBHC Switch' 0"
		cset "name='TX_AIF1_CAP Mixer DEC0' 0"
		cset "name='TX_AIF1_CAP Mixer DEC1' 0"
		cset "name='TX_AIF1_CAP Mixer DEC2' 0"
		cset "name='TX SMIC MUX0' ZERO"
		cset "name='TX SMIC MUX1' ZERO"
		cset "name='TX SMIC MUX2' ZERO"
		cset "name='ADC1_MIXER Switch' 0"
		cset "name='ADC1 MUX' CH1_AMIC_DISABLE"
		cset "name='ADC1 Switch' 0"
		cset "name='ADC2_MIXER Switch' 0"
		cset "name='ADC2 MUX' CH2_AMIC_DISABLE"
		cset "name='ADC2 Switch' 0"
		cset "name='ADC4_MIXER Switch' 0"
		cset "name='ADC4 MUX' CH4_AMIC_DISABLE"
		cset "name='ADC4 Switch' 0"
	]

	Value {
		CapturePCM "hw:${CardId},2"
	}
}''', hifi, count=1)
    unused = ('CapturePriority ', '/codecs/wsa883x/',
              '/codecs/qcom-lpass/wsa-macro/', 'MultiMedia4 Mixer VA_CODEC_DMA_TX_0')
    for name, content in [('OnePlus-Ace5Pro.conf', card), ('HiFi.conf', hifi)]:
        write(root, str(profile.relative_to(root)) + '/' + name,
              ''.join(line for line in content.splitlines(keepends=True)
                      if not any(value in line for value in unused)))
    ucm = root / 'usr/share/alsa/ucm2/conf.d/sm8750/OnePlus-Ace5Pro-hummer-24811.conf'
    ucm.parent.mkdir(parents=True, exist_ok=True)
    ucm.unlink(missing_ok=True)
    ucm.symlink_to('../../Qualcomm/sm8750/OnePlus-Ace5Pro/OnePlus-Ace5Pro.conf')
    write(root, 'etc/systemd/system/ace5pro-audio-ucm.service',
          '[Unit]\nRequires=dev-snd-controlC0.device\nAfter=dev-snd-controlC0.device\nBefore=display-manager.service\n'
          '[Service]\nType=oneshot\nExecStart=/usr/bin/alsaucm -c hw:0 -b /usr/lib/ace5pro/audio-ucm.batch\nRemainAfterExit=yes\n'
          '[Install]\nWantedBy=multi-user.target\n')
    write(root, 'etc/wireplumber/wireplumber.conf.d/51-ace5pro-ucm.conf',
          'monitor.alsa.rules = [\n'
          '  { matches = [ { device.name = "alsa_card.platform-sound" } ]\n'
          '    actions = { update-props = { api.alsa.use-ucm = true api.acp.auto-profile = true } } },\n'
          '  { matches = [ { node.name = "~alsa_output.platform-sound.pro-output-.*" }\n'
          '                { node.name = "~alsa_input.platform-sound.pro-input-.*" } ]\n'
          '    actions = { update-props = { audio.position = [ FL FR ] } } }\n'
          ']\n')
    wants = root / 'etc/systemd/system/multi-user.target.wants/ace5pro-audio-ucm.service'
    wants.parent.mkdir(parents=True, exist_ok=True)
    wants.unlink(missing_ok=True)
    wants.symlink_to('../ace5pro-audio-ucm.service')


def stage_bt_firmware(root):
    source = SOURCE / 'bt-firmware' / 'qca'
    target = root / 'lib/firmware/qca'
    files = sorted(path for path in source.glob('*') if path.is_file())
    if not files:
        raise FileNotFoundError(source)
    target.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.copy2(path, target / path.name)


def sync_rootfs_kernel_payload(root, spec):
    root = Path(root)
    modules = Path(spec['modules']).resolve()
    if not modules.is_relative_to(OUTPUT):
        raise ValueError('Kernel modules must be staged inside build/out')
    release = spec['release']
    source = modules / 'lib/modules' / release
    modules_root = root / 'usr/lib/modules'
    if not source.is_dir() or modules_root.is_symlink():
        raise ValueError('Kernel module tree is incomplete')
    modules_root.mkdir(parents=True, exist_ok=True)
    target = modules_root / release
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    dependencies = {}
    for line in (source / 'modules.dep').read_text().splitlines():
        name, _, required = line.partition(':')
        dependencies[name.removeprefix('kernel/')] = [item.removeprefix('kernel/') for item in required.split()]
    required = {line.strip() for line in (SOURCE / 'init/rootfs-modules.txt').read_text().splitlines() if line.strip()}
    pending = list(required)
    while pending:
        name = pending.pop()
        if name not in dependencies:
            raise ValueError('Missing rootfs module: ' + name)
        for dependency in dependencies[name]:
            if dependency not in required:
                required.add(dependency)
                pending.append(dependency)
    target.mkdir(parents=True)
    for name in required:
        destination = target / 'kernel' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / 'kernel' / name, destination)
    for name in ['modules.builtin', 'modules.builtin.modinfo']:
        shutil.copy2(source / name, target / name)
    order = (source / 'modules.order').read_text().splitlines()
    (target / 'modules.order').write_text(''.join(line + '\n' for line in order if line.removeprefix('kernel/') in required))
    write(root, 'etc/ace5pro-kernel-release', release + '\n')
    plymouth_theme = root / 'etc/alternatives/default.plymouth'
    if plymouth_theme.is_symlink() or plymouth_theme.exists():
        plymouth_theme.unlink()
    plymouth_theme.symlink_to('/usr/share/plymouth/themes/spinner/spinner.plymouth')
    for name, content in ROOTFS_FILES.items():
        write(root, name, content)
    stage_wifi_firmware(root)
    stage_adsp_firmware(root)
    stage_audio_topology(root)
    stage_bt_firmware(root)
    package = root / 'tmp/mesa-sm8750.deb'
    shutil.copy2(BUILD / 'out/mesa/mesa-sm8750.deb', package)
    run('chroot', root, 'dpkg', '-i', '/tmp/mesa-sm8750.deb')
    package.unlink()
    run('depmod', '-b', root, release)


def provision(root, spec):
    ubuntu = spec['config']['ubuntu']
    settings = spec['config']['rootfs']
    run('tar', '--extract', '--gzip', '--file', spec['base'], '--directory', root,
        '--numeric-owner', '--same-owner', '--xattrs', '--xattrs-include=*')
    for directory in ['dev', 'proc', 'sys', 'run', 'tmp']:
        (root / directory).mkdir(exist_ok=True)
    mounts = []

    def mount(kind, target, options=None):
        args = ['mount', '-t', kind]
        if options:
            args += ['-o', options]
        run(*args, kind, root / target)
        mounts.append(root / target)

    def bind(source, target):
        (root / target).mkdir(parents=True, exist_ok=True)
        run('mount', '--bind', source, root / target)
        mounts.append(root / target)

    def chroot(*command, **kwargs):
        return run('chroot', root, '/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive',
                   'LC_ALL=C.UTF-8', 'SYSTEMD_OFFLINE=1', *command, **kwargs)

    try:
        mount('tmpfs', 'dev', 'mode=0755')
        for name, major, minor, mode in [('null', 1, 3, 0o666), ('zero', 1, 5, 0o666),
                                       ('random', 1, 8, 0o666), ('urandom', 1, 9, 0o666),
                                       ('tty', 5, 0, 0o666)]:
            os.mknod(root / 'dev' / name, stat.S_IFCHR | mode, os.makedev(major, minor))
            (root / 'dev' / name).chmod(mode)
        (root / 'dev/pts').mkdir()
        (root / 'dev/shm').mkdir()
        mount('devpts', 'dev/pts', 'newinstance,ptmxmode=0666,mode=0620,gid=5')
        mount('tmpfs', 'dev/shm', 'mode=1777')
        for name, target in [('ptmx', 'pts/ptmx'), ('fd', '/proc/self/fd'),
                             ('stdin', '/proc/self/fd/0'), ('stdout', '/proc/self/fd/1'),
                             ('stderr', '/proc/self/fd/2')]:
            (root / 'dev' / name).symlink_to(target)
        mount('proc', 'proc', 'nosuid,nodev,noexec')
        mount('sysfs', 'sys', 'ro,nosuid,nodev,noexec')
        mount('tmpfs', 'run', 'mode=0755')
        archives = BUILD / 'apt-archives'
        apt_cache = root / 'var/cache/apt/archives'
        archives.mkdir(parents=True, exist_ok=True)
        bind(archives, 'var/cache/apt/archives')
        write(root, 'etc/resolv.conf', Path('/etc/resolv.conf').read_text())
        write(root, 'etc/ssl/certs/ca-certificates.crt',
              Path('/etc/ssl/certs/ca-certificates.crt').read_text())
        write(root, 'etc/apt/apt.conf.d/99-build-ca',
              'Acquire::https::CaInfo "/etc/ssl/certs/ca-certificates.crt";\n')
        write(root, 'usr/sbin/policy-rc.d', '#!/bin/sh\nexit 101\n', 0o755)
        write(root, 'etc/apt/sources.list', '')
        write(root, 'etc/apt/sources.list.d/ubuntu.sources',
              f'Types: deb\nURIs: {ubuntu["archive"]}\n'
              f'Suites: {ubuntu["suite"]} {ubuntu["suite"]}-updates {ubuntu["suite"]}-security\n'
              'Components: main restricted universe multiverse\nArchitectures: arm64\n'
              'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
        write(root, 'etc/apt/preferences.d/ace5pro-kernel',
              'Package: linux-image-* linux-headers-* linux-modules-* linux-generic* linux-virtual*\n'
              'Pin: version *\nPin-Priority: -1\n')
        write(root, 'etc/apt/preferences.d/no-snap',
              'Package: snapd gnome-software-plugin-snap firefox thunderbird chromium-browser chromium-chromedriver\n'
              'Pin: version *\nPin-Priority: -1\n')
        chroot('dpkg', '--print-architecture')
        chroot('apt', '-o', 'Acquire::Retries=3', '-o', 'Acquire::https::Timeout=20',
               '-o', 'APT::Update::Error-Mode=any', 'update')
        chroot('apt', '-y', '-o', 'Dpkg::Options::=--force-confold', 'install', *spec['packages'])
        chroot('dpkg', '--audit')
        chroot('update-ca-certificates', '--fresh')
        (root / 'etc/apt/apt.conf.d/99-build-ca').unlink()
        chroot('locale-gen', settings['locale'])
        chroot('update-locale', 'LANG=' + settings['locale'])
        write(root, 'etc/timezone', settings['timezone'] + '\n')
        (root / 'etc/localtime').unlink(missing_ok=True)
        (root / 'etc/localtime').symlink_to('/usr/share/zoneinfo/' + settings['timezone'])
        write(root, 'etc/hostname', settings['hostname'] + '\n')
        write(root, 'etc/hosts', '127.0.0.1 localhost\n127.0.1.1 ' + settings['hostname'] +
              '\n::1 localhost ip6-localhost ip6-loopback\n')
        write(root, 'etc/fstab', f'UUID={settings["uuid"]} / ext4 defaults,noatime,x-systemd.growfs,errors=remount-ro 0 1\n')
        write(root, 'etc/crypttab', '')
        write(root, 'etc/machine-id', '')
        write(root, 'etc/ace5pro-kernel-release', spec['release'] + '\n')
        for name, content in ROOTFS_FILES.items():
            write(root, name, content)
        for name in ['etc/systemd/system-generators/systemd-gpt-auto-generator',
                     'etc/systemd/system/udisks2.service',
                     'etc/systemd/system/systemd-pstore.service']:
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.unlink(missing_ok=True)
            destination.symlink_to('/dev/null')
        run('systemctl', '--root', root, 'set-default', 'graphical.target')
        chroot('passwd', '-l', 'root')
        for path in (root / 'var/lib/apt/lists').iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        (root / 'usr/sbin/policy-rc.d').unlink()
        (root / 'etc/resolv.conf').unlink()
        (root / 'etc/resolv.conf').symlink_to('../run/systemd/resolve/stub-resolv.conf')
        for path in (root / 'var/log').rglob('*'):
            if path.is_file() and not path.is_symlink():
                path.write_bytes(b'')
        (root / 'var/lib/dbus').mkdir(parents=True, exist_ok=True)
        (root / 'var/lib/dbus/machine-id').unlink(missing_ok=True)
        (root / 'var/lib/dbus/machine-id').symlink_to('/etc/machine-id')
        run('umount', apt_cache)
        mounts.remove(apt_cache)
        chroot('apt', 'clean')
    finally:
        for path in reversed(mounts):
            run('umount', path)


def make_image(root, spec):
    image = Path(spec['image'])
    manifest = Path(spec['package_manifest'])
    if image.parent.resolve() != manifest.parent.resolve() or image.exists():
        raise ValueError('Rootfs image and package manifest must share a new output directory')
    result = run('chroot', root, 'dpkg-query', '-W',
                 '-f=${binary:Package}\t${Version}\t${Architecture}\n',
                 capture_output=True, text=True)
    if any(line.split('\t')[0].split(':')[0] in ['snapd', 'gnome-software-plugin-snap']
           for line in result.stdout.splitlines()):
        raise ValueError('Unexpected Snap package in rootfs')
    manifest.write_text(result.stdout)
    used = int(subprocess.check_output(['du', '-sx', '-B1', str(root)], text=True).split()[0])
    size = ((used * 5 // 4 + 1024 ** 3 + 256 * 1024 ** 2 - 1) // (256 * 1024 ** 2)) * (256 * 1024 ** 2)
    settings = spec['config']['rootfs']
    with image.open('xb') as output:
        output.truncate(size)
    run('mkfs.ext4', '-q', '-b', '4096', '-m', '0', '-U', settings['uuid'],
        '-L', settings['label'], '-E', 'lazy_itable_init=0,lazy_journal_init=0', '-d', root, image)
    result = run('e2fsck', '-fn', image, capture_output=True, text=True)
    image.with_suffix('.fsck.txt').write_text(result.stdout + result.stderr)
    if (image.stat().st_uid, image.stat().st_gid) != (spec['uid'], spec['gid']):
        os.chown(image, spec['uid'], spec['gid'])
    image.chmod(0o644)


def prepare_rootfs(spec_path):
    if os.geteuid() != 0:
        raise SystemExit('Run through build.py')
    spec = json.loads(spec_path.read_text())
    root = Path(spec['root']).resolve()
    if not root.is_relative_to(BUILD) or not root.name.startswith('ubuntu-'):
        raise ValueError('Rootfs must be a cache directory inside build')
    if spec['reuse']:
        if not root.is_dir():
            raise FileNotFoundError(root)
    else:
        root.mkdir(parents=True, exist_ok=True)
        provision(root, spec)
    sync_rootfs_kernel_payload(root, spec)
    make_image(root, spec)


def cpio_entry(name, mode, data, ino):
    name_bytes = name.encode()
    fields = [
        0x070701,
        ino,
        mode,
        0,
        0,
        1,
        0,
        len(data),
        0,
        0,
        0,
        0,
        len(name_bytes) + 1,
        0,
    ]
    header = b"070701" + b"".join(f"{field:08x}".encode() for field in fields[1:])
    record = header + name_bytes + b"\0"
    record += b"\0" * ((-len(record)) % 4)
    record += data
    return record + b"\0" * ((-len(record)) % 4)


def make_cpio(root):
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_dir():
            mode |= stat.S_IFDIR
            data = b""
        elif path.is_file():
            mode |= stat.S_IFREG
            data = path.read_bytes()
        else:
            raise ValueError(f"unsupported initramfs entry: {path}")
        entries.append((relative, mode, data))
    output = bytearray()
    for index, (name, mode, data) in enumerate(entries, 1):
        output.extend(cpio_entry(name, mode, data, index))
    output.extend(cpio_entry("TRAILER!!!", 0, b"", len(entries) + 1))
    return bytes(output)


MAGIC = 0x5941504C
VERSION = 1
HEADER_SIZE = 128
BUILD_ID_SIZE = 64
HEADER_FORMAT = "<IIIIIIIIII64s6I"

def align(value, alignment=4096):
    return (value + alignment - 1) & ~(alignment - 1)

def validate_header(data, available=None):
    if available is None:
        available = len(data)
    if available < HEADER_SIZE or len(data) < HEADER_SIZE:
        raise ValueError("payload shorter than header")
    values = struct.unpack_from(HEADER_FORMAT, data, 0)
    magic, version, header_size, total_size = values[:4]
    if magic != MAGIC or version != VERSION:
        raise ValueError("payload magic or version mismatch")
    if header_size < HEADER_SIZE or header_size > total_size or total_size > available:
        raise ValueError("payload header bounds invalid")
    ranges = []
    for name, offset, size in zip(("image", "dtb", "initrd"), values[4:10:2], values[5:10:2]):
        if offset < header_size or offset > total_size or size > total_size - offset:
            raise ValueError(f"{name} bounds invalid")
        ranges.append((offset, offset + size, name))
    for left, right in zip(sorted(ranges), sorted(ranges)[1:]):
        if left[1] > right[0]:
            raise ValueError("payload segments overlap")
    build_id = values[10].split(b"\0", 1)[0].decode("ascii", "replace")
    return {
        "magic": magic,
        "version": version,
        "header_size": header_size,
        "total_size": total_size,
        "image_offset": values[4],
        "image_size": values[5],
        "dtb_offset": values[6],
        "dtb_size": values[7],
        "initrd_offset": values[8],
        "initrd_size": values[9],
        "build_id": build_id,
    }

def validate_build_id(build_id):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,62}', build_id):
        raise ValueError('Build ID must be ASCII [A-Za-z0-9._-], up to 63 characters')


def make_bundle(image, dtb, initrd, build_id):
    validate_build_id(build_id)
    image_offset = align(HEADER_SIZE)
    dtb_offset = align(image_offset + len(image))
    initrd_offset = align(dtb_offset + len(dtb))
    total_size = initrd_offset + len(initrd)
    build_field = build_id.encode("ascii") + b"\0"
    build_field = build_field.ljust(BUILD_ID_SIZE, b"\0")
    header = struct.pack(
        HEADER_FORMAT,
        MAGIC,
        VERSION,
        HEADER_SIZE,
        total_size,
        image_offset,
        len(image),
        dtb_offset,
        len(dtb),
        initrd_offset,
        len(initrd),
        build_field,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    output = bytearray(total_size)
    output[:HEADER_SIZE] = header
    output[image_offset:image_offset + len(image)] = image
    output[dtb_offset:dtb_offset + len(dtb)] = dtb
    output[initrd_offset:initrd_offset + len(initrd)] = initrd
    validate_header(output)
    return bytes(output)


def load_firmware(inputs):
    template = inputs['uefi_image'].read_bytes()
    if len(template) < 4096 or template[:8] != b'ANDROID!':
        raise ValueError('Mu template is not an Android boot image')
    kernel_size, ramdisk_size = struct.unpack_from('<II', template, 8)
    if struct.unpack_from('<I', template, 40)[0] != 4 or struct.unpack_from('<I', template, 20)[0] != 1584:
        raise ValueError('Mu template must use Android header v4')
    ramdisk_end = align(align(4096 + kernel_size) + ramdisk_size)
    signature = template[ramdisk_end:ramdisk_end + 4096]
    if len(signature) != 4096:
        raise ValueError('Mu template signature is incomplete')
    stream = zlib.decompressobj(31)
    decoded = stream.decompress(template[4096:4096 + kernel_size]) + stream.flush()
    shim = inputs['boot_shim'].read_bytes()
    firmware = inputs['uefi_fd'].read_bytes()
    if decoded != shim + firmware:
        raise ValueError('Mu image, BootShim and FD do not match; build Mu with -d hummer -r RELEASE first')
    return template, shim, firmware, signature, stream.unused_data


def pack_boot(image_path, dtb_path, initrd_path, build_id, inputs, destination):
    bundle = make_bundle(image_path.read_bytes(), dtb_path.read_bytes(), initrd_path.read_bytes(), build_id)
    template, shim, firmware, signature, trailer = load_firmware(inputs)
    kernel = gzip.compress(shim + firmware + bundle, compresslevel=9, mtime=0) + trailer
    header = bytearray(template[:4096])
    struct.pack_into('<II', header, 8, len(kernel), 0)
    boot = bytes(header) + kernel
    boot += bytes(align(len(boot)) - len(boot)) + signature
    destination.write_bytes(boot)


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()

def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')

def rootfs_cache():
    if not (ROOTFS_CACHE / 'etc/os-release').is_file():
        raise FileNotFoundError(ROOTFS_CACHE)
    return ROOTFS_CACHE

def main():
    parser = argparse.ArgumentParser(description='Build Ubuntu 26.10 arm64 for OnePlus Ace 5 Pro')
    parser.add_argument('--prepare-rootfs', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('-b', '--build-id')
    parser.add_argument('-r', '--rootfs', type=Path, metavar='DIR')
    parser.add_argument('-nr', '--no-rootfs', action='store_true')
    parser.add_argument('destination', nargs='?', type=Path, metavar='DESTINATION')
    args = parser.parse_args()
    if args.prepare_rootfs:
        prepare_rootfs(args.prepare_rootfs)
        return
    reuse = args.rootfs is not None
    build_dir = BUILD
    out_dir = OUTPUT
    artifact_root = args.destination.expanduser().resolve() if args.destination else out_dir
    downloads = build_dir / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    build_lock = (build_dir / 'build.lock').open('a')
    try:
        fcntl.flock(build_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another Ubuntu build is still running')
    config = tomllib.loads((ROOT / 'cfg.toml').read_text())
    uuid.UUID(config['rootfs']['uuid'])
    if config['ubuntu']['version'] != '26.10' or config['ubuntu']['suite'] != 'stonking':
        raise ValueError('This configuration targets Ubuntu 26.10 stonking')
    inputs = {name: (ROOT / value).resolve() for name, value in config['inputs'].items()}
    kernel = inputs['kernel']
    image = kernel / 'arch/arm64/boot/Image'
    dtb = kernel / 'arch/arm64/boot/dts/qcom/sm8750-oneplus-hummer.dtb'
    release = (kernel / 'include/config/kernel.release').read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9._+-]+', release):
        raise ValueError('Invalid kernel release')
    tools = ['depmod', 'modinfo', 'llvm-strip', 'aarch64-linux-gnu-gcc']
    if not args.no_rootfs:
        tools += ['sudo', 'unshare', 'tar', 'gpgv', 'mkfs.ext4', 'e2fsck',
                  'qemu-aarch64', 'img2simg']
    missing = [name for name in tools if shutil.which(name) is None]
    if missing:
        raise ValueError('Missing dependencies: ' + ', '.join(missing))
    if not args.no_rootfs:
        registration = Path('/proc/sys/fs/binfmt_misc/qemu-aarch64')
        if not registration.is_file() or 'enabled' not in registration.read_text() or 'F' not in registration.read_text().split('flags:')[-1].splitlines()[0]:
            raise ValueError('Enable qemu-user-binfmt with the F flag before building')
        subprocess.run(['sudo', '-v'], check=True)
    for path in [image, dtb, inputs['uefi_image'], inputs['uefi_fd'], inputs['boot_shim']]:
        if not path.is_file():
            raise FileNotFoundError(path)
    load_firmware(inputs)
    kernel_config = (kernel / '.config').read_text()
    for option in ['CONFIG_EXT4_FS=y', 'CONFIG_EFI_STUB=y', 'CONFIG_EFI_PARTITION=y']:
        if option not in kernel_config.splitlines():
            raise ValueError('Kernel missing ' + option)
    packages = ['sudo', 'curl', 'iputils-ping', 'systemd-resolved', 'systemd-timesyncd',
                'openssh-server',
                'ubuntu-desktop',
                'language-pack-zh-hans', 'language-pack-gnome-zh-hans',
                'fonts-noto-cjk']
    build_id = args.build_id or datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    validate_build_id(build_id)
    output = artifact_root / build_id.lower()
    output.mkdir(parents=True, exist_ok=False)
    work = output / 'work'
    work.mkdir()
    log = (output / 'build.log').open('w')
    def run(*command, **kwargs):
        log.write('\n$ ' + ' '.join(str(value) for value in command) + '\n')
        log.flush()
        command = [str(value) for value in command]
        sys.stdout.flush()
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              bufsize=0, **kwargs) as process:
            while chunk := os.read(process.stdout.fileno(), 65536):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                log.buffer.write(chunk)
                log.buffer.flush()
            result = process.wait()
        if result:
            raise subprocess.CalledProcessError(result, command)
        return subprocess.CompletedProcess(command, result)
    def stage(message):
        print(message, flush=True)
    stage(f'{build_id}: {output}\nBuild log: {output / "build.log"}')
    try:
        ubuntu = config['ubuntu']
        base = downloads / ubuntu['base_file']
        if not reuse and not args.no_rootfs:
            stage('Verifying the official Ubuntu Base archive')
            for name in ['SHA256SUMS', 'SHA256SUMS.gpg', ubuntu['base_file']]:
                path = downloads / name
                if not path.exists():
                    temporary = path.with_name(path.name + '.part')
                    with urllib.request.urlopen(ubuntu['base_url'] + name, timeout=60) as source, temporary.open('wb') as target:
                        shutil.copyfileobj(source, target)
                    temporary.replace(path)
            run('gpgv', '--keyring', '/usr/share/keyrings/ubuntu-cdimage-keyring.gpg',
                downloads / 'SHA256SUMS.gpg', downloads / 'SHA256SUMS')
            sums = {}
            for line in (downloads / 'SHA256SUMS').read_text().splitlines():
                checksum, filename = line.split(maxsplit=1)
                sums[filename.lstrip('*')] = checksum
            if digest(base) != ubuntu['base_sha256'] or sums.get(base.name) != ubuntu['base_sha256']:
                raise ValueError('Ubuntu Base checksum mismatch')
        stage('Checking and staging matching kernel modules')
        module_paths = [str(Path(name).with_suffix('.ko')) for name in
                        (kernel / 'modules.order').read_text().splitlines()]
        module_hashes = {}
        for name in module_paths:
            if not (kernel / name).resolve().is_relative_to(kernel) or not name.endswith('.ko'):
                raise ValueError('Invalid module path: ' + name)
            module_hashes[name] = digest(kernel / name)
        early_modules = (SOURCE / 'init/modules.txt').read_text().splitlines()
        for name in early_modules:
            if name not in module_hashes:
                raise ValueError('Missing early module: ' + name)
            magic = subprocess.check_output(['modinfo', '-F', 'vermagic', str(kernel / name)], text=True).split()[0]
            if magic != release:
                raise ValueError('Kernel/module version mismatch: ' + name)
        staged_module_paths = early_modules if args.no_rootfs else module_paths
        staged_module_hashes = {name: module_hashes[name] for name in staged_module_paths}
        module_key = hashlib.sha256(json.dumps([release, staged_module_hashes], sort_keys=True).encode()).hexdigest()
        modules = out_dir / ('modules-' + module_key[:16] + ('-boot' if args.no_rootfs else ''))
        if not (modules / 'ready').exists():
            if modules.exists():
                shutil.rmtree(modules)
            module_root = modules / 'lib/modules' / release
            batch = []
            for name in staged_module_paths:
                target = module_root / 'kernel' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(kernel / name, target)
                batch.append(target)
                if len(batch) == 128:
                    run('llvm-strip', '--strip-debug', *batch)
                    batch.clear()
            if batch:
                run('llvm-strip', '--strip-debug', *batch)
            for name in ['modules.builtin', 'modules.builtin.modinfo']:
                shutil.copy2(kernel / name, module_root / name)
            (module_root / 'modules.order').write_text(''.join('kernel/' + name + '\n' for name in staged_module_paths))
            run('depmod', '-b', modules, release)
            (modules / 'ready').write_text(module_key)
        if args.rootfs:
            root_cache = args.rootfs.resolve()
        else:
            root_cache = rootfs_cache()
        if not root_cache.is_relative_to(build_dir):
            raise ValueError('Rootfs cache must be inside build')
        if args.no_rootfs:
            if not (root_cache / 'etc/os-release').is_file():
                raise FileNotFoundError(root_cache)
            stage(f'Using rootfs cache {root_cache}; building boot only')
        else:
            run(sys.executable, SOURCE / 'mesa/build.py')
            root_cache.mkdir(parents=True, exist_ok=True)
            spec = {'config': config, 'base': str(base), 'root': str(root_cache),
                    'modules': str(modules), 'release': release, 'packages': packages,
                    'package_manifest': str(output / 'packages.tsv'), 'image': str(output / 'rootfs.img'),
                    'reuse': reuse or (root_cache / 'var/lib/dpkg/status').is_file(),
                    'uid': os.getuid(), 'gid': os.getgid()}
            save_json(work / 'rootfs.json', spec)
            stage('Building Ubuntu Desktop in an isolated ARM64 chroot; this can take a while' if not reuse else
                  f'Reusing rootfs cache {root_cache}; syncing kernel modules')
            run('sudo', '-n', '--preserve-env=http_proxy,https_proxy,no_proxy,HTTP_PROXY,HTTPS_PROXY,NO_PROXY',
                'unshare', '--mount', '--pid', '--fork', '--mount-proc',
                sys.executable, '-u', '-B', Path(__file__).resolve(), '--prepare-rootfs', work / 'rootfs.json')
        stage('Building initramfs for a persistent read-write UFS root')
        staging = work / 'initramfs'
        for name in ['dev', 'proc', 'sys', 'run', 'tmp', 'modules', 'reference', 'lib']:
            (staging / name).mkdir(parents=True, exist_ok=True)
        for name, target in [('init', 'init'), ('probe', 'hardware-probe')]:
            run('aarch64-linux-gnu-gcc', '-static', '-Os', '-s', '-Wall', '-Wextra', '-Werror',
                '-ffunction-sections', '-fdata-sections', '-Wl,--gc-sections',
                f'-DUBUNTU_ROOT_UUID="{config["rootfs"]["uuid"]}"',
                '-o', staging / target, SOURCE / 'init' / (name + '.c'))
        if not args.no_rootfs:
            run('qemu-aarch64', staging / 'init', '--check-root-image', output / 'rootfs.img')
        for name in early_modules:
            shutil.copy2(modules / 'lib/modules' / release / 'kernel' / name, staging / 'modules' / Path(name).name)
        (staging / 'modules/load-order').write_text(''.join(Path(name).name + '\n' for name in early_modules))
        stage_wifi_firmware(staging)
        stage_bt_firmware(staging)
        stage_gpu_firmware(staging)
        shutil.copy2(SOURCE / 'init/disks.txt', staging / 'reference/disks.txt')
        initrd = output / 'initramfs.cpio.gz'
        initrd.write_bytes(gzip.compress(make_cpio(staging), compresslevel=9, mtime=0))
        stage('Packing the Mu + Linux boot image')
        pack_boot(image, dtb, initrd, build_id, inputs, output / 'boot.img')
        if not args.no_rootfs:
            stage('Converting rootfs to Android sparse format for fastboot')
            run('img2simg', output / 'rootfs.img', output / 'rootfs-fastboot.img', '4096')
        files = ['boot.img', 'initramfs.cpio.gz']
        if not args.no_rootfs:
            files += ['rootfs.img', 'rootfs-fastboot.img', 'packages.tsv']
        checksums = {name: digest(output / name) for name in files}
        (output / 'SHA256SUMS').write_text(''.join(f'{value}  {name}\n' for name, value in checksums.items()))
        manifest = {
            'build_id': build_id, 'status': 'HOST_BUILT_HARDWARE_NOT_TESTED',
            'ubuntu': ubuntu, 'kernel-release': release, 'root_uuid': config['rootfs']['uuid'],
            'root_mode': 'UFS ext4 read-write; persistent root filesystem',
            'root_auto_grow': True,
            'ufs_writes_enabled': True, 'esp_required': False,
            'kernel': {'path': str(image), 'sha256': digest(image)},
            'dtb': {'path': str(dtb), 'sha256': digest(dtb)},
            'firmware': {name: {'path': str(path), 'sha256': digest(path)} for name, path in inputs.items() if name != 'kernel'},
            'modules': module_hashes if not args.no_rootfs else staged_module_hashes,
            'rootfs_cache': str(root_cache),
            'rootfs_built': not args.no_rootfs, 'outputs': checksums,
            'first_login': 'GNOME Initial Setup; no preset user or password'}
        if not args.no_rootfs:
            manifest['fastboot_image'] = 'rootfs-fastboot.img'
        save_json(output / 'manifest.json', manifest)
        shutil.rmtree(work)
        rootfs_targets = {}
        if args.no_rootfs:
            for name in ['rootfs.img', 'rootfs-fastboot.img']:
                link = out_dir / name
                if link.is_symlink():
                    target = link.resolve()
                    if target.is_file():
                        rootfs_targets[name] = os.path.relpath(target, out_dir)
        latest = out_dir / 'latest'
        latest.unlink(missing_ok=True)
        latest.symlink_to(os.path.relpath(output, out_dir), target_is_directory=True)
        link_names = ['boot.img', 'initramfs.cpio.gz', 'SHA256SUMS']
        if not args.no_rootfs:
            link_names += ['rootfs.img', 'rootfs-fastboot.img']
        for name in link_names:
            link = out_dir / name
            if link.is_symlink():
                link.unlink()
            if not link.exists():
                link.symlink_to('latest/' + name)
        for name, target in rootfs_targets.items():
            link = out_dir / name
            if link.is_symlink():
                link.unlink()
            if not link.exists():
                link.symlink_to(target)
        if args.no_rootfs:
            stage(f'Built: {output / "boot.img"}\nNo rootfs generated.\nNo phone operation performed.')
        else:
            stage(f'Built: {output / "boot.img"}\nBuilt: {output / "rootfs.img"}\n'
                  f'Built: {output / "rootfs-fastboot.img"}\nNo phone operation performed.')
    finally:
        log.close()
if __name__ == '__main__':
    main()
