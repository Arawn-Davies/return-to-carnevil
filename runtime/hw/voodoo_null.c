/* voodoo_null.c: the Voodoo Graphics card as a sink: it accepts what the game writes,
 * answers the reads the game made in MAME the way MAME answered them, and draws nothing.
 *
 * docs/analysis/hardware-usage.md, "Dynamic check: Voodoo accesses" (the address layout and
 * the registers written and read) and "Measured values the runtime answers with" (every value
 * and kind below, each from mame/m2-facts/voodoo-reads.txt). The 16 MB at physical
 * 0x08000000: registers below 0x400000, the linear frame buffer (LFB) to 0x7FFFFF, texture
 * memory above. A register address is folded to its number (offset & 0x3FC, / 4); bits 10
 * to 13 select chips (0 all, 0x400 the FBI, 0x800 to 0x2000 the TMUs), bit 20 swizzles bytes
 * and bit 21 selects the alternate register map.
 *
 * The registers the game read are of three kinds (the doc's list says which is which):
 * 1. storage, which reads back what was last written: fbzColorPath, fogMode, alphaMode,
 *    fbzMode, lfbMode, clipLeftRight, clipLowYHighY, stipple, color0, color1, and fbiInit0 to
 *    fbiInit4, which start at the values the game first read (measured);
 * 2. status, computed: FIFOs free, idle, no swap pending, and the vertical-retrace bit
 *    for the first RETRACE_NS of each VBLANK period on the virtual clock;
 * 3. fbiInit2 (and, for want of evidence either way, fbiInit3) while PCI initEnable bit 2
 *    is set: the DAC's read data, which depends on dacData writes MAME's trace does not
 *    show, so those reads stop.
 * Any other read stops, as does a read of a storage register never written since reset,
 * and any read after an alternate-map write (the alternate order is not decoded, so the
 * register read may have been written through it). Writes are counted per register; the
 * LFB is memory (a read of a byte never written stops: pixels the card would draw are not
 * drawn here); texture writes are counted and dropped.
 */
#include <string.h>

#include "hw.h"
#include "machine.h"

#define NREGS 256
#define REG_STATUS 0x00u
#define REG_FBIINIT2 (0x218u / 4)
#define REG_FBIINIT3 (0x21Cu / 4)

#define SWIZZLE 0x100000u
#define ALT_MAP 0x200000u
#define CHIP_SHIFT 10
#define CHIP_FBI 1u
#define LFB_BYTES (VOODOO_TEX - VOODOO_LFB)

/* status as MAME returned it when idle: PCI FIFO free 0x3F, memory FIFO free 0xFFFF, no
 * busy bit, no swap pending; and the vertical-retrace bit (hardware-usage.md, "Measured
 * values ...", status) */
#define STATUS_IDLE 0x0FFFF03Fu
#define STATUS_RETRACE 0x40u
/* the retrace bit's length in each frame: the middle of the measured bounds 251,354 ns and
 * 254,120 ns (same section) */
#define RETRACE_NS 252737u

/* The readable registers (kinds 1 and 2) by number, and the power-on values of fbiInit0 to
 * fbiInit4: their first reads (same section). */
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

static struct {
    uint32_t reg[NREGS];
    uint8_t written[NREGS];
    uint32_t writes[NREGS], alt_writes[NREGS];
    int alt_used;
    uint64_t tex_writes;
    uint32_t cfg[64];
    int bar0_set;
    uint8_t lfb[LFB_BYTES];
    uint8_t lfb_set[LFB_BYTES / 8];
} v;

static int readable(uint32_t regnum)
{
    for (size_t i = 0; i < sizeof READABLE / sizeof READABLE[0]; i++)
        if (READABLE[i] == regnum * 4)
            return 1;
    return 0;
}

/* The vertical-retrace bit: the first RETRACE_NS of each period, counted from the VBLANK
 * interrupts once VBLANK runs (machine.h, vblank_start), from time 0 before. */
