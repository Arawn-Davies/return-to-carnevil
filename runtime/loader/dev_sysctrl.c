/* dev_sysctrl.c: `pci:`, the loader's sysctrl driver (fd 6 once the game opens it): ioctls
 * 0x7300 and 0x7301 on the GT64010's registers, and the GT64010's DMA channel 0 behind them.
 *
 * docs/analysis/loader-services.md section 3 (`pci:`), section 8 (pci 0x7300 / 0x7301) and
 * open question 8 (measured, mame/m2-facts/dma0.txt): once per scheduler pass the game writes
 * offset 0x840 (channel 0 control) = 0x0DC0, 0x830 (next record pointer) = the chain's
 * physical address, 0x840 = 0x2DC0, 0x840 = 0x1DC0 (bit 0x1000, the channel enable); it never
 * issues 0x7300. A chain is a list of 16-byte records {byte count, source, destination, next}
 * at physical addresses in RAM, ending with next = 0; the completion is GT64010 cause bit 4.
 *
 * Here the enable write walks the chain at once, checking every record, and then delivers
 * it: each record's data is copied, in order, through the guest-memory helpers (so a
 * destination in the Voodoo's range reaches bus_write and the card), as words
 * (mem_r32/mem_w32) for records of whole aligned words, the size of every measured record,
 * otherwise byte by byte. The card can hold the channel (hw.h, voodoo_holding; from MAME's
 * model: gt64xxx.cpp's perform_dma moves no data while the card asserts pci_stall): before
 * each word the channel asks, and while held it waits on the virtual clock, polling the card
 * at the next time the hold may end, and goes on from that word; the CPU runs meanwhile.
 * Cause bit 4 is raised dma0_delay_ns of the chain after the enable write, the measured
 * completion time, or at once when the last word goes in later than that (a held chain).
 * Only the measured requests are served: the three control values, the next-record pointer
 * and the enable; a record outside RAM, a chain that loops or runs past CHAIN_MAX records, a
 * start while a chain is still being delivered or its cause bit 4 is pending (MAME's model
 * ignores an enable of an active channel; the game never makes one), 0x7300 and every other
 * offset or value stop.
 *
 * open, close: the shared return-0 routine (the sysctrl init flag is set at the hand-off,
 * section 2); read and write are not documented (NULL: they stop).
 */
#include <string.h>

#include "../hw/hw.h"
#include "loader.h"
#include "machine.h"

#define GT_DMA0_NEXT 0x830u
#define GT_DMA0_CTRL 0x840u
#define CTRL_ENABLE 0x1000u
#define CHAIN_MAX 4096u
#define RECORD 16u
#define DMA0_CAUSE_BIT 4
#define LFB_FIRST 0x08400000u /* the Voodoo's frame buffer, physical (hardware-usage.md) */
#define LFB_END 0x08800000u

/* The delay model (the fit is described at dma0_delay_ns). In femtoseconds. */
#define DELAY_BASE_FS 2663890085ull
#define DELAY_RECORD_FS 1939399217ull
#define DELAY_LFB_BYTE_FS 16946022ull

static struct {
    int next_set;     /* 0x830 written since loader_reset */
    uint32_t next;    /* 0x830: the next record pointer */
    uint32_t ctrl;    /* 0x840 as last written */
    int pending;      /* a chain started whose cause bit 4 has not been raised yet */
    /* the chain being delivered: its records (count, source, destination as kseg1
     * addresses), the next to copy and the byte offset in it, when bit 4 is due */
    uint8_t *ram;
    cpu_t *c;
    uint32_t n, at, off, n_other;
    uint64_t due, start, lfb_bytes;
    uint64_t last;    /* when the delivery last went on: the start, or the poll that did */
    uint64_t raise;   /* when cause bit 4 is due: the later of due and the last word */
} gt;

static void (*dma_hook)(uint32_t records, uint32_t n_other, uint64_t lfb_bytes,
                        uint64_t delay_ns);

void loader_set_dma_hook(void (*fn)(uint32_t records, uint32_t n_other, uint64_t lfb_bytes,
                                    uint64_t delay_ns))
{
    dma_hook = fn;
}

static struct {
    uint32_t count, src, dst;
} chain[CHAIN_MAX];

