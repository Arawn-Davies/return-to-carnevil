/* fs.c: the game's file system on the loader's partition 1 (docs/analysis/disk-format.md),
 * read and written through img_read and img_write.
 *
 * Layout used (partition-relative): home block at sector 2 (magic checked); chain table of
 * u16 entries from sector 67 to the start of block 3 (entry of block b at byte 2 * (b - 3);
 * 0 ends a chain, 1 is free); block b at sector 138 + 8 * b; the directory from block 3, 170
 * entries of 24 bytes per block and the next directory block's number at byte 4092 (0 ends
 * it); an entry whose first name word is zero is free.
 *
 * What follows the loader (loader-services.md and disk-format.md, read): unlink zeroes the
 * first name word and frees the chain; rename rewrites the 12-byte name without checking the
 * new one; a directory with no free entry is extended by a block taken from the chain table,
 * linked at byte 4092 of the last directory block and zeroed; read, write and seek move whole
 * words and return the loader's values. What does not (each marked "not the loader's" below):
 * - free blocks are taken first-fit from block 3; the loader searches from a hint that
 *   follows its last allocation (the same as first-fit just after mounting);
 * - a new entry takes the first free entry from block 3 after the name was looked up, so a
 *   name that exists is truncated in place; the loader takes the first entry that is free or
 *   holds the name, scanning from a hint, so it can make a second entry for the name;
 * - mode 2 (read/write) opens an existing file without truncating it and creates an absent
 *   one; the loader's create for this mode was not read for a duplicate check;
 * - the directory entry (length, first block, date/time) is written at every write, so the
 *   file system is consistent after each call; the loader writes it at close;
 * - the date/time word written is the constant FS_DATE << 16 | FS_TIME;
 * - a seek outside the file returns FS_ESEEK and leaves the position; the loader returns a
 *   stale local (inferred in loader-services.md).
 * Damaged input never indexes outside the chain table and never loops: open checks the
 * file's chain (data blocks, no block twice) up to its length, reads stop where the checked
 * part ends (the bytes so far, or FS_EOF), writes there return -1, and a directory link
 * outside the partition or back to a block already read makes the call fail with -1.
 * A handle keeps the length it saw, so two handles on one file do not see each other's
 * growth. Handles opened before img_open are closed by it (fs_unmount).
 */
#include <string.h>

#include "disk.h"

#define FS_PARTITION 1
#define HOME_SECTOR 2u
#define HOME_MAGIC 0x34011280u
#define FAT_SECTOR 67u
#define FAT_SECTORS 95u  /* sectors 67 to 161: the table ends where block 3 starts */
#define BASE_SECTOR 138u /* where block 0 would start (byte 70656) */
#define SPB 8u           /* sectors per block */
#define BLOCK 4096u
#define BLOCK_WORDS 1024u
#define FIRST_BLOCK 3u
#define MAX_BLOCKS (FIRST_BLOCK + FAT_SECTORS * IMG_SECTOR / 2u) /* the table's capacity */
#define END 0u
#define FREE 1u
#define DIR_SLOTS 170
#define DIR_ENTRY 24u
#define DIR_LINK 4092u
#define NAME_LEN 12
#define MAX_WORDS 0x3FFFFFFFu /* longest file whose size in bytes fits in 32 bits */
/* not the loader's: 1 September 1998, 00:00:00 in DOS form, for every entry written */
#define FS_DATE 0x2521u
#define FS_TIME 0x0000u

static struct {
    int mounted;
    uint32_t start;   /* first sector of the partition */
    uint32_t nblocks; /* blocks 3 to nblocks - 1 exist */
    uint8_t fat[FAT_SECTORS * IMG_SECTOR];
    uint8_t dirty[FAT_SECTORS];
} fs;

struct handle {
    int used;
    int mode;           /* the loader's flags: bit 0 read, bit 1 write */
    uint32_t dir_block; /* where the entry is */
    uint32_t slot;
    uint32_t first;     /* first block, 0 while the file has none */
    uint32_t length;    /* words */
    uint32_t pos;       /* words */
    uint32_t cur, cur_index; /* a block of the file and its index in the chain (cur 0: none) */
    uint32_t valid;     /* leading blocks of the chain checked at open: data blocks, no repeat */
};

static struct handle handles[FS_HANDLES];
static uint8_t dirbuf[BLOCK], databuf[BLOCK];
static uint8_t seen[(MAX_BLOCKS + 7) / 8]; /* one bit per block, for the walks that check */

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

void fs_unmount(void)
{
    fs.mounted = 0;
    memset(handles, 0, sizeof handles);
}

