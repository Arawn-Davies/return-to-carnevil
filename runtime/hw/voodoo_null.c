/* voodoo_null.c: the Voodoo Graphics card's registers, status and command dispatch, its
 * FIFOs and its video timing; the frame buffer and the drawing are voodoo_fb.c's, the
 * texture unit voodoo_tmu.c's.
 *
 * docs/analysis/hardware-usage.md: "Dynamic check: Voodoo accesses" (the address layout and
 * the registers written and read), "Measured values the runtime answers with" (the values
 * the measured reads gave) and "The Voodoo model" (every rule below, each marked measured,
 * read, inferred or from MAME's model, MAME 0.276's voodoo.cpp and screen.cpp). The 16 MB at
 * physical 0x08000000: registers below 0x400000, the linear frame buffer (LFB) to 0x7FFFFF,
 * texture memory above. A register address is folded to its number (offset & 0x3FC, / 4);
 * bits 10 to 13 select chips (0 all, 0x400 the FBI, 0x800 TMU0, 0x1000 and 0x2000 TMUs the
 * board does not have), bit 20 swizzles bytes and bit 21 selects the alternate register map,
 * which renumbers registers 0 to 0x3F while fbiInit3 bit 0 is set.
 *
 * The registers the game read are of three kinds (the doc's list says which is which):
 * 1. storage, which reads back what was last executed: fbzColorPath, fogMode, alphaMode,
 *    fbzMode, lfbMode, clipLeftRight, clipLowYHighY, stipple, color0, color1, and fbiInit0 to
 *    fbiInit4, which start at the values the game first read (measured);
 * 2. status, computed: the PCI and memory FIFOs' free space, the busy bits while a command
 *    runs, the vertical retrace, the displayed buffer and the swaps pending;
 * 3. fbiInit2 while PCI initEnable bit 2 is set: the DAC's latched read data (the doc's "The
 *    DAC"). fbiInit3 in that state stops, for want of evidence either way.
 * Any other read stops, as does a read of a storage register never written since reset.
 *
 * Time is the card's: picoseconds since power-on (hw.h, VOODOO_ENTRY_PS), from the virtual
 * clock at each access. A command (triangle, fastfill, swap) keeps the card busy for its
 * clocks at 50 MHz; register, LFB and texture writes made meanwhile wait in the PCI FIFO
 * (and the memory FIFO behind it) and run when the card is free, in order. The card is
 * brought up to date lazily, at each access, by running what has come due in time order:
 * queued writes and retrace starts (where a pending swap happens). The retrace also raises
 * the board's VBLANK interrupt (irq.c, vblank_raise) through a clock event. At the FIFOs'
 * water marks the card holds its writer (hw.h, "Holding the writer" in hardware-usage.md).
 */
#include <string.h>

#include "hw.h"
#include "machine.h"

#define REG_STATUS 0x00u
#define REG_TRIANGLE 0x20u
#define REG_FTRIANGLE 0x40u
#define REG_FASTFILL 0x49u
#define REG_SWAPBUFFER 0x4Au
#define REG_FBIINIT4 0x80u
#define REG_BACKPORCH 0x82u
#define REG_VIDEODIM 0x83u
#define REG_FBIINIT0 0x84u
#define REG_FBIINIT1 0x85u
#define REG_FBIINIT2 0x86u
#define REG_FBIINIT3 0x87u
#define REG_HSYNC 0x88u
#define REG_VSYNC 0x89u
#define REG_DACDATA 0x8Bu

#define SWIZZLE 0x100000u
#define ALT_MAP 0x200000u
#define CHIP_SHIFT 10
#define CHIP_FBI 1u
#define CHIP_TMU0 2u
#define INIT_WRITE_ENABLE 0x1u /* PCI initEnable bit 0: fbiInit writes enabled */
#define INIT_PCI_FIFO 0x2u     /* bit 1: writes wait in the PCI FIFO while the card is busy */

/* The card's clock: 50 MHz (MAME's NOMINAL_CLOCK), 20,000 ps a clock. */
#define CLOCK_PS 20000ull
#define CLOCK_HZ 50000000ull

/* The FIFOs (MAME's model): the PCI FIFO holds 128 entries, two per write, one slot always
 * free; the memory FIFO, in frame-buffer rows fbiInit4 bits 8 to 17 to bits 18 to 27, at
 * most 131,072 entries. */
#define PCI_FIFO_ENTRIES 128u
#define MEM_FIFO_MAX_ENTRIES 131072u
#define QUEUE_WRITES (PCI_FIFO_ENTRIES / 2 + MEM_FIFO_MAX_ENTRIES / 2)
#define FB_LAST_PAGE 511u /* 2 MB of frame buffer in 4 KB pages, less one */

/* fbiInit0 to fbiInit4 */
static int is_fbiinit(uint32_t n)
{
    return n == REG_FBIINIT4 || (n >= REG_FBIINIT0 && n <= REG_FBIINIT3);
}

/* MAME's register table: everything waits in the FIFO but the statistics counters (0x14C to
 * 0x15C) and the initialisation and video registers (0x200 to 0x230). */
static int is_fifo(uint32_t n)
{
    return !(n >= 0x53u && n <= 0x57u) && !(n >= 0x80u && n <= 0x8Cu);
}

