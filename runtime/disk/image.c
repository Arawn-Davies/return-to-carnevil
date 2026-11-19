/* image.c: the user's raw disk image, opened read-only, with every write diverted to an
 * overlay file, so the image itself is never changed.
 *
 * Overlay format (integers little-endian):
 * - bytes 0 to 511, the header:
 *     +0   8 bytes   magic "CEOVRLAY"
 *     +8   u32       format version, 1
 *     +12  u32       count of sector records that follow
 *     +16  u64       size of the raw image in bytes
 *     +24  32 bytes  SHA-256 of the raw image's first sector
 *     the rest zero;
 * - then the records, record i at byte 512 + 520 * i: a u32 absolute sector number (LBA), a
 *   zero u32, and the sector's 512 bytes as last written.
 * A sector's first write appends its record and then updates the count; later writes of it
 * rewrite the record in place, so each changed sector has exactly one record. Opening checks
 * the magic, the version, the size and first-sector hash against the raw image, and that the
 * file is exactly 512 + 520 * count bytes: shorter is truncated; longer is a record appended
 * by a write that stopped before the count was updated, and is refused as well rather than
 * guessed at.
 */
#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64

#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "disk.h"

#define OVL_MAGIC "CEOVRLAY"
#define OVL_VERSION 1u
#define OVL_HEADER 512u
#define OVL_RECORD (8u + IMG_SECTOR)
#define MAX_PARTITIONS ((IMG_SECTOR - 0x10u) / 12u) /* entries that fit in the header */

struct rec {
    uint32_t lba;
    uint32_t index; /* record number in the overlay */
};

static struct {
    int raw, ovl; /* file descriptors, -1 when closed */
    uint64_t size;
    uint32_t sectors;
    uint8_t header[IMG_SECTOR];
    struct rec *map; /* sorted by lba */
    uint32_t nmap, cap;
} img = {-1, -1, 0, 0, {0}, NULL, 0, 0};

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static uint64_t get64(const uint8_t *p)
{
    return (uint64_t)get32(p) | (uint64_t)get32(p + 4) << 32;
}

static void put64(uint8_t *p, uint64_t v)
{
    put32(p, (uint32_t)v);
    put32(p + 4, (uint32_t)(v >> 32));
}

static void hex(const uint8_t d[32], char out[65])
{
    for (int i = 0; i < 32; i++)
        snprintf(out + 2 * i, 3, "%02x", d[i]);
}

static int read_at(int fd, void *buf, size_t n, uint64_t off)
{
    uint8_t *p = buf;
    while (n) {
        ssize_t got = pread(fd, p, n, (off_t)off);
        if (got < 0 && errno == EINTR)
            continue;
        if (got <= 0)
            return -1;
        p += got;
        n -= (size_t)got;
        off += (uint64_t)got;
    }
    return 0;
}

static int write_at(int fd, const void *buf, size_t n, uint64_t off)
{
    const uint8_t *p = buf;
    while (n) {
        ssize_t put = pwrite(fd, p, n, (off_t)off);
        if (put < 0 && errno == EINTR)
            continue;
        if (put <= 0)
            return -1;
        p += put;
        n -= (size_t)put;
        off += (uint64_t)put;
    }
    return 0;
}

static void close_image(void)
{
    if (img.raw >= 0)
        close(img.raw);
    if (img.ovl >= 0)
        close(img.ovl);
    free(img.map);
    img.raw = img.ovl = -1;
    img.size = 0;
    img.sectors = 0;
    img.map = NULL;
    img.nmap = img.cap = 0;
}

static int fail(char *err, size_t errlen, const char *fmt, ...)
{
    if (err && errlen) {
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(err, errlen, fmt, ap);
        va_end(ap);
    }
    close_image();
    return -1;
}

/* The first map index whose lba is at least lba. */
static uint32_t lower_bound(uint32_t lba)
{
    uint32_t lo = 0, hi = img.nmap;
    while (lo < hi) {
        uint32_t mid = lo + (hi - lo) / 2;
        if (img.map[mid].lba < lba)
            lo = mid + 1;
        else
            hi = mid;
    }
    return lo;
}

static int map_insert(uint32_t lba, uint32_t index)
{
    uint32_t at = lower_bound(lba);
    if (at < img.nmap && img.map[at].lba == lba)
        return -1; /* two records for one sector */
    if (img.nmap == img.cap) {
        uint32_t cap = img.cap ? img.cap * 2 : 64;
        struct rec *m = realloc(img.map, (size_t)cap * sizeof *m);
        if (!m)
            return -1;
        img.map = m;
        img.cap = cap;
    }
    memmove(img.map + at + 1, img.map + at, (size_t)(img.nmap - at) * sizeof *img.map);
    img.map[at].lba = lba;
    img.map[at].index = index;
    img.nmap++;
    return 0;
}