/* one bit per word of RAM: the records of the chain being walked, to see a loop */
static uint8_t visited[RAM_SIZE / 4 / 8];

static void dma0_done(void *arg);
static void dma0_resume(void *arg);

void ld_sysctrl_reset(void)
{
    vclock_cancel(dma0_done, NULL);
    vclock_cancel(dma0_resume, NULL);
    memset(&gt, 0, sizeof gt);
    memset(visited, 0, sizeof visited);
    voodoo_dma_writer(0);
    dma_hook = NULL;
}

/* dma0_delay_ns: fitted to dma0.txt (open question 8, measured): the time from the enable
 * write to the game's cause-bit-4 handler depends on the chain only, and for the eight
 * chain shapes MAME ran (28 to 576 records, 3,580 starts) it is
 *     2,663.890 ns + 1,939.399 ns per record not writing the frame buffer
 *                  + 16.946 ns per byte of the records that do (0x640-byte records),
 * a least-squares fit to the shapes' times (about 400 cycles, 291 cycles a record and 2.54
 * cycles a byte at 150 MHz; hardware-usage.md, "Measured values the runtime answers with").
 * Its largest error on any measured start is 5 ns, 0.002% of that chain's 0.25 ms (the
 * shapes' own times spread by up to 7 ns). 64-bit throughout. */
uint64_t dma0_delay_ns(uint32_t n_other, uint64_t lfb_bytes)
{
    return (DELAY_BASE_FS + DELAY_RECORD_FS * n_other + DELAY_LFB_BYTE_FS * lfb_bytes) /
           1000000u;
}

static void dma0_done(void *arg)
{
    (void)arg;
    gt.pending = 0;
    gt_cause_raise(DMA0_CAUSE_BIT);
    if (dma_hook)
        dma_hook(gt.n, gt.n_other, gt.lfb_bytes, gt.raise - gt.start);
}

/* A physical address as a kseg1 address for the guest-memory helpers; RAM and the devices
 * below 512 MB only. */
static uint32_t guest(uint32_t phys, const char *what, uint32_t rec)
{
    if (phys >= 0x20000000u)
        ld_stop("DMA channel 0: record 0x%08X: %s 0x%08X is not a physical address the "
                "runtime maps", (unsigned)rec, what, (unsigned)phys);
    return 0xA0000000u | phys;
}

static uint32_t ram_word(uint32_t phys)
{
    return mem_r32(ld.ram, ld.c, 0xA0000000u | phys);
}

/* Copies the chain from where it stopped until it ends or the card holds the channel; at the
 * end, cause bit 4 at its due time (or now, if that has passed). */
static void dma0_deliver(void)
{
    uint64_t now;
    voodoo_dma_writer(1);
    while (gt.at < gt.n) {
        uint32_t count = chain[gt.at].count, s = chain[gt.at].src, d = chain[gt.at].dst;
        int words = ((count | s | d) & 3u) == 0;
        while (gt.off < count) {
            if (voodoo_holding()) {
                voodoo_dma_writer(0);
                gt.last = voodoo_hold_check_ns();
                vclock_at(gt.last, dma0_resume, NULL);
                return;
            }
            if (words) {
                mem_w32(gt.ram, gt.c, d + gt.off, mem_r32(gt.ram, gt.c, s + gt.off));
                gt.off += 4;
            } else {
                mem_w8(gt.ram, gt.c, d + gt.off, mem_r8(gt.ram, gt.c, s + gt.off));
                gt.off++;
            }
        }
        gt.at++;
        gt.off = 0;
    }
    voodoo_dma_writer(0);
    now = vclock_now(gt.c);
    gt.raise = gt.due > gt.last ? gt.due : gt.last;
    if (gt.due > now)
        vclock_at(gt.due, dma0_done, NULL);
    else
        dma0_done(NULL);
}

/* A held chain's poll: the card brought up to now; still held, the next poll; else on. */
static void dma0_resume(void *arg)
{
    (void)arg;
    voodoo_poll(gt.c);
    if (voodoo_holding())
        vclock_at(gt.last = voodoo_hold_check_ns(), dma0_resume, NULL);
    else
        dma0_deliver();
}