/* The alternate register order for registers 0 to 0x3F (MAME's s_alias_map, a fact about the
 * card reproduced as data): each colour, Z, A, S, T and W start value followed by its d/dX
 * and d/dY, for the fixed-point registers (0 to 0x1F) and the float ones (0x20 to 0x3F). */
static const uint8_t ALT_ORDER[0x20] = {
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x10, 0x18, 0x09, 0x11, 0x19,
    0x0A, 0x12, 0x1A, 0x0B, 0x13, 0x1B, 0x0C, 0x14, 0x1C, 0x0D, 0x15, 0x1D, 0x0E, 0x16,
    0x1E, 0x0F, 0x17, 0x1F};

static uint32_t alt_renumber(uint32_t n)
{
    return n < 0x40u ? ALT_ORDER[n & 0x1Fu] + (n & 0x20u) : n;
}

/* The DAC behind dacData (hardware-usage.md, "The DAC"): data in bits 0 to 7, the DAC register
 * in bits 8 to 10, the read flag in bit 11. Register 5 read while register 7 holds 0x01, 0x07
 * or 0x0B answers 0x55, 0x71 or 0x79 (inferred, matching every measured read); register 5's
 * power-on value is 0 (the first plain read of it returned 0, measured). */
#define DAC_READ 0x800u
#define DAC_REG5_POWER_ON 0x00u
static const struct {
    uint8_t reg7, answer;
} DAC_REG5[] = {{0x01, 0x55}, {0x07, 0x71}, {0x0B, 0x79}};

/* The readable registers (kinds 1 and 2) by number, and the power-on values of fbiInit0 to
 * fbiInit4: their first reads (measured), which are also MAME's power-on values. */
static const uint16_t READABLE[] = {0x000, 0x104, 0x108, 0x10C, 0x110, 0x114, 0x118, 0x11C,
                                    0x140, 0x144, 0x148, 0x200, 0x210, 0x214, 0x218, 0x21C};
static const struct {
    uint16_t reg;
    uint32_t value;
} POWER_ON[] = {{0x210, 0x00000410}, {0x214, 0x00201102}, {0x218, 0x80000040},
                {0x21C, 0x001E4000}, {0x200, 0x00000001}};

/* The PCI configuration words (same section, "PCI configuration"): vendor 0x121A, device 1
 * (word 0); revision 2 (word 2); BAR0 (word 4) reads back the base written with bit 3 set. */
#define CFG_ID 0x0001121Au
#define CFG_CLASS_REV 0x00000002u
#define CFG_BAR0 4u
#define CFG_BAR0_TYPE 0x8u
#define CFG_INIT_ENABLE 0x10u   /* byte offset 0x40: initEnable */
#define INIT_REMAP_DAC 0x4u     /* its bit 2, under which fbiInit2 read the DAC (measured) */

/* The screen before the game programs the video timing: MAME's seattle.cpp, 57 Hz, 640 x 480
 * all visible, VBLANK at every frame from power-on (measured: the capture's frame counter
 * steps at multiples of 1/57 s since power-on, 296 x 17,543,859.6 ns at 5.1930 s). */
#define AS_PER_S 1000000000000000000ull
#define AS_PER_PS 1000000ull
#define DEFAULT_HZ 57u
#define DEFAULT_HEIGHT 480u
#define DEFAULT_WIDTH 640u
/* the line rates MAME's recompute_video_timing chooses between: standard, medium, VGA */
#define LINE_HZ_STD 15750u
#define LINE_HZ_MED 25000u
#define LINE_HZ_VGA 31500u

enum { E_REG, E_LFB, E_TEX };
struct entry {
    uint8_t kind, chips, size;
    uint16_t n;
    uint32_t off, value; /* off: into the 16 MB */
};

static struct {
    uint32_t reg[NREGS_VOODOO];
    uint8_t written[NREGS_VOODOO];
    uint32_t writes[NREGS_VOODOO], alt_writes[NREGS_VOODOO];
    uint64_t tex_writes;
    uint32_t cfg[64];
    int bar0_set;
    uint8_t dac[8], dac_set;   /* the DAC registers, and which have a value (bit n) */
    int latched;               /* a read has been latched since reset */
    uint8_t latch;
    /* the command running and the FIFOs */
    uint64_t op_end;           /* ps the running command ends, 0 when idle */
    struct entry q[QUEUE_WRITES];
    uint32_t q_head, q_count, n_mem; /* the oldest n_mem writes are in the memory FIFO */
    uint32_t mem_entries;      /* the memory FIFO's size, 0 when it is off */
    /* swaps (MAME's reg_swapbuffer_w, vblank_start, swap_buffers) */
    uint32_t swaps_pending, vblank_count, swap_interval;
    int swap_waiting;
    /* the screen: VBLANK starts at ref + k * frame; its rows, its width in pixels, and the
     * first row after the visible area */
    uint64_t ref_ps, frame_as, frame_ps;
    uint32_t height, width, vis_end;
    /* the card's retrace, once the video timing is programmed */
    int armed;
    uint64_t retrace_next, retrace_len, irq_next;
    uint64_t last_ps;          /* the latest card time an access brought */
    /* the hold (MAME's stall_cpu, STALLED_UNTIL_FIFO_LWM) */
    int stalled;               /* the card is holding its writer */
    int dma_writer;            /* the writes now come from the GT64010's DMA, not the CPU */
    uint64_t holds_cpu, hold_cpu_ns, holds_dma; /* for the report */
} v;

