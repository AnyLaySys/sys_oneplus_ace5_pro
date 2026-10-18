#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/fs.h>
#include <linux/netlink.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static int event_field(const char *data, size_t size, const char *field)
{
    size_t expected = strlen(field);
    for (size_t offset = 0; offset < size;) {
        const char *end = memchr(data + offset, 0, size - offset);
        if (!end)
            return 0;
        size_t length = (size_t)(end - data - offset);
        if (length == expected && !memcmp(data + offset, field, length))
            return 1;
        offset += length + 1;
    }
    return 0;
}

struct binding_target {
    const char *devpath;
    const char *driver;
    const char *module;
    int required;
    int confirmed;
};

static struct binding_target bindings[] = {
    {"DEVPATH=/devices/platform/firmware:scm", "DRIVER=qcom_scm",
     "/modules/qnoc-sm8750.ko", 0, 0},
    {"DEVPATH=/devices/platform/soc@0/f100000.pinctrl", "DRIVER=sm8750-tlmm",
     "/modules/pinctrl-sm8750.ko", 0, 0},
    {"DEVPATH=/devices/platform/soc@0/15000000.iommu", "DRIVER=arm-smmu",
     "/modules/arm_smmu.ko", 0, 0},
    {"DEVPATH=/devices/platform/soc@0/1d80000.phy", "DRIVER=qcom-qmp-ufs-phy",
     "/modules/phy-qcom-qmp-ufs.ko", 0, 0},
    {"DEVPATH=/devices/platform/soc@0/1d84000.ufs", "DRIVER=ufshcd-qcom",
     "/modules/ufs-qcom.ko", 0, 0},
};

static int binding_event(const char *data, size_t size, const struct binding_target *target)
{
    return event_field(data, size, "ACTION=bind") &&
        event_field(data, size, target->devpath) &&
        event_field(data, size, target->driver);
}

static int watch_bind_events(void)
{
    for (size_t i = 0; i < sizeof(bindings) / sizeof(bindings[0]); i++)
        bindings[i].required = access(bindings[i].module, R_OK) == 0;
    int fd = socket(AF_NETLINK, SOCK_DGRAM | SOCK_CLOEXEC | SOCK_NONBLOCK, NETLINK_KOBJECT_UEVENT);
    if (fd < 0)
        return -1;
    int size = 1024 * 1024;
    struct sockaddr_nl address = {.nl_family = AF_NETLINK, .nl_groups = 1};
    if (setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &size, sizeof(size)) ||
        bind(fd, (struct sockaddr *)&address, sizeof(address))) {
        close(fd);
        return -1;
    }
    return fd;
}

static void finish_binding_checks(int fd)
{
    if (fd < 0)
        return;
    struct timespec start, now;
    clock_gettime(CLOCK_MONOTONIC, &start);
    for (;;) {
        char data[8192];
        struct sockaddr_nl source;
        socklen_t length = sizeof(source);
        ssize_t count = recvfrom(fd, data, sizeof(data), MSG_TRUNC,
                                 (struct sockaddr *)&source, &length);
        if (count >= 0) {
            if ((size_t)count > sizeof(data))
                break;
            if (length >= sizeof(source) && source.nl_pid == 0) {
                for (size_t i = 0; i < sizeof(bindings) / sizeof(bindings[0]); i++) {
                    struct binding_target *target = &bindings[i];
                    if (target->required && !target->confirmed && binding_event(data, (size_t)count, target))
                        target->confirmed = 1;
                }
            }
        } else if (errno != EAGAIN && errno != EINTR) {
            break;
        }
        int pending = 0;
        for (size_t i = 0; i < sizeof(bindings) / sizeof(bindings[0]); i++)
            pending += bindings[i].required && !bindings[i].confirmed;
        if (!pending)
            break;
        clock_gettime(CLOCK_MONOTONIC, &now);
        if ((now.tv_sec - start.tv_sec) * 1000000000LL + now.tv_nsec - start.tv_nsec >= 3000000000LL)
            break;
        if (count < 0 && errno == EAGAIN) {
            struct pollfd event = {.fd = fd, .events = POLLIN};
            poll(&event, 1, 100);
        }
    }
    close(fd);
}

static int load_module(const char *path)
{
    usleep(200000);
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return -1;
    long result = syscall(SYS_finit_module, fd, "", 0);
    int saved = result < 0 ? errno : 0;
    close(fd);
    return result == 0 || saved == EEXIST ? 0 : -1;
}

static int load_plan(void)
{
    FILE *plan = fopen("/modules/load-order", "r");
    char module[128], path[160];
    if (!plan)
        return errno == ENOENT ? 0 : -1;
    while (fgets(module, sizeof(module), plan)) {
        module[strcspn(module, "\r\n")] = 0;
        if (!module[0] || strlen(module) != strspn(module, "abcdefghijklmnopqrstuvwxyz0123456789-_.") ||
            strstr(module, "..")) {
            fclose(plan);
            return -1;
        }
        snprintf(path, sizeof(path), "/modules/%s", module);
        if (load_module(path)) {
            fclose(plan);
            return -1;
        }
    }
    int failed = ferror(plan);
    fclose(plan);
    return failed ? -1 : 0;
}

