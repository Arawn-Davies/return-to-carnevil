/* diskslots.c: the disk slots that are not about files: 0x118 select partition, 0x128 the
 * partition table, 0x158 raw sector read, 0x1D8 the completion callback and 0x220 the
 * process hooks.
 *
 * docs/analysis/loader-services.md section 1 (each slot), section 4 ("Transfer mechanism",
 * "The movie player", "Stand-in recommendation for the disk"), section 8, and open questions
 * 5 and 6 (measured). Transfers complete synchronously from the image (runtime/disk/disk.h);
 * the completion callback or the wake hook, which on the board run at interrupt level from
 * the IDE completion path, are called through rt_call before the slot returns, with the
 * argument in a0 and ra = LD_DISK_CALLBACK_RA, on the caller's stack (an interrupt taken
 * inside them uses the exception stack, so they must not be on it), and with every register
 * of the slot's caller restored afterwards. Open question 5 measured that this is harmless
 * provided the slot never calls wait(), which it never does; 0x1D8 and 0x220 then never
 * wait either. The selected partition (0x118) is kept but changes nothing yet: the file
 * system reads partition 1, the only one the game selects.
 *
 * The partition table copy lives at LD_PART_TABLE, 0x800B7160: the address the board's
 * loader hands out (section 1, slot 0x128), in the loader's BSS. It is free because the
 * loader code that used it (these slots and the IDE driver) is replaced here, and no other
 * stand-in uses 0x800B7160 to 0x800B735F (the argument block is at 0x800B6E30, the exception
 * frame at 0x80014328, the exception stack below 0x800C1400); keeping the board's address
 * keeps the pointer the game stores equal to MAME's.
 */
#include <string.h>

#include "../disk/disk.h"
#include "loader.h"
#include "machine.h"

/* the loader's partition numbering (section 1, slot 0x118): 1 is selected at the hand-off */
#define PARTITION_AT_HANDOFF 1u

static struct {
    uint32_t partition;          /* 0x118's n: 0 the whole disk, 1 the disk's first */
    uint32_t callback;           /* 0x1D8's cb, 0 for none */
    uint32_t proc, wait, wake;   /* 0x220's arguments */
} dk;

void ld_disk_reset(void)
{
    dk.partition = PARTITION_AT_HANDOFF;
    dk.callback = dk.proc = dk.wait = dk.wake = 0;
}

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

/* The header as slot 0x128 hands it out, and its partition count; no image stops. */
static const uint8_t *header(uint32_t *count)
{
    const uint8_t *h = img_header();
    if (!h)
        ld_stop("no disk image is open");
    *count = get32(h + 4);
    return h;
}

/* fn(arg) as the IDE completion path calls it, keeping every register of the slot's caller
 * (the loader's dispatcher restores them after an interrupt). */
static void call_hook(cpu_t *c, uint32_t fn, uint32_t arg)
{
    uint32_t r[32], hi = c->hi, lo = c->lo;
    memcpy(r, c->r, sizeof r);
    c->r[4] = arg;
    c->r[31] = LD_DISK_CALLBACK_RA;
    rt_call(ld.ram, c, fn);
    memcpy(c->r, r, sizeof r);
    c->hi = hi, c->lo = lo;
}

/* 0x118 select partition(n): section 8; n above the table's count word returns 2 */
void ld_slot_disk_select(cpu_t *c)
{
    uint32_t count, n = c->r[4];
    header(&count);
    if (n > count) {
        c->r[2] = 2;
        return;
    }
    dk.partition = n;
    c->r[2] = 0;
}

/* 0x128 partition table(): section 8; the magic, the count and the count + 1 entries of 12
 * bytes from + 8 (entry 0, the whole disk, has the total sector count at + 0xC) */
void ld_slot_disk_table(cpu_t *c)
{
    uint32_t count;
    const uint8_t *h = header(&count);
    uint32_t size = 8 + 12 * (count + 1);
    if (size > IMG_SECTOR)
        ld_stop("the disk header's partition count %u does not fit its sector",
                (unsigned)count);
    for (uint32_t i = 0; i < size; i++)
        mem_w8(ld.ram, c, LD_PART_TABLE + i, h[i]);
    c->r[2] = LD_PART_TABLE;
}

/* 0x158 disk_read_sectors(lba, buf, count): section 8 */
void ld_slot_disk_read(cpu_t *c)
{
    uint32_t lba = c->r[4], buf = c->r[5], count = c->r[6], total, parts;
    uint8_t sector[IMG_SECTOR];
    int failed = 0;
    header(&parts);
    total = img_sectors();
    if ((int32_t)count <= 0)
        ld_stop("count %d: the game reads 1 or more sectors; what the loader does with this "
                "is not documented", (int)(int32_t)count);
    /* out of range, as the loader tests it: lba or lba + count at or beyond the drive size;
     * no transfer, the callback (if any) gets the count */
    if (lba >= total || (uint64_t)lba + count >= total) {
        if (dk.callback)
            call_hook(c, dk.callback, count);
        c->r[2] = count;
        return;
    }
    if ((uint64_t)buf + (uint64_t)count * IMG_SECTOR > 0x100000000ull)
        ld_stop("the buffer 0x%08X of %u sectors wraps the address space", (unsigned)buf,
                (unsigned)count);
    for (uint32_t i = 0; i < count && !failed; i++) {
        uint32_t at = buf + (uint32_t)((uint64_t)i * IMG_SECTOR);
        if (img_read(lba + i, sector, 1) != 0) {
            failed = 1;
            break;
        }
        for (uint32_t j = 0; j < IMG_SECTOR; j++)
            mem_w8(ld.ram, c, at + j, sector[j]);
    }
    if (dk.callback) {
        call_hook(c, dk.callback, failed ? 1 : 0);
        c->r[2] = 0;
        return;
    }
    if (failed)
        ld_stop("host read error at sector %u..%u with no completion callback: what the "
                "loader returns then (after its retries) is not documented", (unsigned)lba,
                (unsigned)(lba + count - 1));
    if (dk.proc) {
        uint32_t cookie = mem_r32(ld.ram, c, dk.proc);
        if (cookie) {
            if (!dk.wake)
                ld_stop("a disk process hook with cookie 0x%08X but no wake routine",
                        (unsigned)cookie);
            call_hook(c, dk.wake, cookie);
            c->r[2] = 0;
            return;
        }
    }
    c->r[2] = count;
}

/* 0x1D8 disk_set_callback(cb): section 8, store; the queue is always empty, so no wait; no
 * return value */
void ld_slot_disk_callback(cpu_t *c)
{
    dk.callback = c->r[4];
}

/* 0x220 install_disk_info(proc, wait, wake): section 8, store the three words; never wait;
 * no return value */
void ld_slot_disk_info(cpu_t *c)
{
    dk.proc = c->r[4];
    dk.wait = c->r[5];
    dk.wake = c->r[6];
}
