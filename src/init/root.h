#include <dirent.h>
#include <limits.h>
#include <linux/fs.h>

#ifndef UBUNTU_ROOT_UUID
#error UBUNTU_ROOT_UUID is required
#endif

static int root_uuid(unsigned char uuid[16])
{
    const char *text = UBUNTU_ROOT_UUID;
    unsigned int bytes = 0;
    for (size_t i = 0; text[i];) {
        if (text[i] == '-') {
            i++;
            continue;
        }
        unsigned int value;
        if (bytes == 16 || !text[i + 1] || sscanf(text + i, "%2x", &value) != 1)
            return -1;
        uuid[bytes++] = value;
        i += 2;
    }
    return bytes == 16 ? 0 : -1;
}

static int match_root(int fd, int require_clean)
{
    unsigned char super[1024], uuid[16];
    if (root_uuid(uuid)) {
        errno = EINVAL;
        return -1;
    }
    if (pread(fd, super, sizeof(super), 1024) != sizeof(super))
        return 0;
    if (super[56] != 0x53 || super[57] != 0xef || memcmp(super + 104, uuid, sizeof(uuid)))
        return 0;
    if ((super[58] & 2) ||
        (require_clean ? (super[96] & 4) || !(super[58] & 1) :
                         !(super[58] & 1) && !(super[96] & 4))) {
        errno = EUCLEAN;
        return -1;
    }
    return 1;
}

static int check_root_image(const char *path)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    struct stat st;
    if (fd < 0)
        return 1;
    int valid = !fstat(fd, &st) && S_ISREG(st.st_mode) && match_root(fd, 1) == 1;
    close(fd);
    printf("ROOT_IMAGE_UUID=%s MATCH=%s\n", UBUNTU_ROOT_UUID, valid ? "PASS" : "FAIL");
    return valid ? 0 : 1;
}

static int find_ufs_root(char device[PATH_MAX])
{
    DIR *blocks = opendir("/sys/class/block");
    struct dirent *entry;
    int matches = 0;
    int failure = 0;
    if (!blocks)
        return -1;
    while ((entry = readdir(blocks))) {
        char path[PATH_MAX], resolved[PATH_MAX];
        const char parent[] = "/sys/devices/platform/soc@0/1d84000.ufs/";
        snprintf(path, sizeof(path), "/sys/class/block/%s/partition", entry->d_name);
        if (access(path, F_OK))
            continue;
        snprintf(path, sizeof(path), "/sys/class/block/%s", entry->d_name);
        if (!realpath(path, resolved) || strncmp(resolved, parent, sizeof(parent) - 1))
            continue;
        snprintf(path, sizeof(path), "/dev/%s", entry->d_name);
        int fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
        struct stat st;
        int readonly = 0;
        if (fd < 0)
            continue;
        int match = 0;
        if (!fstat(fd, &st) && S_ISBLK(st.st_mode))
            match = match_root(fd, 0);
        if (match < 0) {
            failure = errno;
        } else if (match == 1) {
            if (ioctl(fd, BLKROGET, &readonly) || readonly != 0)
                failure = EROFS;
            matches++;
            snprintf(device, PATH_MAX, "%s", path);
        }
        close(fd);
    }
    closedir(blocks);
    if (failure || matches != 1) {
        errno = failure ? failure : matches ? EEXIST : ENOENT;
        return -1;
    }
    return 0;
}

static int protect_other_partitions(const char *device)
{
    char path[PATH_MAX], root_disk[PATH_MAX];
    snprintf(path, sizeof(path), "/sys/class/block/%s/..", strrchr(device, '/') + 1);
    if (!realpath(path, root_disk))
        return -1;
    DIR *blocks = opendir("/sys/class/block");
    if (!blocks)
        return -1;
    struct dirent *entry;
    int failure = 0, protected = 0;
    while ((entry = readdir(blocks))) {
        char resolved[PATH_MAX];
        const char parent[] = "/sys/devices/platform/soc@0/1d84000.ufs/";
        snprintf(path, sizeof(path), "/sys/class/block/%s", entry->d_name);
        if (!realpath(path, resolved) || strncmp(resolved, parent, sizeof(parent) - 1) ||
            !strcmp(resolved, root_disk))
            continue;
        snprintf(path, sizeof(path), "/dev/%s", entry->d_name);
        if (!strcmp(path, device))
            continue;
        int fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
        struct stat st;
        int readonly = 1, actual = 0;
        if (fd < 0 || fstat(fd, &st) || !S_ISBLK(st.st_mode) ||
            ioctl(fd, BLKROSET, &readonly) || ioctl(fd, BLKROGET, &actual) || actual != 1)
            failure = errno ? errno : EROFS;
        else
            protected++;
        if (fd >= 0)
            close(fd);
        if (failure)
            break;
    }
    closedir(blocks);
    if (failure || !protected) {
        errno = failure ? failure : ENODEV;
        return -1;
    }
    return 0;
}

static int prepare_ufs_root(void)
{
    char device[PATH_MAX];
    if (find_ufs_root(device))
        return -1;
    if (protect_other_partitions(device))
        return -1;
    directory("/ubuntu");
    if (mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) ||
        mount(device, "/ubuntu", "ext4", MS_NOATIME, "errors=remount-ro"))
        return -1;
    directory("/run/firmware");
    if (mount("/lib/firmware", "/run/firmware", NULL, MS_BIND, NULL) ||
        write_text("/sys/module/firmware_class/parameters/path", "/run/firmware"))
        return -1;
    const char *paths[] = {"/dev", "/proc", "/sys", "/run"};
    for (size_t i = 0; i < sizeof(paths) / sizeof(paths[0]); i++) {
        char target[64];
        snprintf(target, sizeof(target), "/ubuntu%s", paths[i]);
        directory(target);
        if (mount(paths[i], target, NULL, MS_BIND | MS_REC, NULL))
            return -1;
    }
    return write_text("/run/root-mode.txt", "WRITES=PERSISTENT\n");
}

static int prepare_ubuntu_root(void)
{
    int result = prepare_ufs_root();
    if (!result)
        result = write_text("/run/ubuntu-root.ready", "PASS\n");
    return result;
}