static int mount(void)
{
    uint32_t start, count;
    if (fs.mounted)
        return 0;
    if (img_partition(FS_PARTITION, &start, &count) != 0 || count < BASE_SECTOR + 4 * SPB)
        return -1;
    uint8_t home[IMG_SECTOR];
    if (img_read(start + HOME_SECTOR, home, 1) != 0 || get32(home) != HOME_MAGIC)
        return -1;
    if (img_read(start + FAT_SECTOR, fs.fat, FAT_SECTORS) != 0)
        return -1;
    uint32_t n = (count - BASE_SECTOR) / SPB;
    fs.start = start;
    fs.nblocks = n < MAX_BLOCKS ? n : MAX_BLOCKS;
    memset(fs.dirty, 0, sizeof fs.dirty);
    fs.mounted = 1;
    return 0;
}

/* --- chain table ------------------------------------------------------------------------- */

static int data_block(uint32_t b)
{
    return b >= FIRST_BLOCK && b < fs.nblocks;
}

/* The chain entry of b; END for a number that is not a data block, so a damaged link can
 * never index outside the table. */
static uint32_t fat_get(uint32_t b)
{
    if (!data_block(b))
        return END;
    uint32_t at = 2 * (b - FIRST_BLOCK);
    return (uint32_t)fs.fat[at] | (uint32_t)fs.fat[at + 1] << 8;
}

static void fat_set(uint32_t b, uint32_t v)
{
    uint32_t at = 2 * (b - FIRST_BLOCK);
    fs.fat[at] = (uint8_t)v;
    fs.fat[at + 1] = (uint8_t)(v >> 8);
    fs.dirty[at / IMG_SECTOR] = 1;
}

static int fat_sync(void)
{
    for (uint32_t i = 0; i < FAT_SECTORS; i++) {
        if (!fs.dirty[i])
            continue;
        if (img_write(fs.start + FAT_SECTOR + i, fs.fat + i * IMG_SECTOR, 1) != 0)
            return -1;
        fs.dirty[i] = 0;
    }
    return 0;
}

/* not the loader's: first fit from block 3. The block's entry becomes 0 (end of chain), as
 * the loader's allocator leaves it. 0 when the partition is full. */
static uint32_t allocate(void)
{
    for (uint32_t b = FIRST_BLOCK; b < fs.nblocks; b++) {
        if (fat_get(b) == FREE) {
            fat_set(b, END);
            return b;
        }
    }
    return 0;
}

static void free_chain(uint32_t b)
{
    for (uint32_t steps = 0; data_block(b) && steps < fs.nblocks; steps++) {
        uint32_t next = fat_get(b);
        if (next == FREE)
            break;
        fat_set(b, FREE);
        b = next;
    }
}

/* How many of the first `want` blocks of the chain from `first` are data blocks met for the
 * first time: the chain is good up to there, and a damaged link (outside the partition, or
 * back into the file) ends it. */
static uint32_t chain_valid(uint32_t first, uint32_t want)
{
    memset(seen, 0, sizeof seen);
    uint32_t b = first, n = 0;
    while (n < want && data_block(b) && !(seen[b / 8] >> (b % 8) & 1)) {
        seen[b / 8] |= (uint8_t)(1u << (b % 8));
        n++;
        b = fat_get(b);
    }
    return n;
}

static int read_block(uint32_t b, uint8_t *buf)
{
    return data_block(b) ? img_read(fs.start + BASE_SECTOR + b * SPB, buf, SPB) : -1;
}

static int write_block(uint32_t b, const uint8_t *buf)
{
    return data_block(b) ? img_write(fs.start + BASE_SECTOR + b * SPB, buf, SPB) : -1;
}

/* --- directory --------------------------------------------------------------------------- */

/* The upper-cased name, NUL padded to 12 bytes; -1 for an empty or too long name. */
static int make_key(const char *name, char key[NAME_LEN])
{
    size_t n = strlen(name);
    if (n == 0 || n > NAME_LEN)
        return -1;
    memset(key, 0, NAME_LEN);
    for (size_t i = 0; i < n; i++) {
        char ch = name[i];
        key[i] = ch >= 'a' && ch <= 'z' ? (char)(ch - 'a' + 'A') : ch;
    }
    return 0;
}

/* Entry name <-> key: each 4-byte group of the stored name is byte-reversed. */
static void entry_key(const uint8_t *e, char key[NAME_LEN])
{
    for (int i = 0; i < NAME_LEN; i++)
        key[i] = (char)e[(i & ~3) + 3 - (i & 3)];
    for (int i = 0, end = 0; i < NAME_LEN; i++) {
        end |= key[i] == 0;
        key[i] = end ? 0 : key[i] >= 'a' && key[i] <= 'z' ? (char)(key[i] - 'a' + 'A') : key[i];
    }
}