uint32_t voodoo_reg(uint32_t n)
{
    return n < NREGS_VOODOO ? v.reg[n] : 0;
}

/* The card time of an access. The card's state holds times it has passed (the running
 * command's end, the next retrace), so a clock that goes back under it, as machine_reset
 * without hw_reset does, stops. */
static uint64_t card_now(cpu_t *c, uint32_t phys, int size, int write, uint32_t value)
{
    uint64_t now = vclock_now(c) * 1000u + VOODOO_ENTRY_PS;
    if (now < v.last_ps)
        hw_stop(c, phys, size, write, value, "the virtual clock went backwards under the "
                "Voodoo (from %llu ps to %llu ps since power-on): a machine_reset without "
                "hw_reset", (unsigned long long)v.last_ps, (unsigned long long)now);
    v.last_ps = now;
    return now;
}

/* The virtual time (ns) at or after a card time. */
static uint64_t to_ns(uint64_t ps)
{
    return ps <= VOODOO_ENTRY_PS ? 0 : (ps - VOODOO_ENTRY_PS + 999u) / 1000u;
}

/* --- the DAC ------------------------------------------------------------------------------ */

/* A dacData write (stored as a register as well). */
static void dac_write(cpu_t *c, uint32_t value)
{
    uint32_t reg = (value >> 8) & 7u, data = value & 0xFFu;
    if (value & ~0xFFFu)
        hw_stop(c, VOODOO_PHYS + 4 * REG_DACDATA, 4, 1, value, "dacData with bits above 11 "
                "set, which the game never wrote");
    if (!(value & DAC_READ)) {
        v.dac[reg] = (uint8_t)data;
        v.dac_set |= (uint8_t)(1u << reg);
        return;
    }
    if (reg == 5 && !(v.dac_set & 0x80u))
        hw_stop(c, VOODOO_PHYS + 4 * REG_DACDATA, 4, 1, value, "a read request of DAC register "
                "5 before register 7 was written since reset: register 5's answer depends on "
                "register 7, whose power-on value is not measured");
    if (reg == 5)
        for (size_t i = 0; i < sizeof DAC_REG5 / sizeof DAC_REG5[0]; i++)
            if (DAC_REG5[i].reg7 == v.dac[7]) {
                v.latch = DAC_REG5[i].answer, v.latched = 1;
                return;
            }
    if (!(v.dac_set >> reg & 1u))
        hw_stop(c, VOODOO_PHYS + 4 * REG_DACDATA, 4, 1, value, "a read request of DAC register "
                "%u, never written since reset: its power-on value is not measured",
                (unsigned)reg);
    v.latch = v.dac[reg], v.latched = 1;
}

/* --- video timing and the retrace (MAME's recompute_video_timing, screen_device::configure
 * and time_until_pos, adjust_vblank_start_timer, vblank_start, vblank_stop) --------------- */

static void retrace_event(void *arg)
{
    (void)arg;
    vblank_raise();
    v.irq_next += v.frame_ps;
    vclock_at(to_ns(v.irq_next), retrace_event, NULL);
}

/* The next time after `now` the beam reaches the start of row `row`: rows count from the
 * screen's VBLANK start, which is vis_end rows after row 0; a time within half a pixel
 * counts as past. */
static uint64_t until_row(uint64_t now, uint32_t row)
{
    uint64_t scan = v.frame_ps / v.height, pixel = v.frame_ps / ((uint64_t)v.height * v.width);
    uint64_t cur = now - v.ref_ps, target = ((row + v.height - v.vis_end) % v.height) * scan;
    if (target <= cur + pixel / 2)
        target += v.frame_ps;
    while (target <= cur)
        target += v.frame_ps;
    return v.ref_ps + target;
}

static int in_retrace(uint64_t now)
{
    uint64_t last;
    if (!v.armed || v.retrace_next < v.frame_ps || v.retrace_next <= now)
        return 0;
    last = v.retrace_next - v.frame_ps;
    return last <= now && now < last + v.retrace_len;
}

static uint64_t abs_diff(uint64_t a, uint64_t b)
{
    return a > b ? a - b : b - a;
}

/* reg_video_w: once hSync, vSync, backPorch and videoDimensions are all non-zero (in their
 * register widths), the screen takes vtotal rows at the standard, medium or VGA line rate
 * whose frame is closest to its current one; it keeps the time of its last VBLANK start,
 * unless that is a new frame ago, when VBLANK starts now. The card's retrace runs from row
 * vSync-off to row 0. */
