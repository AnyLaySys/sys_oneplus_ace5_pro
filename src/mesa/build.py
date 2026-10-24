#!/usr/bin/env python3
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parents[1]
BUILD = ROOT / 'build/out/mesa'
CHECKOUT = ROOT / 'build/src/mesa'
SYSROOT = BUILD / 'mesa-sysroot'
OUTPUT = BUILD / 'mesa-a830'
PACKAGE = BUILD / 'mesa-sm8750.deb'


def run(*command):
    return subprocess.run([str(value) for value in command], check=True)


def digest(path, algorithm='sha256'):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def download(spec):
    archives = [BUILD / 'apt-archives', BUILD / 'downloads/mesa']
    for directory in archives:
        for path in directory.glob(spec['package'] + '_*.deb'):
            if digest(path, 'sha512') == spec['sha512']:
                return path
    path = archives[1] / urllib.parse.unquote(spec['url'].rsplit('/', 1)[1])
    temporary = path.with_suffix('.part')
    with urllib.request.urlopen(spec['url'], timeout=60) as source, temporary.open('wb') as target:
        shutil.copyfileobj(source, target)
    if digest(temporary, 'sha512') != spec['sha512']:
        raise ValueError('Package checksum mismatch: ' + spec['package'])
    temporary.replace(path)
    return path


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    inputs = hashlib.sha256()
    for path in sorted(SOURCE.rglob('*')):
        if path.is_file():
            inputs.update(str(path.relative_to(SOURCE)).encode() + b'\0')
            inputs.update(path.read_bytes())
    key = inputs.hexdigest()
    stamp = BUILD / 'mesa-inputs.sha256'
    if PACKAGE.is_file() and stamp.is_file() and stamp.read_text() == key + '\n' + digest(PACKAGE) + '\n':
        print('Reusing ' + str(PACKAGE))
        return
    upstream = json.loads((SOURCE / 'upstream.json').read_text())
    CHECKOUT.parent.mkdir(parents=True, exist_ok=True)
    if not (CHECKOUT / '.git').is_dir():
        run('git', 'clone', '--depth=1', '--branch', upstream['tag'], upstream['url'], CHECKOUT)
    commit = subprocess.check_output(['git', '-C', str(CHECKOUT), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != upstream['commit']:
        raise ValueError('Mesa checkout must be at ' + upstream['commit'])
    shutil.copytree(SOURCE / 'overlay', CHECKOUT, dirs_exist_ok=True)
    (BUILD / 'downloads/mesa').mkdir(parents=True, exist_ok=True)
    SYSROOT.mkdir(exist_ok=True)
    dependencies = json.loads((SOURCE / 'dependencies.json').read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        archives = list(pool.map(download, dependencies))
    for path in archives:
        run('dpkg-deb', '-x', path, SYSROOT)
    cross = BUILD / 'mesa-cross.ini'
    cross.write_text((SOURCE / 'cross.ini').read_text().replace('@SYSROOT@', str(SYSROOT)))
    reconfigure = (OUTPUT / 'build.ninja').is_file()
    command = OUTPUT / 'meson-private/cmd_line.txt'
    if reconfigure and (not command.is_file() or str(cross) not in command.read_text() or
                        str(CHECKOUT) not in command.read_text()):
        shutil.rmtree(OUTPUT)
        reconfigure = False
    run('meson', 'setup', *(['--reconfigure'] if reconfigure else []),
        OUTPUT, CHECKOUT, '--cross-file', cross, '--prefix=/opt/mesa-a830', '--libdir=lib',
        '--buildtype=release', '-Dplatforms=x11,wayland', '-Dgallium-drivers=freedreno',
        '-Dvulkan-drivers=freedreno', '-Dfreedreno-kmds=msm', '-Dllvm=disabled',
        '-Dglx=dri', '-Dglvnd=enabled', '-Degl=enabled', '-Dgbm=enabled',
        '-Dgles1=disabled', '-Dgles2=enabled', '-Dgallium-va=disabled',
        '-Dvideo-codecs=[]', '-Dbuild-tests=false', '-Dspirv-tools=disabled')
    run('ninja', '-C', OUTPUT, '-j', os.cpu_count() or 1)
    staging = BUILD / 'mesa-stage'
    run('meson', 'install', '-C', OUTPUT, '--no-rebuild', '--destdir', staging)
    package_root = BUILD / 'mesa-package'
    if package_root.exists():
        shutil.rmtree(package_root)
    source_lib = staging / 'opt/mesa-a830/lib'
    target_lib = package_root / 'opt/mesa-a830/lib'
    target_lib.mkdir(parents=True)
    for path in source_lib.iterdir():
        if path.is_file() and (path.name.startswith(('libEGL_mesa.so.', 'libGLX_mesa.so.',
                                                    'libgbm.so.', 'libgallium-')) or
                               path.name == 'libvulkan_freedreno.so'):
            shutil.copy2(path, target_lib / path.name, follow_symlinks=False)
    for directory, names in [('gbm', ['dri_gbm.so']), ('dri', ['libdril_dri.so', 'msm_dri.so'])]:
        (target_lib / directory).mkdir()
        for name in names:
            shutil.copy2(source_lib / directory / name, target_lib / directory / name, follow_symlinks=False)
    for path in target_lib.rglob('*'):
        if path.is_file() and not path.is_symlink():
            run('aarch64-linux-gnu-strip', '--strip-unneeded', path)
    config = package_root / 'etc/ld.so.conf.d/00-mesa-sm8750.conf'
    config.parent.mkdir(parents=True)
    config.write_text('/opt/mesa-a830/lib\n')
    documentation = package_root / 'usr/share/doc/mesa-sm8750'
    documentation.mkdir(parents=True)
    shutil.copy2(CHECKOUT / 'docs/license.rst', documentation / 'copyright')
    shutil.copytree(SOURCE / 'debian', package_root / 'DEBIAN')
    run('dpkg-deb', '--root-owner-group', '-Zxz', '-z6', '--build', package_root, PACKAGE)
    stamp.write_text(key + '\n' + digest(PACKAGE) + '\n')
    print('Built ' + str(PACKAGE))


if __name__ == '__main__':
    main()