static void store_key(uint8_t *e, const char key[NAME_LEN])
{
    for (int i = 0; i < NAME_LEN; i++)
        e[(i & ~3) + 3 - (i & 3)] = (uint8_t)key[i];
}

static int is_free(const uint8_t *e)
{
    return (e[0] | e[1] | e[2] | e[3]) == 0;
}

struct where {
    uint32_t block, slot; /* block 0: none */
};

/* Scans the directory: the entry holding key (when key is not NULL), the first free entry,
 * and the last directory block. -1 when the directory is broken: a link outside the
 * partition, or back to a directory block already read. Leaves dirbuf undefined. */
static int dir_scan(const char *key, struct where *found, struct where *free_slot,
                    uint32_t *last)
{
    found->block = free_slot->block = 0;
    memset(seen, 0, sizeof seen);
    uint32_t b = FIRST_BLOCK;
    for (uint32_t steps = 0; steps < fs.nblocks; steps++) {
        if (!data_block(b) || seen[b / 8] >> (b % 8) & 1 || read_block(b, dirbuf) != 0)
            return -1;
        seen[b / 8] |= (uint8_t)(1u << (b % 8));
        for (uint32_t s = 0; s < DIR_SLOTS; s++) {
            const uint8_t *e = dirbuf + s * DIR_ENTRY;
            char k[NAME_LEN];
            if (is_free(e)) {
                if (!free_slot->block)
                    *free_slot = (struct where){b, s};
                continue;
            }
            entry_key(e, k);
            if (key && !found->block && memcmp(k, key, NAME_LEN) == 0)
                *found = (struct where){b, s};
        }
        *last = b;
        uint32_t next = get32(dirbuf + DIR_LINK);
        if (next == 0)
            return 0;
        b = next;
    }
    return -1;
}

/* 0 found, 1 absent (or not a valid name), -1 when the file system cannot be read. */
static int find(const char *name, struct where *found)
{
    char key[NAME_LEN];
    struct where free_slot;
    uint32_t last;
    if (mount() != 0)
        return -1;
    if (make_key(name, key) != 0)
        return 1;
    if (dir_scan(key, found, &free_slot, &last) != 0)
        return -1;
    return found->block ? 0 : 1;
}

/* Rewrites the entry of f from its handle (length, date/time, first block). */
static int entry_update(const struct handle *f)
{
    if (read_block(f->dir_block, dirbuf) != 0)
        return -1;
    uint8_t *e = dirbuf + f->slot * DIR_ENTRY;
    put32(e + 12, f->length);
    put32(e + 16, FS_DATE << 16 | FS_TIME);
    put32(e + 20, f->first);
    return write_block(f->dir_block, dirbuf);
}

/* A new empty entry for key: the first free entry, or slot 0 of a new directory block. */
static int create(const char *key, struct where *at)
{
    struct where found, free_slot;
    uint32_t last;
    if (dir_scan(NULL, &found, &free_slot, &last) != 0)
        return -1;
    if (free_slot.block) {
        *at = free_slot;
    } else {
        /* Write order (final review I5 a): the new block zeroed, then the chain table that
         * allocates it, then the link that makes it part of the directory. A kill before the
         * link leaves an allocated block nothing refers to (a leak); the link never reaches a
         * block the table still calls free, which the next allocation would hand out. */
        uint32_t b = allocate();
        if (!b)
            return -1;
        memset(dirbuf, 0, BLOCK);
        if (write_block(b, dirbuf) != 0 || fat_sync() != 0 || read_block(last, dirbuf) != 0)
            return -1;
        put32(dirbuf + DIR_LINK, b);
        if (write_block(last, dirbuf) != 0)
            return -1;
        *at = (struct where){b, 0};
    }
    if (read_block(at->block, dirbuf) != 0)
        return -1;
    uint8_t *e = dirbuf + at->slot * DIR_ENTRY;
    memset(e, 0, DIR_ENTRY);
    store_key(e, key);
    put32(e + 16, FS_DATE << 16 | FS_TIME);
    return write_block(at->block, dirbuf) == 0 && fat_sync() == 0 ? 0 : -1;
}

/* --- calls ------------------------------------------------------------------------------- */