static void video_timing(cpu_t *c, uint64_t now, uint32_t n, uint32_t value)
{
    uint32_t hs = v.reg[REG_HSYNC] & 0x3FFFFFFu, vs = v.reg[REG_VSYNC] & 0xFFFFFFFu;
    uint32_t bp = v.reg[REG_BACKPORCH] & 0xFFFFFFu, dim = v.reg[REG_VIDEODIM] & 0x3FFFFFFu;
    uint32_t vtotal, vbp, vvis, vmax, vsyncoff;
    uint64_t std, med, vga, ds, dm, dv, frame, last;
    if (!hs || !vs || !bp || !dim)
        return;
    if (in_retrace(now))
        hw_stop(c, VOODOO_PHYS + 4 * n, 4, 1, value, "the video timing reprogrammed during "
                "the vertical retrace: when MAME's model ends that retrace is not modelled");
    vsyncoff = (vs >> 16) & 0xFFFu;
    vtotal = vsyncoff + (vs & 0xFFFu);
    /* every quantity the clock divides or steps by must be non-zero: the frame's rows (and
     * so its period and its row period) and the row's pixels (hSync's fields plus 2, never
     * zero) */
    if (vtotal == 0 || ((hs >> 16) & 0x3FFu) + 1 + (hs & 0xFFu) + 1 == 0)
        hw_stop(c, VOODOO_PHYS + 4 * n, 4, 1, value, "Voodoo video timing with zero total "
                "lines or pixels (hSync 0x%08X, vSync 0x%08X, backPorch 0x%08X, "
                "videoDimensions 0x%08X): MAME's model divides by them", (unsigned)hs,
                (unsigned)vs, (unsigned)bp, (unsigned)dim);
    vbp = (bp >> 16) & 0xFFu;
    vvis = (dim >> 16) & 0x3FFu;
    vmax = vbp + (vvis ? vvis - 1 : 0);
    if (vmax > vtotal - 1)
        vmax = vtotal - 1;
    std = AS_PER_S / LINE_HZ_STD * vtotal;
    med = AS_PER_S / LINE_HZ_MED * vtotal;
    vga = AS_PER_S / LINE_HZ_VGA * vtotal;
    ds = abs_diff(std, v.frame_as), dm = abs_diff(med, v.frame_as), dv = abs_diff(vga, v.frame_as);
    frame = ds < dm && ds < dv ? std : dm < dv ? med : vga;
    last = v.ref_ps + (now - v.ref_ps) / v.frame_ps * v.frame_ps;
    v.frame_as = frame;
    v.frame_ps = (frame + AS_PER_PS / 2) / AS_PER_PS;
    if (now - last >= v.frame_ps)
        last = now;
    v.ref_ps = last;
    v.height = vtotal;
    v.width = ((hs >> 16) & 0x3FFu) + 1 + (hs & 0xFFu) + 1;
    v.vis_end = vmax + 1;
    v.retrace_next = until_row(now, vsyncoff);
    v.retrace_len = until_row(v.retrace_next, 0) - v.retrace_next;
    v.armed = 1;
    v.irq_next = v.retrace_next;
    vclock_cancel(retrace_event, NULL);
    vclock_at(to_ns(v.irq_next), retrace_event, NULL);
    vclock_reload(c); /* so the current slice ends at the first retrace */
}

int voodoo_vsync(uint64_t *next_ps, uint64_t *len_ps, uint64_t *period_ps)
{
    if (!v.armed)
        return 0;
    *next_ps = v.retrace_next, *len_ps = v.retrace_len, *period_ps = v.frame_ps;
    return 1;
}

/* --- the FIFOs ------------------------------------------------------------------------------ */

static uint32_t n_pci(void)
{
    return v.q_count - v.n_mem;
}

static uint32_t pci_space(void)
{
    return PCI_FIFO_ENTRIES - 1 - 2 * n_pci();
}

/* recompute_fbmem_fifo: on when fbiInit0 bit 13 is set and the rows are in order; a new
 * configuration empties it */
static void mem_fifo_configure(void)
{
    uint32_t i4 = v.reg[REG_FBIINIT4], last = (i4 >> 18) & 0x3FFu, first = (i4 >> 8) & 0x3FFu;
    if (last > FB_LAST_PAGE)
        last = FB_LAST_PAGE;
    if (first <= last && (v.reg[REG_FBIINIT0] & 0x2000u)) {
        v.mem_entries = (last + 1 - first) * 0x1000u / 4;
        if (v.mem_entries > MEM_FIFO_MAX_ENTRIES)
            v.mem_entries = MEM_FIFO_MAX_ENTRIES;
    } else {
        v.mem_entries = 0;
    }
    v.q_head = (v.q_head + v.n_mem) % QUEUE_WRITES; /* the memory FIFO's writes are lost */
    v.q_count -= v.n_mem;
    v.n_mem = 0;
}

static void card_advance(cpu_t *c, uint64_t now);

/* check_stalled_cpu: the hold ends when the memory FIFO (if it is on) is below its high-water
 * mark, or else the PCI FIFO has more than its low-water mark free, or the card is idle */
static void check_release(void)
{
    uint32_t i0 = v.reg[REG_FBIINIT0];
    if (!v.stalled)
        return;
    if (!v.op_end)
        v.stalled = 0;
    else if (i0 & 0x2000u)
        v.stalled = 2 * v.n_mem >= 2 * 32 * ((i0 >> 14) & 0x7FFu);
    else
        v.stalled = pci_space() <= 2 * ((i0 >> 6) & 0x1Fu);
}

/* The next card time the hold may end: the running command's end (MAME's stall resume
 * timer), or the retrace start that does a waiting swap (swap_buffers checks the stall too),
 * whichever is first. */
static uint64_t hold_check_ps(void)
{
    uint64_t t = v.op_end;
    if (v.swap_waiting && v.armed && (!t || v.retrace_next < t))
        t = v.retrace_next;
    return t;
}