static uint64_t record_offset(uint32_t index)
{
    return OVL_HEADER + (uint64_t)OVL_RECORD * index;
}

static int open_overlay(const char *path, const uint8_t first_hash[32], char *err, size_t errlen)
{
    struct stat rs, os;
    if (stat(path, &os) == 0 && fstat(img.raw, &rs) == 0 && rs.st_dev == os.st_dev &&
        rs.st_ino == os.st_ino)
        return fail(err, errlen, "overlay %s is the raw image itself", path);

    uint8_t h[OVL_HEADER];
    img.ovl = open(path, O_RDWR | O_CLOEXEC);
    if (img.ovl < 0 && errno == ENOENT) {
        img.ovl = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
        if (img.ovl < 0)
            return fail(err, errlen, "cannot create overlay %s: %s", path, strerror(errno));
        memset(h, 0, sizeof h);
        memcpy(h, OVL_MAGIC, 8);
        put32(h + 8, OVL_VERSION);
        put64(h + 16, img.size);
        memcpy(h + 24, first_hash, 32);
        if (write_at(img.ovl, h, sizeof h, 0) != 0)
            return fail(err, errlen, "cannot write overlay %s: %s", path, strerror(errno));
        return 0;
    }
    if (img.ovl < 0)
        return fail(err, errlen, "cannot open overlay %s: %s", path, strerror(errno));

    if (fstat(img.ovl, &os) != 0)
        return fail(err, errlen, "cannot stat overlay %s: %s", path, strerror(errno));
    uint64_t have = (uint64_t)os.st_size;
    if (have < OVL_HEADER)
        return fail(err, errlen, "overlay %s is truncated: %llu bytes, shorter than its header",
                    path, (unsigned long long)have);
    if (read_at(img.ovl, h, sizeof h, 0) != 0)
        return fail(err, errlen, "cannot read overlay %s: %s", path, strerror(errno));
    if (memcmp(h, OVL_MAGIC, 8) != 0)
        return fail(err, errlen, "%s is not an overlay (bad magic)", path);
    if (get32(h + 8) != OVL_VERSION)
        return fail(err, errlen, "overlay %s has format version %u, this program reads %u",
                    path, get32(h + 8), OVL_VERSION);
    if (get64(h + 16) != img.size || memcmp(h + 24, first_hash, 32) != 0) {
        char want[65], got[65];
        hex(h + 24, want);
        hex(first_hash, got);
        return fail(err, errlen,
                    "overlay %s was made for an image of %llu bytes whose first sector has "
                    "SHA-256 %s, not for this image of %llu bytes whose first sector has "
                    "SHA-256 %s", path, (unsigned long long)get64(h + 16), want,
                    (unsigned long long)img.size, got);
    }
    uint32_t count = get32(h + 12);
    uint64_t need = record_offset(count);
    if (have < need)
        return fail(err, errlen, "overlay %s is truncated: %llu bytes, its %u records need %llu",
                    path, (unsigned long long)have, count, (unsigned long long)need);
    if (have > need)
        return fail(err, errlen,
                    "overlay %s has %llu bytes after its %u records (a write that did not "
                    "finish)", path, (unsigned long long)(have - need), count);
    for (uint32_t i = 0; i < count; i++) {
        uint8_t lba[4];
        if (read_at(img.ovl, lba, 4, record_offset(i)) != 0)
            return fail(err, errlen, "cannot read overlay %s: %s", path, strerror(errno));
        if (get32(lba) >= img.sectors || map_insert(get32(lba), i) != 0)
            return fail(err, errlen, "overlay %s record %u names sector %u, outside the image "
                        "or already recorded", path, i, get32(lba));
    }
    return 0;
}