int fs_find(const char *name, struct fs_stat *st)
{
    struct where w;
    if (find(name, &w) != 0 || read_block(w.block, dirbuf) != 0)
        return -1;
    const uint8_t *e = dirbuf + w.slot * DIR_ENTRY;
    uint32_t dt = get32(e + 16);
    st->size = get32(e + 12) * 4;
    st->attr = 0;
    st->time = (uint16_t)dt;
    st->date = (uint16_t)(dt >> 16);
    return 0;
}

static struct handle *handle(int h)
{
    return h >= 0 && h < FS_HANDLES && handles[h].used ? &handles[h] : NULL;
}

/* Modes 0 read, 1 write (created, or truncated when it exists), 2 read/write (opened, or
 * created when absent). Mode 3 and others: the loader's open leaves the descriptor free for
 * them, loader-services.md does not say more, and no measured open uses them (the game's use
 * 0, 1 and 2), so they are refused with -1. */
int fs_open(const char *name, int mode)
{
    static const int flags[3] = {1, 2, 3};
    char key[NAME_LEN];
    if (mode < 0 || mode > 2 || mount() != 0 || make_key(name, key) != 0)
        return -1;
    int h = 0;
    while (h < FS_HANDLES && handles[h].used)
        h++;
    if (h == FS_HANDLES)
        return -1;

    struct where w, free_slot;
    uint32_t last;
    if (dir_scan(key, &w, &free_slot, &last) != 0)
        return -1;
    if (!w.block && (mode == 0 || create(key, &w) != 0))
        return -1;
    if (read_block(w.block, dirbuf) != 0)
        return -1;
    const uint8_t *e = dirbuf + w.slot * DIR_ENTRY;
    struct handle f = {1, flags[mode], w.block, w.slot, get32(e + 20), get32(e + 12), 0, 0, 0,
                       0};
    if (f.length > MAX_WORDS)
        return -1;
    if (mode == 1 && (f.length || f.first)) {
        /* Freeing (final review I5 a): the directory entry first, which no longer refers to
         * the chain, then the chain table that releases it. A kill between leaves the blocks
         * allocated and unreferenced (a leak), never an entry reaching free blocks. */
        free_chain(f.first);
        f.first = 0;
        f.length = 0;
        if (entry_update(&f) != 0 || fat_sync() != 0)
            return -1;
    }
    /* A damaged entry or chain still opens, as the loader's does; reads and writes stop
     * where the checked part of the chain ends. */
    f.valid = chain_valid(f.first, (f.length + BLOCK_WORDS - 1) / BLOCK_WORDS);
    handles[h] = f;
    return h;
}

/* The block at index in f's chain, walking from the cached block when it is not past it;
 * 0 beyond the part of the chain checked at open (or extended by fs_write since). */
static uint32_t walk(struct handle *f, uint32_t index)
{
    if (index >= f->valid)
        return 0;
    uint32_t b = f->first, i = 0;
    if (f->cur && f->cur_index <= index) {
        b = f->cur;
        i = f->cur_index;
    }
    for (; i < index; i++) {
        b = fat_get(b);
        if (!data_block(b))
            return 0;
    }
    if (!data_block(b))
        return 0;
    f->cur = b;
    f->cur_index = index;
    return b;
}

int fs_read(int h, void *buf, uint32_t n)
{
    struct handle *f = handle(h);
    if (!f || !(f->mode & 1))
        return -1;
    if (n % 4)
        return 0;
    if (f->pos >= f->length)
        return FS_EOF;
    uint32_t want = n / 4, left = f->length - f->pos;
    uint32_t words = want < left ? want : left, done = 0;
    if (words > 0x1FFFFFFFu) /* so the byte count fits the int returned */
        words = 0x1FFFFFFFu;
    uint8_t *p = buf;
    while (done < words) {
        uint32_t off = f->pos % BLOCK_WORDS, chunk = BLOCK_WORDS - off;
        if (chunk > words - done)
            chunk = words - done;
        uint32_t b = walk(f, f->pos / BLOCK_WORDS);
        if (!b)
            break;
        if (chunk == BLOCK_WORDS) {
            if (read_block(b, p) != 0)
                break;
        } else {
            if (read_block(b, databuf) != 0)
                break;
            memcpy(p, databuf + off * 4, (size_t)chunk * 4);
        }
        p += (size_t)chunk * 4;
        done += chunk;
        f->pos += chunk;
    }
    /* A chain that ends early ends the read: the bytes so far, or FS_EOF for none. */
    return done ? (int)(done * 4) : FS_EOF;
}