/* The enable write: walk and check the chain from gt.next, then deliver it. */
static void dma0_start(void)
{
    uint32_t rec = gt.next, n = 0, n_other = 0, walked[CHAIN_MAX];
    uint64_t lfb_bytes = 0;
    if (gt.pending)
        ld_stop("DMA channel 0 enabled while the chain started before is still pending (the "
                "game waits for cause bit 4 first; what the GT64010 does then is not measured)");
    if (!gt.next_set || rec == 0)
        ld_stop("DMA channel 0 enabled with next record 0x%08X: no chain (record 0x%08X)",
                (unsigned)rec, (unsigned)rec);
    while (rec != 0) {
        if ((rec & 3u) || rec >= RAM_SIZE || RAM_SIZE - rec < RECORD)
            ld_stop("DMA channel 0: record 0x%08X is outside RAM or not word aligned",
                    (unsigned)rec);
        if (visited[rec / 32] >> (rec / 4 % 8) & 1)
            ld_stop("DMA channel 0: the chain loops: record 0x%08X is reached twice",
                    (unsigned)rec);
        if (n == CHAIN_MAX)
            ld_stop("DMA channel 0: the chain runs past %u records (at record 0x%08X)",
                    CHAIN_MAX, (unsigned)rec);
        visited[rec / 32] |= (uint8_t)(1u << (rec / 4 % 8));
        walked[n++] = rec;
        uint32_t count = ram_word(rec), src = ram_word(rec + 4), dst = ram_word(rec + 8);
        uint32_t next = ram_word(rec + 12);
        if (count == 0 || count > 0xFFFFu)
            ld_stop("DMA channel 0: record 0x%08X has byte count 0x%X (the measured records "
                    "have 4 to 0x640)", (unsigned)rec, (unsigned)count);
        chain[n - 1].count = count;
        chain[n - 1].src = guest(src, "source", rec);
        chain[n - 1].dst = guest(dst, "destination", rec);
        if (dst >= LFB_FIRST && dst < LFB_END)
            lfb_bytes += count;
        else
            n_other++;
        rec = next;
    }
    for (uint32_t i = 0; i < n; i++)
        visited[walked[i] / 32] = 0;
    gt.next = 0; /* the channel has followed the chain to its end (inferred) */
    gt.pending = 1;
    gt.ram = ld.ram, gt.c = ld.c;
    gt.n = n, gt.at = 0, gt.off = 0;
    gt.start = vclock_now(ld.c);
    gt.n_other = n_other, gt.lfb_bytes = lfb_bytes;
    gt.due = gt.start + dma0_delay_ns(n_other, lfb_bytes);
    gt.last = gt.start;
    dma0_deliver();
    vclock_reload(ld.c); /* so the events are seen in time (machine.h, irq_raise) */
}

/* ioctl: section 3, `pci:` */
static int pci_ioctl(int fd, uint32_t cmd, uint32_t arg)
{
    uint32_t offset, value;
    if (cmd != 0x7301)
        ld_bad_ioctl(fd, cmd, arg); /* 0x7300, never issued in MAME: its answers not measured */
    if (arg & 3u)
        ld_stop("ioctl 0x7301: the {offset, value} pointer 0x%08X is not word aligned",
                (unsigned)arg);
    offset = mem_r32(ld.ram, ld.c, arg);
    value = mem_r32(ld.ram, ld.c, arg + 4);
    if (offset == GT_DMA0_NEXT) {
        gt.next = value;
        gt.next_set = 1;
        return 0;
    }
    if (offset != GT_DMA0_CTRL)
        ld_stop("ioctl 0x7301 of GT64010 offset 0x%X (value 0x%08X): the game writes only "
                "0x830 and 0x840 (dma0.txt)", (unsigned)offset, (unsigned)value);
    if (value != 0x0DC0u && value != 0x2DC0u && value != 0x1DC0u)
        ld_stop("ioctl 0x7301 of DMA channel 0 control 0x%08X: the game writes only 0x0DC0, "
                "0x2DC0 and 0x1DC0 (dma0.txt)", (unsigned)value);
    gt.ctrl = value;
    if (value & CTRL_ENABLE)
        dma0_start();
    return 0;
}

const struct ldev ld_pci = {"pci:", ld_ret0_open, ld_ret0_close, NULL, NULL, pci_ioctl};
