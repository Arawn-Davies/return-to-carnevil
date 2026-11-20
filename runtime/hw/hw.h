/* hw.h: the hardware GAME.EXE touches directly, behind bus_read and bus_write
 * (runtime/core/bus.c): the Voodoo Graphics sink and its PCI configuration (voodoo_null.c),
 * the gun registers (gun.c), the sound FIFO and the register at 0x17F00000 (misc.c).
 *
 * docs/analysis/hardware-usage.md is the reference: "GAME.EXE", the two "Dynamic check"
 * sections, "Measured values the runtime answers with" and "Decisions for the runtime",
 * items 1 to 4. Every value a device answers with is one that section quotes from the MAME
 * measurements; whatever they do not show stops (hw_stop) rather than being guessed.
 *
 * Addresses here are physical (bus.c folds kseg0 and kseg1 away). The devices see the
 * access size (1, 2 or 4) and, for a write, the value masked to it.
 */
#ifndef CARNEVIL_HW_H
#define CARNEVIL_HW_H

#include <stdint.h>

#include "recomp.h"

/* hw_reset: every device here to its power-on state (the values hardware-usage.md gives for
 * the hand-off), every counter to 0. For tests and for the start of a run, which must call
 * it before the first access (until then the state is all zero, which is not the board's);
 * machine_reset and loader_reset do not call it. */
void hw_reset(void);
/* The parts of it, one per file with state (hw_reset is in misc.c). */
void voodoo_reset(void);
void misc_reset(void);

/* hw_stop: the stop of an access no device models: names the physical address, the size,
 * read or write (with the value of a write), c->pc (the pc as last stored, exact only in a
 * --trace-pc build) and why. Defined in bus.c. */
_Noreturn void hw_stop(cpu_t *c, uint32_t phys, int size, int write, uint32_t value,
                       const char *fmt, ...) __attribute__((format(printf, 6, 7)));

/* --- the Voodoo Graphics card (voodoo_null.c, voodoo_fb.c, voodoo_tmu.c) ------------------ */

/* Physical 0x08000000 to 0x08FFFFFF: registers (offset 0 to 0x3FFFFF), the linear frame
 * buffer (0x400000 to 0x7FFFFF) and texture memory (0x800000 to 0xFFFFFF). off is the
 * offset into the 16 MB. */
#define VOODOO_PHYS 0x08000000u
#define VOODOO_SIZE 0x01000000u
#define VOODOO_LFB 0x00400000u
#define VOODOO_TEX 0x00800000u
uint32_t voodoo_read(cpu_t *c, uint32_t off, int size);
void voodoo_write(cpu_t *c, uint32_t off, int size, uint32_t value);

/* Writes per register number (offset & 0x3FC, / 4) by the standard map, and through the
 * alternate map (address bit 21, counted by the raw number before it is renumbered);
 * texture memory writes. For the later report. */
uint32_t voodoo_reg_writes(uint32_t regnum);
uint32_t voodoo_alt_writes(uint32_t regnum);
uint64_t voodoo_tex_writes(void);

/* The card's vertical retrace, which is also the board's VBLANK interrupt (voodoo_null.c;
 * hardware-usage.md, "The Voodoo model", "Video timing and retrace"). Times are picoseconds
 * since power-on: the runtime's virtual time 0 is GAME.EXE's entry, VOODOO_ENTRY_PS after
 * power-on (measured: the entry at CPU cycle 750,535,450 at 150 MHz, tools/mame/README.md,
 * "M2 facts"). voodoo_vsync: 1 with the next retrace start after the time last processed,
 * the retrace's length and the frame period once the video timing is programmed, else 0. */
#define VOODOO_ENTRY_PS 5003569666667ull
int voodoo_vsync(uint64_t *next_ps, uint64_t *len_ps, uint64_t *period_ps);

/* --- inside the Voodoo model (voodoo_null.c, voodoo_fb.c, voodoo_tmu.c) ------------------ */

#define NREGS_VOODOO 256
/* The FBI's copy of register n, as last executed. */
uint32_t voodoo_reg(uint32_t n);

/* Why a triangle (or an LFB write) was dropped: the first pipeline feature its modes enable
 * that the model does not implement (vfb_capability, tmu_capability). */