static int boolean_parameter(const char *path)
{
    char value[8];
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return -1;
    ssize_t size = read(fd, value, sizeof(value) - 1);
    close(fd);
    if (size <= 0)
        return -1;
    value[size] = 0;
    value[strcspn(value, "\r\n")] = 0;
    if (!strcmp(value, "Y") || !strcmp(value, "1"))
        return 1;
    if (!strcmp(value, "N") || !strcmp(value, "0"))
        return 0;
    return -1;
}

static int storage_parameters(void)
{
    int mcq = boolean_parameter("/sys/module/ufshcd_core/parameters/use_mcq_mode");
    return mcq == 0;
}

static int whole_scsi_disk(const char *name)
{
    return strlen(name) == 3 && name[0] == 's' && name[1] == 'd' && name[2] >= 'a' && name[2] <= 'z';
}

struct disk_capacity {
    uint64_t bytes;
    unsigned int block;
    int seen;
};

static int load_capacities(const char *path, struct disk_capacity expected[6])
{
    FILE *input = fopen(path, "r");
    if (!input)
        return -1;
    int count = 0;
    char guid[33], name[32], extra;
    unsigned long long bytes;
    unsigned int block;
    while (count < 6 && fscanf(input, "%32s %llu %u %31s", guid, &bytes, &block, name) == 4) {
        if (strlen(guid) != 32 || strspn(guid, "0123456789abcdef") != 32 ||
            !bytes || block != 4096 || bytes % block)
            break;
        expected[count++] = (struct disk_capacity){.bytes = bytes, .block = block};
    }
    int valid = count == 6 && fscanf(input, " %c", &extra) == EOF && !ferror(input);
    fclose(input);
    return valid ? 0 : -1;
}

static int match_capacity(struct disk_capacity expected[6], uint64_t bytes, unsigned int block)
{
    for (int i = 0; i < 6; i++) {
        if (!expected[i].seen && expected[i].bytes == bytes && expected[i].block == block) {
            expected[i].seen = 1;
            return 0;
        }
    }
    return -1;
}

static int enumerate_ufs(void)
{
    struct disk_capacity expected[6];
    if (load_capacities("/reference/disks.txt", expected))
        return -1;
    DIR *directory = opendir("/sys/class/block");
    if (!directory)
        return -1;
    struct dirent *entry;
    int disks = 0, matched = 0, errors = 0;
    while ((entry = readdir(directory))) {
        if (!whole_scsi_disk(entry->d_name))
            continue;
        disks++;
        char path[128], resolved[PATH_MAX];
        const char parent[] = "/sys/devices/platform/soc@0/1d84000.ufs/";
        snprintf(path, sizeof(path), "/sys/class/block/%.3s", entry->d_name);
        if (!realpath(path, resolved) || strncmp(resolved, parent, sizeof(parent) - 1)) {
            errors++;
            continue;
        }
        snprintf(path, sizeof(path), "/dev/%.3s", entry->d_name);
        int fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
        uint64_t bytes = 0;
        unsigned int block = 0;
        struct stat st;
        if (fd < 0 || fstat(fd, &st) || !S_ISBLK(st.st_mode) ||
            ioctl(fd, BLKGETSIZE64, &bytes) || ioctl(fd, BLKSSZGET, &block)) {
            if (fd >= 0)
                close(fd);
            errors++;
            continue;
        }
        close(fd);
        matched += match_capacity(expected, bytes, block) == 0;
    }
    closedir(directory);
    int all_bound = 1;
    for (size_t i = 0; i < sizeof(bindings) / sizeof(bindings[0]); i++)
        if (bindings[i].required && !bindings[i].confirmed)
            all_bound = 0;
    return disks == 6 && matched == 6 && !errors && all_bound ? 0 : -1;
}

static int disk_count(void)
{
    DIR *directory = opendir("/sys/class/block");
    struct dirent *entry;
    int count = 0;
    if (!directory)
        return 0;
    while ((entry = readdir(directory))) {
        if (whole_scsi_disk(entry->d_name))
            count++;
    }
    closedir(directory);
    return count;
}

int main(void)
{
    int ufs_enabled = access("/modules/ufs-qcom.ko", R_OK) == 0;
    if (ufs_enabled && !storage_parameters())
        return 1;
    int events = watch_bind_events();
    if (load_plan()) {
        if (events >= 0)
            close(events);
        return 1;
    }
    finish_binding_checks(events);
    if (!ufs_enabled)
        return 0;
    for (int i = 0; i < 30 && disk_count() < 6; i++)
        usleep(100000);
    if (enumerate_ufs())
        return 1;
    return 0;
}
