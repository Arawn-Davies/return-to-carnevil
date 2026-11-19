/* disk.h: the game's hard disk, from the user's raw image, and the file system on it.
 *
 * Three layers, each over the one before, all plain C over host buffers:
 * - sha256: SHA-256 (sha256.c), for the overlay header and for checking GAME.EXE;
 * - img_*: the raw image opened read-only, every write diverted to an overlay file
 *   (image.c, whose top comment gives the overlay's format);
 * - fs_*: the file system of docs/analysis/disk-format.md on the loader's partition 1,
 *   the disk's first partition (fs.c).
 * One image is open at a time. Nothing here prints or exits; errors are return values.
 */
#ifndef CARNEVIL_DISK_H
#define CARNEVIL_DISK_H

#include <stddef.h>
#include <stdint.h>

/* --- SHA-256 ----------------------------------------------------------------------------- */

typedef struct {
    uint32_t h[8];
    uint64_t bytes;  /* message length so far */
    uint8_t buf[64];
    uint32_t used;   /* bytes in buf */
} sha256_ctx;

void sha256_init(sha256_ctx *s);
void sha256_update(sha256_ctx *s, const void *data, size_t n);
void sha256_final(sha256_ctx *s, uint8_t out[32]);
void sha256(const void *data, size_t n, uint8_t out[32]);

/* --- the image --------------------------------------------------------------------------- */

#define IMG_SECTOR 512u

/* Opens raw_path read-only and overlay_path read-write, creating the overlay if it is absent
 * (overlay_path NULL: no overlay, and img_write fails). Closes any image opened before, and
 * with it the file system's handles. 0, or -1 with a message in err. */
int img_open(const char *raw_path, const char *overlay_path, char *err, size_t errlen);
/* count sectors from absolute sector lba: 0, or -1 when the range is outside the disk or the
 * host read or write fails. Reads see the overlay's sectors over the raw image's. */
int img_read(uint32_t lba, void *buf, uint32_t count);
int img_write(uint32_t lba, const void *buf, uint32_t count);
uint32_t img_sectors(void);
/* Pushes the overlay's writes to the host disk (fsync). */
void img_flush(void);
/* The disk header (sector 0) as the loader's partition-table slot hands it out: with the
 * word at + 0xC replaced by the disk's sector count (loader-services.md, slot 0x128). NULL
 * when no image is open. */
const uint8_t *img_header(void);
/* Partition n in the loader's numbering (0 the whole disk, 1 the disk's first partition):
 * its first sector and size in sectors. 0, or -1 when n is not in the table or the
 * partition does not lie inside the image. */
int img_partition(int n, uint32_t *start, uint32_t *count);

/* --- the file system --------------------------------------------------------------------- */

#define FS_EOF (-9298)    /* 0xFFFFDBAE: read at end of file, or a broken chain */
#define FS_ENOENT (-9289) /* 0xFFFFDBB7: rename of an absent name */
#define FS_ESEEK (-9297)  /* 0xFFFFDBAF: seek outside the file */
#define FS_HANDLES 16

struct fs_stat {
    uint32_t size; /* bytes */
    uint32_t attr; /* always 0: the directory entry has no attribute */
    uint16_t time; /* DOS time and date halves of the entry's date/time word */
    uint16_t date;
};

/* Names are at most 12 characters and are compared upper-cased. */
int fs_find(const char *name, struct fs_stat *st);       /* 0, or -1 when absent */
int fs_open(const char *name, int mode);                 /* handle, or -1 */
int fs_read(int h, void *buf, uint32_t n);
int fs_write(int h, const void *buf, uint32_t n);
long fs_seek_words(int h, long off_bytes, int whence);
int fs_close(int h);
int fs_unlink(const char *name);
int fs_rename(const char *a, const char *b);
/* Closes every handle and drops the cached chain table; img_open calls it. */
void fs_unmount(void);

#endif
