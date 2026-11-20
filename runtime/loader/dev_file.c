/* dev_file.c: the file driver (any name the name table does not match, section 2's `file`)
 * on the disk library's file system (runtime/disk/disk.h), and the file slots that take a
 * name or a file descriptor: find-first, unlink, rename and lseek (section 1).
 *
 * The disk library already returns the loader's values (whole-word reads and writes, the
 * end-of-file and rename errors); this file moves the bytes between guest memory and the
 * host through mem_r8 and mem_w8, in chunks.
 */
#include <string.h>

#include "../disk/disk.h"
#include "loader.h"

#define NAME_MAX_LEN 12 /* the directory's 8.3 form, NNNNNNNN.EEE */
#define CHUNK 16384u    /* a multiple of 4, so whole-word transfers stay whole */

/* A file name argument: the string, refused when longer than the directory can hold (how
 * the loader cuts a longer name down to the 8.3 form is not documented). */
static void file_name(uint32_t addr, char s[LD_STRING_MAX], const char *what)
{
    ld_string(addr, s);
    if (strlen(s) > NAME_MAX_LEN)
        ld_stop("%s(\"%s\"): a name longer than %d characters, whose 8.3 form is not "
                "documented", what, s, NAME_MAX_LEN);
}

/* open: the name is upper-cased in place in guest memory (0x80008A20, section 1); modes 0,
 * 1 and 2 are the file system's read, write (truncate or create) and read/write */
static int file_open(int fd, uint32_t name, int mode)
{
    char s[LD_STRING_MAX];
    int h;
    file_name(name, s, "open");
    for (uint32_t i = 0; s[i]; i++) {
        if (s[i] >= 'a' && s[i] <= 'z') {
            s[i] = (char)(s[i] - 'a' + 'A');
            mem_w8(ld.ram, ld.c, name + i, (uint8_t)s[i]);
        }
    }
    h = fs_open(s, mode);
    if (h < 0)
        return -1;
    ld_desc((uint32_t)fd)->fh = h;
    return 0;
}

/* close: section 1, slot 0x018 */
static int file_close(int fd)
{
    struct ldesc *d = ld_desc((uint32_t)fd);
    int r = fs_close(d->fh);
    d->fh = -1;
    return r;
}

/* read: section 2, `file`: 0 when n is not a multiple of 4, the bytes read, or 0xFFFFDBAE
 * at end of file */
static int file_read(int fd, uint32_t buf, uint32_t n)
{
    static uint8_t chunk[CHUNK];
    int h = ld_desc((uint32_t)fd)->fh;
    uint32_t done = 0;
    if (n % 4 || n == 0)
        return fs_read(h, chunk, n);
    while (done < n) {
        uint32_t want = n - done < CHUNK ? n - done : CHUNK;
        int r = fs_read(h, chunk, want);
        if (r <= 0)
            return done ? (int)done : r;
        for (uint32_t i = 0; i < (uint32_t)r; i++)
            mem_w8(ld.ram, ld.c, buf + done + i, chunk[i]);
        done += (uint32_t)r;
        if ((uint32_t)r < want)
            break;
    }
    return (int)done;
}

/* write: section 2, `file`: 0 when n is not a multiple of 4, n, or -1 */
static int file_write(int fd, uint32_t buf, uint32_t n)
{
    static uint8_t chunk[CHUNK];
    int h = ld_desc((uint32_t)fd)->fh;
    uint32_t done = 0;
    if (n % 4 || n == 0)
        return fs_write(h, chunk, n);
    while (done < n) {
        uint32_t want = n - done < CHUNK ? n - done : CHUNK;
        for (uint32_t i = 0; i < want; i++)
            chunk[i] = (uint8_t)mem_r8(ld.ram, ld.c, buf + done + i);
        if (fs_write(h, chunk, want) != (int)want)
            return -1;
        done += want;
    }
    return (int)n;
}

/* The file driver's ioctls (0x99F6 to 0x99FA) are the loader's own; the game issues none
 * (section 2), so there is no ioctl routine and every command stops. */
const struct ldev ld_file = {"file:", file_open, file_close, file_read, file_write, NULL};

/* 0x038 findfirst: section 1; an exact name only (the game asks for game.env and for
 * files it then opens); the result fields at +21 to +42 of find_t. Bytes 0 to 20, the
 * loader's private copy of the pattern and its mask, are not written: the game reads only
 * the size at +26 (section 1) */
void ld_slot_findfirst(cpu_t *c)
{
    char s[LD_STRING_MAX];
    struct fs_stat st;
    uint32_t buf = c->r[6];
    file_name(c->r[4], s, "findfirst");
    if (strpbrk(s, "*?"))
        ld_stop("findfirst(\"%s\"): a wildcard pattern; the game asks for exact names only", s);
    if (fs_find(s, &st) != 0) {
        c->r[2] = 0xFFFFFFFFu;
        return;
    }
    mem_w8(ld.ram, c, buf + 21, st.attr);
    mem_w8(ld.ram, c, buf + 22, st.time & 0xFFu);
    mem_w8(ld.ram, c, buf + 23, st.time >> 8);
    mem_w8(ld.ram, c, buf + 24, st.date & 0xFFu);
    mem_w8(ld.ram, c, buf + 25, st.date >> 8);
    for (uint32_t i = 0; i < 4; i++)
        mem_w8(ld.ram, c, buf + 26 + i, st.size >> (8 * i) & 0xFFu);
    for (uint32_t i = 0;; i++) {
        char ch = s[i] >= 'a' && s[i] <= 'z' ? (char)(s[i] - 'a' + 'A') : s[i];
        mem_w8(ld.ram, c, buf + 30 + i, (uint8_t)ch);
        if (!ch)
            break;
    }
    c->r[2] = 0;
}

/* 0x190 unlink: section 1; 0 also for an absent name */
void ld_slot_unlink(cpu_t *c)
{
    char s[LD_STRING_MAX];
    file_name(c->r[4], s, "unlink");
    c->r[2] = (uint32_t)fs_unlink(s);
}

/* 0x1B8 rename: section 1; 0xFFFFDBB7 when old is absent */
void ld_slot_rename(cpu_t *c)
{
    char a[LD_STRING_MAX], b[LD_STRING_MAX];
    file_name(c->r[4], a, "rename");
    file_name(c->r[5], b, "rename");
    c->r[2] = (uint32_t)fs_rename(a, b);
}

/* 0x1C0 lseek: section 1, positions in words; a bad fd or whence above 2 gives 0 */
void ld_slot_lseek(cpu_t *c)
{
    uint32_t fd = c->r[4], whence = c->r[6];
    int32_t off = (int32_t)c->r[5];
    struct ldesc *d = ld_desc(fd);
    long r;
    if (!d || whence > 2) {
        c->r[2] = 0;
        return;
    }
    /* the loader seeks the descriptor's own file block whatever it holds: a device's
     * unused one, or a closed file's stale one; what that returns is not documented */
    if (!d->flags)
        ld_stop("lseek(%u, %d, %u): the descriptor is not open", (unsigned)fd, (int)off,
                (unsigned)whence);
    if (d->dev != &ld_file)
        ld_stop("lseek(%u, %d, %u) on %s, not a file", (unsigned)fd, (int)off,
                (unsigned)whence, d->dev->prefix);
    r = fs_seek_words(d->fh, off, (int)whence);
    if (r == FS_ESEEK)
        ld_stop("lseek(%u, %d, %u): a position outside the file, for which the loader "
                "returns a stale local (section 1, inferred)", (unsigned)fd, (int)off,
                (unsigned)whence);
    c->r[2] = (uint32_t)r;
}