static uint32_t retrace(cpu_t *c)
{
    uint64_t now = vclock_now(c), p = machine.vblank_period, phase;
    if (p)
        phase = (now % p + p - machine.vblank_next % p) % p;
    else
        phase = now % VBLANK_PERIOD_NS;
    return phase < RETRACE_NS ? STATUS_RETRACE : 0;
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

static uint32_t reg_read(cpu_t *c, uint32_t off, int size)
{
    uint32_t phys = VOODOO_PHYS + off, n = reg_of(c, off, size, 0, 0);
    if (off & ALT_MAP)
        hw_stop(c, phys, 4, 0, 0, "Voodoo read through the alternate register map (bit 21), "
                "which the game never read through");
    if ((off >> CHIP_SHIFT) & 0xFu)
        hw_stop(c, phys, 4, 0, 0, "Voodoo read with chip bits 0x%X, which the game never "
                "read with", (unsigned)((off >> CHIP_SHIFT) & 0xFu));
    if (n == REG_STATUS)
        return STATUS_IDLE | retrace(c);
    if (!readable(n))
        hw_stop(c, phys, 4, 0, 0, "Voodoo register 0x%03X is not one the game read in MAME "
                "(voodoo-reads.txt), so what it reads as is not known", (unsigned)(4 * n));
    if ((n == REG_FBIINIT2 || n == REG_FBIINIT3) && (v.cfg[CFG_INIT_ENABLE] & INIT_REMAP_DAC))
        hw_stop(c, phys, 4, 0, 0, "fbiInit%u read with PCI initEnable bit 2 set: it reads the "
                "DAC, whose answers depend on dacData writes MAME's trace does not show (not "
                "modelled)", n == REG_FBIINIT2 ? 2u : 3u);
    if (v.alt_used)
        hw_stop(c, phys, 4, 0, 0, "Voodoo register 0x%03X read after a write through the "
                "alternate register map, whose order the sink does not decode: the register "
                "may have been written through it", (unsigned)(4 * n));
    if (!v.written[n])
        hw_stop(c, phys, 4, 0, 0, "Voodoo register 0x%03X never written since reset, and its "
                "power-on value is not measured", (unsigned)(4 * n));
    return v.reg[n];
}

static void reg_write(cpu_t *c, uint32_t off, int size, uint32_t value)
{
    uint32_t phys = VOODOO_PHYS + off, n = reg_of(c, off, size, 1, value);
    uint32_t chips = (off >> CHIP_SHIFT) & 0xFu;
    if (off & ALT_MAP) {
        if (chips)
            hw_stop(c, phys, 4, 1, value, "Voodoo write through the alternate map with chip "
                    "bits 0x%X, which the game never made", (unsigned)chips);
        v.alt_writes[n]++;
        v.alt_used = 1;
        return;
    }
    if (n == REG_STATUS)
        hw_stop(c, phys, 4, 1, value, "a write of the Voodoo status register, which the game "
                "never made in MAME");
    v.writes[n]++;
    /* the value the game can read back is the FBI's: chips 0 (all) or with the FBI bit */
    if (chips == 0 || (chips & CHIP_FBI)) {
        v.reg[n] = value;
        v.written[n] = 1;
    }
}

static int lfb_is_set(uint32_t i)
{
    return v.lfb_set[i / 8] >> (i % 8) & 1;
}

uint32_t voodoo_read(cpu_t *c, uint32_t off, int size)
{
    uint32_t phys = VOODOO_PHYS + off, value = 0;
    if (off < VOODOO_LFB)
        return reg_read(c, off, size);
    if (off >= VOODOO_TEX)
        hw_stop(c, phys, size, 0, 0, "a texture memory read: the game made none in MAME");
    if ((size != 2 && size != 4) || (off & (uint32_t)(size - 1)))
        hw_stop(c, phys, size, 0, 0, "LFB reads are aligned halfwords and words");
    off -= VOODOO_LFB;
    for (int i = size - 1; i >= 0; i--) {
        if (!lfb_is_set(off + (uint32_t)i))
            hw_stop(c, phys, size, 0, 0, "LFB byte at 0x%08X never written since reset: what "
                    "the card would have drawn there is not drawn by the sink",
                    (unsigned)(phys + (uint32_t)i));
        value = value << 8 | v.lfb[off + (uint32_t)i];
    }
    return value;
}

void voodoo_write(cpu_t *c, uint32_t off, int size, uint32_t value)
{
    uint32_t phys = VOODOO_PHYS + off;
    if (off < VOODOO_LFB) {
        reg_write(c, off, size, value);
        return;
    }
    if (off >= VOODOO_TEX) {
        v.tex_writes++;
        return;
    }
    if ((size != 2 && size != 4) || (off & (uint32_t)(size - 1)))
        hw_stop(c, phys, size, 1, value, "LFB writes are aligned halfwords and words");
    off -= VOODOO_LFB;
    for (uint32_t i = 0; i < (uint32_t)size; i++, value >>= 8) {
        v.lfb[off + i] = (uint8_t)value;
        v.lfb_set[(off + i) / 8] |= (uint8_t)(1u << ((off + i) % 8));
    }
}

uint32_t voodoo_reg_writes(uint32_t regnum)
{
    return regnum < NREGS ? v.writes[regnum] : 0;
}

uint32_t voodoo_alt_writes(uint32_t regnum)
{
    return regnum < NREGS ? v.alt_writes[regnum] : 0;
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

int voodoo_cfg_write(uint32_t word, uint32_t value, const char **why)
{
    if (word >= 64 || word == 0 || word == 2) {
        *why = "a write the game never made (words 0 and 2 are the identification)";
        return -1;
    }
    if (word == CFG_BAR0)
        v.bar0_set = 1;
    v.cfg[word] = value;
    return 0;
}

void voodoo_reset(void)
{
    memset(&v, 0, sizeof v);
    for (size_t i = 0; i < sizeof POWER_ON / sizeof POWER_ON[0]; i++) {
        v.reg[POWER_ON[i].reg / 4] = POWER_ON[i].value;
        v.written[POWER_ON[i].reg / 4] = 1;
    }
}