enum {
    DROP_SUBPIXEL,
    DROP_GOURAUD,
    DROP_COLOR_COMBINE,
    DROP_CHROMA,
    DROP_STIPPLE,
    DROP_DEPTH,
    DROP_ALPHA_MASK,
    DROP_AUX_WRITE,
    DROP_ALPHA_TEST,
    DROP_ALPHA_BLEND,
    DROP_FOG,
    DROP_TMUS_DISABLED,
    DROP_TMU_OFF,
    DROP_PERSPECTIVE,
    DROP_TRILINEAR,
    DROP_MIPMAP,
    DROP_BILINEAR,
    DROP_TEX_FORMAT,
    DROP_TEX_COMBINE,
    DROP_LFB_MODE,
    DROP_NO_BUFFER,
    DROP_COUNT
};
#define VFB_DRAWABLE (-1)

/* The frame buffer (voodoo_fb.c). vfb_layout: recompute_video_memory from fbiInit1 and 2.
 * vfb_lfb_read: 0 with the value, or -1 with the reason the caller stops on (a pixel the
 * model cannot answer for). vfb_lfb_write, vfb_fastfill and vfb_triangle return NULL, or set
 * *why, for an access MAME's model leaves undefined (outside the memory); vfb_fastfill and
 * vfb_triangle return the card's clocks for the command. vfb_capability: VFB_DRAWABLE or the
 * DROP_ reason for the current mode registers. vfb_dropped: triangles (and, for
 * DROP_LFB_MODE, LFB writes) dropped for a reason; a dropped draw flags its buffer, and an
 * LFB read of a flagged buffer gives -1. */
void vfb_reset(void);
void vfb_layout(uint32_t fbiinit1, uint32_t fbiinit2);
int vfb_lfb_read(uint32_t off, int size, uint32_t *value, const char **why);
const char *vfb_lfb_write(uint32_t off, int size, uint32_t value);
uint32_t vfb_fastfill(const char **why);
uint32_t vfb_triangle(const char **why);
int vfb_capability(void);
void vfb_swap(void);
int vfb_front(void);
uint32_t vfb_dropped(int reason);
const char *vfb_drop_name(int reason);

/* The texture unit (voodoo_tmu.c). tmu_reg_write: TMU0's copy of a register (the S, T and W
 * registers in MAME's internal formats). tmu_tex_write: a texture-memory word, NULL or the
 * reason it cannot be placed. tmu_capability: as vfb_capability, for the texture path.
 * tmu_iter_start, tmu_iter_step and tmu_texel: S, T and W at a pixel dx, dy from vertex A,
 * and its texel; tmu_texel returns -1 for a texel never written since reset. */
struct tmu_iter {
    double s, t, w, ds, dt, dw;
};
void tmu_reset(void);
void tmu_reg_write(uint32_t n, uint32_t value);
const char *tmu_tex_write(uint32_t off, uint32_t value);
int tmu_config_send(void);
int tmu_capability(void);
void tmu_iter_start(struct tmu_iter *it, int32_t dx, int32_t dy);
void tmu_iter_step(struct tmu_iter *it);
int tmu_texel(const struct tmu_iter *it, uint8_t rgb[3]);
/* MAME's float_to_int64: a raw IEEE single to fixed point with `bits` fraction bits. */
int64_t voodoo_float_fixed(uint32_t data, int bits);

/* The card's PCI configuration space: device VOODOO_PCI_DEV (measured), word = register
 * number (byte offset / 4, 0 to 63). voodoo_cfg_read returns 0 with the word in *out, or -1
 * with the reason in *why when the word's value is not one MAME showed; voodoo_cfg_write
 * returns 0 having stored the value of one of the words the game wrote in MAME, or -1 with
 * the reason for any other word, BAR0 included. The loader's PCI slots
 * (runtime/loader/slots.c) turn a -1 into their stop. voodoo_cfg_map_bar0: BAR0 = base, as
 * slot 0x110 maps the card (the only BAR0 write the game makes). */
#define VOODOO_PCI_DEV 8u
int voodoo_cfg_read(uint32_t word, uint32_t *out, const char **why);
int voodoo_cfg_write(uint32_t word, uint32_t value, const char **why);
void voodoo_cfg_map_bar0(uint32_t base);

/* --- the gun (gun.c) --------------------------------------------------------------------- */

#define GUN_PHYS 0x16800000u
#define GUN_SIZE 0x20u
uint32_t gun_read(cpu_t *c, uint32_t off, int size);
void gun_write(cpu_t *c, uint32_t off, int size, uint32_t value);

/* --- the sound FIFO and 0x17F00000 (misc.c) --------------------------------------------- */

#define SNDFIFO_PHYS 0x13000000u
#define ARST_PHYS 0x17F00000u
void sndfifo_write(cpu_t *c, int size, uint32_t value);
uint64_t sndfifo_writes(void);
uint32_t arst_read(cpu_t *c, int size);
void arst_write(cpu_t *c, int size, uint32_t value);

#endif