/* The CPU held by its store (MAME: gt64xxx's pci_stall spins the CPU until the card
 * releases it): virtual time passes, with the clock's events on the way, until the card has
 * drained to the point of release. The store itself is already queued. */
static void hold(cpu_t *c, uint32_t phys, int size, uint32_t value)
{
    while (v.stalled) {
        /* a signal ends the run here too (machine.h, rt_poll_signal): the held store is
         * already queued, so the guest instruction is complete as far as anything can see */
        rt_poll_signal();
        uint64_t t = to_ns(hold_check_ps()), now = vclock_now(c);
        if (t > now) {
            vclock_advance(c, t - now);
            v.hold_cpu_ns += t - now;
        }
        card_advance(c, card_now(c, phys, size, 1, value));
    }
}

/* A write the busy card queues (add_to_fifo): at a water mark the card holds its writer
 * (hw.h; a CPU store is held here, the DMA by dev_sysctrl.c); a write MAME's model would lose
 * (a full FIFO) stops. */
static void enqueue(cpu_t *c, uint32_t phys, int size, struct entry e)
{
    uint32_t i0 = v.reg[REG_FBIINIT0], i4 = v.reg[REG_FBIINIT4];
    int hold_now = 0;
    if (2 * (n_pci() + 1) > PCI_FIFO_ENTRIES - 1)
        hw_stop(c, phys, size, 1, e.value, "a Voodoo write with the PCI FIFO full (MAME "
                "asserts; the card would have held the writer before)");
    v.q[(v.q_head + v.q_count) % QUEUE_WRITES] = e;
    v.q_count++;
    if ((i0 & 0x2000u) && pci_space() <= 2 * ((i4 >> 2) & 0x3Fu)) {
        while (n_pci()) {
            const struct entry *h = &v.q[(v.q_head + v.n_mem) % QUEUE_WRITES];
            if ((h->kind == E_LFB && !(i0 & 0x800u)) || (h->kind == E_TEX && !(i0 & 0x1000u)))
                break;
            if (2 * (v.n_mem + 1) + 1 > v.mem_entries)
                hw_stop(c, phys, size, 1, e.value, "a Voodoo write with the memory FIFO full "
                        "or off (MAME's model loses it)");
            v.n_mem++;
        }
        if ((i0 & 0x10u) && 2 * v.n_mem >= 2 * 32 * ((i0 >> 14) & 0x7FFu))
            hold_now = 1; /* the memory FIFO at its high-water mark (fbiInit0 bits 14 to 24) */
    }
    if ((i0 & 0x10u) && pci_space() <= 2 * ((i0 >> 6) & 0x1Fu))
        hold_now = 1; /* the PCI FIFO at its low-water mark (fbiInit0 bits 6 to 10) */
    if (hold_now && !v.stalled) {
        v.stalled = 1;
        if (v.dma_writer)
            v.holds_dma++;
        else
            v.holds_cpu++;
    }
    if (v.stalled && !v.dma_writer)
        hold(c, phys, size, e.value);
}

/* --- executing a write ------------------------------------------------------------------------ */

/* MAME's float_to_int32: a raw IEEE single to fixed point with `bits` fraction bits, the
 * left shift wrapping in 32 bits as the x86-64 build does. */
static uint32_t float_fixed32(uint32_t data, int bits)
{
    int exponent = (int)((data >> 23) & 0xFFu) - 127 - 23 + bits;
    uint32_t m = (data & 0x7FFFFFu) | 0x800000u;
    if (exponent < 0)
        m = exponent > -32 ? m >> -exponent : 0;
    else
        m = exponent < 32 ? m << exponent : 0x7FFFFFFFu;
    return (data & 0x80000000u) ? 0 - m : m;
}

/* swap_buffers: the buffers rotate, a pending swap is done, and a running command's queue
 * goes on from `at` */
static void swap_now(uint64_t at)
{
    vfb_swap();
    if (v.swaps_pending)
        v.swaps_pending--;
    v.vblank_count = 0;
    v.swap_waiting = 0;
    if (v.op_end)
        v.op_end = at;
}

/* A register write as the card executes it, at card time `at` (the time of the access or of
 * the flush that runs it); returns its clocks. */
