#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/klog.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static int write_all(int fd, const void *data, size_t length)
{
    const char *cursor = data;
    while (length) {
        ssize_t count = write(fd, cursor, length);
        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0)
            return -1;
        cursor += count;
        length -= (size_t)count;
    }
    return 0;
}

static ssize_t read_file(const char *path, char *buffer, size_t capacity)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    size_t total = 0;
    if (fd < 0)
        return -1;
    while (total + 1 < capacity) {
        ssize_t count = read(fd, buffer + total, capacity - total - 1);
        if (count < 0 && errno == EINTR)
            continue;
        if (count < 0) {
            int saved = errno;
            close(fd);
            errno = saved;
            return -1;
        }
        if (!count)
            break;
        total += (size_t)count;
    }
    close(fd);
    buffer[total] = 0;
    return (ssize_t)total;
}

static void directory(const char *path)
{
    mkdir(path, 0755);
}

static void mount_fs(const char *source, const char *target, const char *type)
{
    unsigned long flags = MS_NOSUID | MS_NOEXEC;
    if (strcmp(type, "devtmpfs") && strcmp(type, "devpts"))
        flags |= MS_NODEV;
    mount(source, target, type, flags, "");
}

static int write_text(const char *path, const char *text)
{
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0)
        return -1;
    int result = write_all(fd, text, strlen(text));
    close(fd);
    return result;
}

#include "root.h"

#define KLOG_READ_ALL 3
#define KLOG_SIZE_BUFFER 10

static void save_probe_log(int status)
{
    int size = klogctl(KLOG_SIZE_BUFFER, NULL, 0);
    if (size <= 0 || prepare_ufs_root())
        return;
    char *buffer = malloc((size_t)size);
    if (!buffer)
        return;
    int length = klogctl(KLOG_READ_ALL, buffer, size);
    int fd = open("/ubuntu/var/log/ace5pro-early-boot.log",
                  O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd >= 0) {
        dprintf(fd, "hardware-probe status=0x%x\n", status);
        if (length > 0)
            write_all(fd, buffer, (size_t)length);
        fsync(fd);
        close(fd);
        sync();
    }
    free(buffer);
}

static void ubuntu_worker(void)
{
    int null = open("/dev/null", O_WRONLY | O_CLOEXEC);
    if (null >= 0) {
        dup2(null, STDOUT_FILENO);
        dup2(null, STDERR_FILENO);
        close(null);
    }
    pid_t probe = fork();
    if (probe == 0) {
        execl("/hardware-probe", "/hardware-probe", (char *)NULL);
        _exit(127);
    }
    if (probe < 0)
        _exit(1);
    int status = 0;
    pid_t waited;
    do {
        waited = waitpid(probe, &status, 0);
    } while (waited < 0 && errno == EINTR);
    if (waited != probe || probe <= 0 || !WIFEXITED(status) || WEXITSTATUS(status)) {
        save_probe_log(status);
        _exit(1);
    }
    if (prepare_ubuntu_root())
        _exit(1);
    if (chroot("/ubuntu") || chdir("/"))
        _exit(1);
    setenv("PATH", "/usr/sbin:/usr/bin:/sbin:/bin", 1);
    setenv("LANG", "C.UTF-8", 1);
    execl("/bin/sh", "sh", "/usr/lib/ace5pro/preflight.sh", (char *)NULL);
    _exit(127);
}

static int enter_ubuntu(void)
{
    struct stat st;
    if (stat("/ubuntu/usr/lib/systemd/systemd", &st) || !S_ISREG(st.st_mode))
        return -1;
    int console = open("/dev/console", O_RDWR | O_NOCTTY);
    if (console < 0)
        return -1;
    for (int i = 0; i < 3; i++)
        if (dup2(console, i) < 0)
            return -1;
    if (console > 2)
        close(console);
    if (chdir("/ubuntu") || mount(".", "/", NULL, MS_MOVE, NULL) || chroot(".") || chdir("/"))
        return -1;
    setenv("PATH", "/usr/sbin:/usr/bin:/sbin:/bin", 1);
    setenv("LANG", "C.UTF-8", 1);
    setenv("TERM", "linux", 1);
    execl("/usr/lib/systemd/systemd", "systemd", (char *)NULL);
    return -1;
}

int main(int argc, char **argv)
{
    if (argc == 3 && !strcmp(argv[1], "--check-root-image"))
        return check_root_image(argv[2]);
    if (getpid() != 1) {
        fputs("This program must run as PID 1.\n", stderr);
        return 2;
    }
    directory("/dev");
    const char *dirs[] = {"/proc", "/sys", "/run", "/tmp"};
    for (size_t i = 0; i < sizeof(dirs) / sizeof(dirs[0]); i++)
        directory(dirs[i]);
    mount_fs("proc", "/proc", "proc");
    mount_fs("sysfs", "/sys", "sysfs");
    mount_fs("devtmpfs", "/dev", "devtmpfs");
    mount_fs("tmpfs", "/run", "tmpfs");
    mount_fs("tmpfs", "/tmp", "tmpfs");
    directory("/dev/pts");
    mount_fs("devpts", "/dev/pts", "devpts");
    mount_fs("debugfs", "/sys/kernel/debug", "debugfs");
    mount_fs("pstore", "/sys/fs/pstore", "pstore");
    pid_t worker = fork();
    if (worker == 0)
        ubuntu_worker();
    char preflight[96];
    for (;;) {
        if (read_file("/run/ubuntu-root.ready", preflight, sizeof(preflight)) > 0 &&
            !strcmp(preflight, "PASS\n") &&
            read_file("/run/userspace.status", preflight, sizeof(preflight)) > 0 &&
            !strcmp(preflight, "PASS\n"))
            break;
        while (waitpid(-1, NULL, WNOHANG) > 0) {}
        sleep(1);
    }
    sync();
    enter_ubuntu();
    for (;;) {
        while (waitpid(-1, NULL, WNOHANG) > 0) {}
        sleep(5);
    }
}