int fs_write(int h, const void *buf, uint32_t n)
{
    struct handle *f = handle(h);
    if (!f || !(f->mode & 2))
        return -1;
    if (n % 4 || n == 0)
        return 0;
    uint32_t words = n / 4, done = 0;
    if (words > MAX_WORDS - f->pos || n > (uint32_t)0x7FFFFFFF)
        return -1;
    const uint8_t *p = buf;
    int ok = 1, changed = 0;
    while (done < words) {
        uint32_t index = f->pos / BLOCK_WORDS, off = f->pos % BLOCK_WORDS;
        uint32_t chunk = BLOCK_WORDS - off, have = (f->length + BLOCK_WORDS - 1) / BLOCK_WORDS;
        if (chunk > words - done)
            chunk = words - done;
        uint32_t b;
        int fresh = 0;
        if (index < have) {
            b = walk(f, index);
        } else {
            /* pos <= length, so this is the block just after the file's last one: always a
             * new block, since a link the chain already has past the length is not trusted
             * (it could lead back into the file); such a tail is left as it was, allocated
             * and unreachable. No healthy partition has one: on part0 none of the 1,523
             * entries has a chain longer than its length needs (the 3 ending on a block
             * boundary all end with 0; none has length 0), and the loader's create
             * (0x80008904, called from 0x80009B5C) gives a new file first block 0
             * (measured and read) */
            uint32_t prev = index ? walk(f, index - 1) : 0;
            fresh = 1;
            b = index && !prev ? 0 : allocate();
            if (b) {
                if (prev)
                    fat_set(prev, b);
                else
                    f->first = b;
                f->valid = index + 1;
                changed = 1;
            }
        }
        if (!b) {
            ok = 0;
            break;
        }
        if (chunk == BLOCK_WORDS) {
            if (write_block(b, p) != 0) {
                ok = 0;
                break;
            }
        } else {
            if (fresh)
                memset(databuf, 0, BLOCK);
            else if (read_block(b, databuf) != 0) {
                ok = 0;
                break;
            }
            memcpy(databuf + off * 4, p, (size_t)chunk * 4);
            if (write_block(b, databuf) != 0) {
                ok = 0;
                break;
            }
        }
        p += (size_t)chunk * 4;
        done += chunk;
        f->pos += chunk;
        if (f->pos > f->length)
            f->length = f->pos;
        changed = 1;
    }
    /* Allocating (final review I5 a): the data blocks, then the chain table that allocates
     * and links them, then the directory entry whose length and first block reach them. A
     * kill before the entry leaves the new blocks allocated past the length the entry gives
     * (unreachable: fs_write never follows a link past the length, above), a leak; the entry
     * never reaches a block the table still calls free. Nothing written and nothing
     * allocated: the entry is left as it was. */
    if (changed && (fat_sync() != 0 || entry_update(f) != 0))
        ok = 0;
    return ok ? (int)n : -1;
}

long fs_seek_words(int h, long off_bytes, int whence)
{
    struct handle *f = handle(h);
    if (!f || whence < 0 || whence > 2)
        return 0;
    /* the offset in words, rounded down as the loader's arithmetic shift does */
    long q = off_bytes / 4 - (off_bytes % 4 < 0);
    int64_t to = whence == 0 ? q : whence == 1 ? (int64_t)f->pos + q : (int64_t)f->length;
    if (to < 0 || to > f->length)
        return FS_ESEEK;
    f->pos = (uint32_t)to;
    return (long)(to * 4);
}

int fs_close(int h)
{
    struct handle *f = handle(h);
    if (!f)
        return -1;
    f->used = 0;
    return fat_sync() == 0 ? 0 : -1;
}

/* 0 also when the name is absent, as the loader's unlink. */
int fs_unlink(const char *name)
{
    struct where w;
    int r = find(name, &w);
    if (r != 0)
        return r > 0 ? 0 : -1;
    if (read_block(w.block, dirbuf) != 0)
        return -1;
    uint8_t *e = dirbuf + w.slot * DIR_ENTRY;
    uint32_t first = get32(e + 20);
    /* Freeing (final review I5 a): the entry first, then the chain table; a kill between
     * leaks the file's blocks, and no entry is left reaching free ones. */
    memset(e, 0, 4);
    if (write_block(w.block, dirbuf) != 0)
        return -1;
    free_chain(first);
    return fat_sync();
}

int fs_rename(const char *a, const char *b)
{
    struct where w;
    char key[NAME_LEN];
    int r = find(a, &w);
    if (r != 0)
        return r > 0 ? FS_ENOENT : -1;
    if (make_key(b, key) != 0 || read_block(w.block, dirbuf) != 0)
        return -1;
    store_key(dirbuf + w.slot * DIR_ENTRY, key);
    return write_block(w.block, dirbuf);
}