static uint32_t execute_reg(cpu_t *c, uint32_t chips, uint32_t n, uint32_t value, uint64_t at)
{
    const char *why = NULL;
    uint32_t cycles = 0;
    int fbi = chips == 0 || (chips & CHIP_FBI), tmu = chips == 0 || (chips & CHIP_TMU0);
    if (fbi) {
        v.reg[n] = value;
        v.written[n] = 1;
        /* the float vertex and colour registers write their fixed-point twins (MAME's
         * reg_fpassive_4_w and _12_w) */
        if (n >= 0x22u && n <= 0x27u)
            v.reg[n - 0x20u] = float_fixed32(value, 4);
        else if ((n >= 0x28u && n <= 0x2Cu) || (n >= 0x30u && n <= 0x34u) ||
                 (n >= 0x38u && n <= 0x3Cu))
            v.reg[n - 0x20u] = float_fixed32(value, 12);
    }
    if (tmu)
        tmu_reg_write(n, value);
    switch (n) {
    case REG_TRIANGLE:
    case REG_FTRIANGLE:
        cycles = vfb_triangle(&why);
        break;
    case REG_FASTFILL:
        cycles = vfb_fastfill(&why);
        break;
    case REG_SWAPBUFFER:
        v.swap_waiting = 1;
        v.swap_interval = (value >> 1) & 0xFFu;
        if (!(value & 1u))
            swap_now(at);
        else
            cycles = (uint32_t)((v.swap_interval + 1) * CLOCK_HZ / 10);
        break;
    case REG_FBIINIT0:
        if (!fbi)
            break;
        if (value & 0x2u) /* graphics reset: both FIFOs emptied */
            v.q_count = v.n_mem = 0;
        if (value & 0x4u) /* FIFO reset: the PCI FIFO emptied */
            v.q_count = v.n_mem;
        mem_fifo_configure();
        break;
    case REG_FBIINIT4:
        if (fbi)
            mem_fifo_configure();
        break;
    case REG_FBIINIT1:
    case REG_FBIINIT2:
        if (fbi)
            vfb_layout(v.reg[REG_FBIINIT1], v.reg[REG_FBIINIT2]);
        break;
    case REG_BACKPORCH:
    case REG_VIDEODIM:
    case REG_HSYNC:
    case REG_VSYNC:
        if (fbi)
            video_timing(c, at, n, value);
        break;
    case REG_DACDATA:
        if (fbi)
            dac_write(c, value);
        break;
    default:
        break;
    }
    if (why)
        hw_stop(c, VOODOO_PHYS + 4 * n, 4, 1, value, "%s", why);
    return cycles;
}

static uint32_t execute(cpu_t *c, const struct entry *e, uint64_t at)
{
    const char *why;
    if (e->kind == E_REG)
        return execute_reg(c, e->chips, e->n, e->value, at);
    if (e->kind == E_LFB)
        why = vfb_lfb_write(e->off - VOODOO_LFB, e->size, e->value);
    else
        why = tmu_tex_write(e->off - VOODOO_TEX, e->value);
    if (why)
        hw_stop(c, VOODOO_PHYS + e->off, e->size, 1, e->value, "%s", why);
    return 0;
}

/* execute_fifos from the running command's end: queued writes in order (memory FIFO first)
 * until one takes clocks; idle when the queue runs dry. `at` is the time of the flush. */
static void run_fifo(cpu_t *c, uint64_t at)
{
    while (v.q_count) {
        struct entry e = v.q[v.q_head];
        uint32_t cycles;
        v.q_head = (v.q_head + 1) % QUEUE_WRITES;
        v.q_count--;
        if (v.n_mem)
            v.n_mem--;
        cycles = execute(c, &e, at);
        if (cycles) {
            v.op_end += cycles * CLOCK_PS;
            return;
        }
    }
    v.op_end = 0;
}

/* Everything due up to `now`, in time order: queued writes as the running command ends,
 * and retrace starts (vblank_start: a pending swap whose interval has passed happens). A
 * queued write that comes due before a retrace start runs in that retrace's flush. */
static void card_advance(cpu_t *c, uint64_t now)
{
    for (;;) {
        int op = v.op_end && v.op_end <= now;
        int vb = v.armed && v.retrace_next <= now;
        if (op && (!vb || v.op_end <= v.retrace_next)) {
            run_fifo(c, vb ? v.retrace_next : now);
        } else if (vb) {
            uint64_t at = v.retrace_next;
            v.retrace_next += v.frame_ps;
            if (v.vblank_count < 250)
                v.vblank_count++;
            if (v.swap_waiting && v.vblank_count >= v.swap_interval) {
                swap_now(at);
                while (v.op_end && v.op_end <= at)
                    run_fifo(c, at);
            }
        } else {
            check_release();
            return;
        }
    }
}

/* --- register accesses ------------------------------------------------------------------------ */

static int readable(uint32_t regnum)
{
    for (size_t i = 0; i < sizeof READABLE / sizeof READABLE[0]; i++)
        if (READABLE[i] == regnum * 4)
            return 1;
    return 0;
}

/* The register number of a register-space access, after the checks reads and writes share. */
static uint32_t reg_of(cpu_t *c, uint32_t off, int size, int write, uint32_t value)
{
    uint32_t phys = VOODOO_PHYS + off;
    if (size != 4 || (off & 3u))
        hw_stop(c, phys, size, write, value, "Voodoo register at offset 0x%06X: the game's "
                "register accesses are aligned words", (unsigned)off);
    if (off & SWIZZLE)
        hw_stop(c, phys, size, write, value, "Voodoo register at offset 0x%06X with address "
                "bit 20 (byte swizzle), which the game never used", (unsigned)off);
    if (off & 0x0FC000u)
        hw_stop(c, phys, size, write, value, "Voodoo register at offset 0x%06X with address "
                "bits 14 to 19 set, which the game never used", (unsigned)off);
    return (off & 0x3FCu) >> 2;
}

/* reg_status_r: PCI FIFO free writes (bits 0 to 5, at most 0x3F), the retrace (6), busy
 * (7 to 9), the displayed buffer (10 and 11), memory FIFO free writes (12 to 27, 0xFFFF
 * while it is off) and the swaps pending (28 to 30, at most 7). */
