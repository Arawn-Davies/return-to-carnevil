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
 * 3. fbiInit2 while PCI initEnable bit 2 is set: the DAC's latched read data (the doc's "The
 *    DAC": a dacData write stores a byte in a DAC register, or with its read flag latches
 *    one, register 5 answering by register 7's value). fbiInit3 in that state stops, for
 *    want of evidence either way.
 * Any other read stops, as does a read of a storage register never written since reset,
 * and any read after an alternate-map write (the alternate order is not decoded, so the
 * register read may have been written through it). Writes are counted per register; LFB
 * writes are accepted and every LFB read stops (the card's frame-buffer addresses alias and
 * its pipeline draws, neither modelled here: the doc's "Voodoo frame buffer reads"); texture
 * writes are counted and dropped.
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
#define REG_DACDATA (0x22Cu / 4)
#define REG_SWAPBUFFER (0x128u / 4)
#define INIT_WRITE_ENABLE 0x1u /* PCI initEnable bit 0: fbiInit writes enabled */

/* fbiInit0 to fbiInit4 (0x210 to 0x21C, 0x200) */
static int is_fbiinit(uint32_t n)
{
    return n == 0x200u / 4 || (n >= 0x210u / 4 && n <= 0x21Cu / 4);
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
    uint8_t dac[8], dac_set;   /* the DAC registers, and which have a value (bit n) */
    int latched;               /* a read has been latched since reset */
    int swapped;               /* a swapbufferCMD write has been made since reset */
    uint8_t latch;
} v;

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
    if (n == REG_STATUS) {
        if (v.swapped)
            hw_stop(c, phys, 4, 0, 0, "a status read after a swapbufferCMD write: the "
                    "displayed-buffer field (and swap-pending) is not modelled yet");
        if (v.alt_used)
            hw_stop(c, phys, 4, 0, 0, "a status read after a write through the alternate "
                    "register map, which is not decoded yet: a swap or any other command "
                    "written through it cannot be ruled out");
        return STATUS_IDLE | retrace(c);
    }
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
    if (is_fbiinit(n) && !(v.cfg[CFG_INIT_ENABLE] & INIT_WRITE_ENABLE))
        hw_stop(c, phys, 4, 1, value, "an fbiInit write while PCI initEnable bit 0 is clear "
                "(the card may ignore it; the game made none in MAME)");
    v.writes[n]++;
    if (n == REG_SWAPBUFFER)
        v.swapped = 1;
    /* the value the game can read back is the FBI's: chips 0 (all) or with the FBI bit */
    if (chips == 0 || (chips & CHIP_FBI)) {
        v.reg[n] = value;
        v.written[n] = 1;
        if (n == REG_DACDATA)
            dac_write(c, value);
    }
}

uint32_t voodoo_read(cpu_t *c, uint32_t off, int size)
{
    uint32_t phys = VOODOO_PHYS + off;
    if (off < VOODOO_LFB)
        return reg_read(c, off, size);
    if (off >= VOODOO_TEX)
        hw_stop(c, phys, size, 0, 0, "a texture memory read: the game made none in MAME");
    hw_stop(c, phys, size, 0, 0, "an LFB read: the card's frame-buffer addresses alias (the "
            "start-up memory test read 0xFFFF at 0x08432100 after writing 0xDEAD there) and "
            "its pipeline draws (the dither ramp), and the sink models neither "
            "(hardware-usage.md, \"Voodoo frame buffer reads\")");
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
    /* accepted and dropped: nothing reads them back (voodoo_read) */
    if ((size != 2 && size != 4) || (off & (uint32_t)(size - 1)))
        hw_stop(c, phys, size, 1, value, "LFB writes are aligned halfwords and words");
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

void voodoo_reset(void)
{
    memset(&v, 0, sizeof v);
    v.dac[5] = DAC_REG5_POWER_ON;
    v.dac_set = 1u << 5;
    for (size_t i = 0; i < sizeof POWER_ON / sizeof POWER_ON[0]; i++) {
        v.reg[POWER_ON[i].reg / 4] = POWER_ON[i].value;
        v.written[POWER_ON[i].reg / 4] = 1;
    }
}