int img_open(const char *raw_path, const char *overlay_path, char *err, size_t errlen)
{
    fs_unmount();
    close_image();
    if (err && errlen)
        err[0] = 0;

    img.raw = open(raw_path, O_RDONLY | O_CLOEXEC);
    if (img.raw < 0)
        return fail(err, errlen, "cannot open raw image %s: %s", raw_path, strerror(errno));
    struct stat st;
    if (fstat(img.raw, &st) != 0)
        return fail(err, errlen, "cannot stat raw image %s: %s", raw_path, strerror(errno));
    if (!S_ISREG(st.st_mode))
        return fail(err, errlen, "raw image %s is not a regular file", raw_path);
    img.size = (uint64_t)st.st_size;
    if (img.size % IMG_SECTOR)
        return fail(err, errlen, "raw image %s is %llu bytes, not a whole number of %u-byte "
                    "sectors", raw_path, (unsigned long long)img.size, IMG_SECTOR);
    if (img.size < IMG_SECTOR)
        return fail(err, errlen, "raw image %s is %llu bytes, too small to hold the disk header",
                    raw_path, (unsigned long long)img.size);
    if (img.size / IMG_SECTOR > UINT32_MAX)
        return fail(err, errlen, "raw image %s has more than 2^32 sectors", raw_path);
    img.sectors = (uint32_t)(img.size / IMG_SECTOR);

    uint8_t first[IMG_SECTOR], hash[32];
    if (read_at(img.raw, first, sizeof first, 0) != 0)
        return fail(err, errlen, "cannot read raw image %s: %s", raw_path, strerror(errno));
    if (memcmp(first, "TRAP", 4) != 0)
        return fail(err, errlen, "raw image %s is not a CarnEvil disk (no TRAP magic)",
                    raw_path);
    if (get32(first + 4) == 0 || get32(first + 4) > MAX_PARTITIONS)
        return fail(err, errlen, "raw image %s has an implausible partition count %u", raw_path,
                    get32(first + 4));
    sha256(first, sizeof first, hash);

    if (overlay_path && open_overlay(overlay_path, hash, err, errlen) != 0)
        return -1;
    /* The header as the loader hands it out: sector 0 as now written, and at + 0xC the
     * disk's sector count, which the loader puts there (loader-services.md, slot 0x128). */
    if (img_read(0, img.header, 1) != 0)
        return fail(err, errlen, "cannot read sector 0 of %s", raw_path);
    put32(img.header + 0xC, img.sectors);
    return 0;
}

static int in_range(uint32_t lba, uint32_t count)
{
    return img.raw >= 0 && lba < img.sectors && (uint64_t)lba + count <= img.sectors;
}

int img_read(uint32_t lba, void *buf, uint32_t count)
{
    if (!in_range(lba, count))
        return -1;
    uint8_t *p = buf;
    uint32_t end = lba + count, at = lower_bound(lba);
    while (lba < end) {
        if (at < img.nmap && img.map[at].lba == lba) {
            if (read_at(img.ovl, p, IMG_SECTOR, record_offset(img.map[at].index) + 8) != 0)
                return -1;
            lba++;
            at++;
            p += IMG_SECTOR;
            continue;
        }
        /* A run of sectors the overlay does not hold, read from the raw image at once. */
        uint32_t stop = at < img.nmap && img.map[at].lba < end ? img.map[at].lba : end;
        size_t n = (size_t)(stop - lba) * IMG_SECTOR;
        if (read_at(img.raw, p, n, (uint64_t)lba * IMG_SECTOR) != 0)
            return -1;
        p += n;
        lba = stop;
    }
    return 0;
}

int img_write(uint32_t lba, const void *buf, uint32_t count)
{
    if (!in_range(lba, count) || img.ovl < 0)
        return -1;
    const uint8_t *p = buf;
    for (uint32_t i = 0; i < count; i++, p += IMG_SECTOR) {
        uint32_t s = lba + i, at = lower_bound(s);
        if (at < img.nmap && img.map[at].lba == s) {
            if (write_at(img.ovl, p, IMG_SECTOR, record_offset(img.map[at].index) + 8) != 0)
                return -1;
            continue;
        }
        uint8_t rec[OVL_RECORD], n[4];
        put32(rec, s);
        put32(rec + 4, 0);
        memcpy(rec + 8, p, IMG_SECTOR);
        put32(n, img.nmap + 1);
        if (write_at(img.ovl, rec, sizeof rec, record_offset(img.nmap)) != 0 ||
            write_at(img.ovl, n, 4, 12) != 0 || map_insert(s, img.nmap) != 0)
            return -1;
    }
    return 0;
}

uint32_t img_sectors(void)
{
    return img.sectors;
}

void img_flush(void)
{
    if (img.ovl >= 0)
        fsync(img.ovl);
}

const uint8_t *img_header(void)
{
    return img.raw >= 0 ? img.header : NULL;
}

int img_partition(int n, uint32_t *start, uint32_t *count)
{
    if (img.raw < 0 || n < 0 || (uint32_t)n > get32(img.header + 4) ||
        (uint32_t)n > MAX_PARTITIONS)
        return -1;
    if (n == 0) {
        *start = 0;
        *count = img.sectors;
        return 0;
    }
    const uint8_t *e = img.header + 0x10 + 12 * (n - 1);
    uint32_t s = get32(e + 4), c = get32(e + 8);
    if (c == 0 || (uint64_t)s + c > img.sectors)
        return -1;
    *start = s;
    *count = c;
    return 0;
}