static uint32_t status(uint64_t now)
{
    uint32_t r = pci_space() / 2, mem = 0xFFFFu;
    if (r > 0x3Fu)
        r = 0x3Fu;
    if (in_retrace(now))
        r |= 0x40u;
    if (v.op_end)
        r |= 0x380u;
    r |= (uint32_t)vfb_front() << 10;
    if (v.reg[REG_FBIINIT0] & 0x2000u) {
        uint32_t space = v.mem_entries ? (v.mem_entries - 1 - 2 * v.n_mem) / 2 : 0;
        mem = space < 0xFFFFu ? space : 0xFFFFu;
    }
    r |= mem << 12;
    r |= (v.swaps_pending < 7 ? v.swaps_pending : 7) << 28;
    return r;
}

static uint32_t reg_read(cpu_t *c, uint32_t off, int size)
{
    uint32_t phys = VOODOO_PHYS + off, n = reg_of(c, off, size, 0, 0);
    uint64_t now = card_now(c, phys, size, 0, 0);
    if (off & ALT_MAP)
        hw_stop(c, phys, 4, 0, 0, "Voodoo read through the alternate register map (bit 21), "
                "which the game never read through");
    if ((off >> CHIP_SHIFT) & 0xFu)
        hw_stop(c, phys, 4, 0, 0, "Voodoo read with chip bits 0x%X, which the game never "
                "read with", (unsigned)((off >> CHIP_SHIFT) & 0xFu));
    card_advance(c, now);
    if (n == REG_STATUS)
        return status(now);
    if (!readable(n))
        hw_stop(c, phys, 4, 0, 0, "Voodoo register 0x%03X is not one the game read in MAME "
                "(voodoo-reads.txt), so what it reads as is not known", (unsigned)(4 * n));
    if (n == REG_FBIINIT2 && (v.cfg[CFG_INIT_ENABLE] & INIT_REMAP_DAC)) {
        if (!v.latched)
            hw_stop(c, phys, 4, 0, 0, "fbiInit2 read with PCI initEnable bit 2 set reads the "
                    "DAC's latched data, and no DAC read was requested since reset");
        return v.latch;
    }
    if (n == REG_FBIINIT3 && (v.cfg[CFG_INIT_ENABLE] & INIT_REMAP_DAC))
        hw_stop(c, phys, 4, 0, 0, "fbiInit3 read with PCI initEnable bit 2 set: whether it "
                "reads the DAC too is not measured (the game never made such a read)");
    if (!v.written[n])
        hw_stop(c, phys, 4, 0, 0, "Voodoo register 0x%03X never written since reset, and its "
                "power-on value is not measured", (unsigned)(4 * n));
    return v.reg[n];
}

static void reg_write(cpu_t *c, uint32_t off, int size, uint32_t value)
{
    uint32_t phys = VOODOO_PHYS + off, n = reg_of(c, off, size, 1, value), cycles;
    uint32_t chips = (off >> CHIP_SHIFT) & 0xFu;
    uint64_t now = card_now(c, phys, size, 1, value);
    if (off & ALT_MAP) {
        if (chips)
            hw_stop(c, phys, 4, 1, value, "Voodoo write through the alternate map with chip "
                    "bits 0x%X, which the game never made", (unsigned)chips);
        v.alt_writes[n]++;
        if (v.reg[REG_FBIINIT3] & 1u) /* tri_register_remap */
            n = alt_renumber(n);
    } else {
        v.writes[n]++;
    }
    if (n == REG_STATUS)
        hw_stop(c, phys, 4, 1, value, "a write of the Voodoo status register, which the game "
                "never made in MAME");
    if (is_fbiinit(n) && !(v.cfg[CFG_INIT_ENABLE] & INIT_WRITE_ENABLE))
        hw_stop(c, phys, 4, 1, value, "an fbiInit write while PCI initEnable bit 0 is clear "
                "(the card may ignore it; the game made none in MAME)");
    card_advance(c, now);
    if (!is_fifo(n)) {
        execute_reg(c, chips, n, value, now);
        return;
    }
    if (n == REG_SWAPBUFFER)
        v.swaps_pending++;
    if (v.op_end && (v.cfg[CFG_INIT_ENABLE] & INIT_PCI_FIFO)) {
        enqueue(c, phys, 4, (struct entry){E_REG, (uint8_t)chips, 4, (uint16_t)n, off, value});
        return;
    }
    cycles = execute_reg(c, chips, n, value, now);
    if (cycles)
        v.op_end = now + cycles * CLOCK_PS;
}

/* --- the address space ---------------------------------------------------------------------- */

uint32_t voodoo_read(cpu_t *c, uint32_t off, int size)
{
    uint32_t phys = VOODOO_PHYS + off, value;
    const char *why;
    if (off < VOODOO_LFB)
        return reg_read(c, off, size);
    if (off >= VOODOO_TEX)
        hw_stop(c, phys, size, 0, 0, "a texture memory read: the game made none in MAME");
    if ((size != 2 && size != 4) || (off & (uint32_t)(size - 1)))
        hw_stop(c, phys, size, 0, 0, "LFB reads are aligned halfwords and words");
    card_advance(c, card_now(c, phys, size, 0, 0));
    if (vfb_lfb_read(off - VOODOO_LFB, size, &value, &why) != 0)
        hw_stop(c, phys, size, 0, 0, "%s", why);
    return value;
}

void voodoo_write(cpu_t *c, uint32_t off, int size, uint32_t value)
{
    uint32_t phys = VOODOO_PHYS + off;
    struct entry e = {E_LFB, 0, (uint8_t)size, 0, off, value};
    uint64_t now;
    if (off < VOODOO_LFB) {
        reg_write(c, off, size, value);
        return;
    }
    if (off >= VOODOO_TEX) {
        if (size != 4 || (off & 3u))
            hw_stop(c, phys, size, 1, value, "texture writes are aligned words");
        v.tex_writes++;
        e.kind = E_TEX;
    } else if ((size != 2 && size != 4) || (off & (uint32_t)(size - 1))) {
        hw_stop(c, phys, size, 1, value, "LFB writes are aligned halfwords and words");
    }
    now = card_now(c, phys, size, 1, value);
    card_advance(c, now);
    if (v.op_end && (v.cfg[CFG_INIT_ENABLE] & INIT_PCI_FIFO))
        enqueue(c, phys, size, e);
    else
        execute(c, &e, now);
}

void voodoo_dma_writer(int on)
{
    v.dma_writer = on;
}

int voodoo_holding(void)
{
    return v.stalled;
}

uint64_t voodoo_hold_check_ns(void)
{
    return to_ns(hold_check_ps());
}

void voodoo_hold_counts(uint64_t *cpu, uint64_t *cpu_ns, uint64_t *dma)
{
    *cpu = v.holds_cpu, *cpu_ns = v.hold_cpu_ns, *dma = v.holds_dma;
}

void voodoo_poll(cpu_t *c)
{
    card_advance(c, card_now(c, VOODOO_PHYS, 4, 0, 0));
}

uint32_t voodoo_reg_writes(uint32_t regnum)
{
    return regnum < NREGS_VOODOO ? v.writes[regnum] : 0;
}

uint32_t voodoo_alt_writes(uint32_t regnum)
{
    return regnum < NREGS_VOODOO ? v.alt_writes[regnum] : 0;
}

uint64_t voodoo_tex_writes(void)
{
    return v.tex_writes;
}

/* --- PCI configuration ------------------------------------------------------------------- */

int voodoo_cfg_read(uint32_t word, uint32_t *out, const char **why)
{
    switch (word) {
    case 0:
        *out = CFG_ID;
        return 0;
    case 2:
        *out = CFG_CLASS_REV;
        return 0;
    case CFG_BAR0:
        if (!v.bar0_set) {
            *why = "BAR0 read before slot 0x110 mapped the card (its value then is not "
                   "measured)";
            return -1;
        }
        *out = (v.cfg[CFG_BAR0] & ~0xFu) | CFG_BAR0_TYPE;
        return 0;
    default:
        *why = "a word the game never read in MAME (voodoo-reads.txt), so what it reads as is "
               "not known";
        return -1;
    }
}

/* The words the game wrote through slot 0x0F8 (measured, voodoo-reads.txt: hardware-usage.md,
 * "PCI configuration"): 1 (byte offset 0x04), 0x10 (0x40, initEnable), 0x11 (0x44), 0x12
 * (0x48), 0x30 (0xC0) and 0x38 (0xE0). BAR0 is written only by slot 0x110
 * (voodoo_cfg_map_bar0), so a BAR-sizing write never reaches it. */
static const uint8_t CFG_WRITTEN[] = {0x01, 0x10, 0x11, 0x12, 0x30, 0x38};

int voodoo_cfg_write(uint32_t word, uint32_t value, const char **why)
{
    for (size_t i = 0; i < sizeof CFG_WRITTEN; i++)
        if (CFG_WRITTEN[i] == word) {
            v.cfg[word] = value;
            return 0;
        }
    *why = "a write of a word the game never wrote in MAME (it wrote byte offsets 0x04, 0x40, "
           "0x44, 0x48, 0xC0 and 0xE0, and BAR0 only through slot 0x110)";
    return -1;
}

void voodoo_cfg_map_bar0(uint32_t base)
{
    v.cfg[CFG_BAR0] = base;
    v.bar0_set = 1;
}

/* Power-on: the registers' measured first values, the DAC, idle, both FIFOs empty and the
 * memory FIFO off, MAME's default screen and no retrace, the frame buffer and the TMU. */
void voodoo_reset(void)
{
    vclock_cancel(retrace_event, NULL);
    memset(&v, 0, sizeof v);
    v.dac[5] = DAC_REG5_POWER_ON;
    v.dac_set = 1u << 5;
    for (size_t i = 0; i < sizeof POWER_ON / sizeof POWER_ON[0]; i++) {
        v.reg[POWER_ON[i].reg / 4] = POWER_ON[i].value;
        v.written[POWER_ON[i].reg / 4] = 1;
    }
    v.frame_as = AS_PER_S / DEFAULT_HZ;
    v.frame_ps = (v.frame_as + AS_PER_PS / 2) / AS_PER_PS;
    v.height = DEFAULT_HEIGHT;
    v.width = DEFAULT_WIDTH;
    v.vis_end = DEFAULT_HEIGHT;
    vfb_reset();
    tmu_reset();
}
