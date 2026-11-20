/* voodoo_tmu.c: the Voodoo's texture unit (TMU0, the board's only one): its registers, its
 * 4 MB of texture memory, and the texel the triangle fill (voodoo_fb.c) asks it for.
 *
 * docs/analysis/hardware-usage.md, "The Voodoo model" ("Texture unit"). Everything here is
 * from MAME 0.276's model of the card (voodoo.cpp: reg_starts_w and the other S/T/W
 * writers, internal_texture_w; voodoo_render.cpp: rasterizer_texture::recompute and
 * fetch_texel, rasterizer_params::compute), checked against the capture where it has data:
 * the start-up TMU check (`voodoo-ramp.txt`, 6.567 s) reads back the TMU's configuration word
 * and a texel of a texture-memory sizing test. The model implements what that check uses
 * and no more: point sampling from one LOD (lod_min = lod_max) of an RGB 565 texture without
 * perspective, the texture combine that passes the texel through, and the configuration send
 * (trexInit1 bit 18). tmu_capability names the first feature a triangle's modes enable that
 * is not among these.
 */
#include <string.h>

#include "hw.h"

/* Texture memory: set_tmumem(4, 0) in MAME's seattle.cpp (one TMU, 4 MB). */
#define TEX_BYTES (4u << 20)
#define TEX_MASK (TEX_BYTES - 1u)
/* texBaseAddr is in units of 8 bytes, 20 bits (MAME's tmu_state: basemask 0xFFFFF, shift 3) */
#define BASE_MASK 0xFFFFFu
#define BASE_SHIFT 3
/* What the TMU sends as its texel under trexInit1 bit 18: MAME's tmu_config for one TMU,
 * 0x11 in blue (voodoo.cpp, device_start); the check's pixels, 0x0002 and 0x0003 under the
 * 4 x 4 dither, follow from it and from no other blue of 0x10 to 0x12 (measured). */
#define TMU_CONFIG_WORD 0x11u

/* register numbers (offset / 4) */
#define R_STARTS 0x0Du
#define R_STARTT 0x0Eu
#define R_STARTW 0x0Fu
#define R_DSDX 0x15u
#define R_DTDX 0x16u
#define R_DWDX 0x17u
#define R_DSDY 0x1Du
#define R_DTDY 0x1Eu
#define R_DWDY 0x1Fu
#define R_FSTARTS 0x2Du
#define R_FSTARTT 0x2Eu
#define R_FSTARTW 0x2Fu
#define R_FDSDX 0x35u
#define R_FDTDX 0x36u
#define R_FDWDX 0x37u
#define R_FDSDY 0x3Du
#define R_FDTDY 0x3Eu
#define R_FDWDY 0x3Fu
#define R_TEXMODE 0xC0u
#define R_TLOD 0xC1u
#define R_TEXBASE 0xC3u /* to 0xC6: texBaseAddr, _1, _2, _3_8 */
#define R_TREXINIT1 0xC8u

#define TREXINIT1_SEND_CONFIG (1u << 18)

static struct {
    uint32_t reg[NREGS_VOODOO];
    /* S and T in 32.32, W in 16.32 (MAME: 14.18 << 14, 2.30 << 2, floats to .32) */
    int64_t starts, startt, startw, dsdx, dtdx, dwdx, dsdy, dtdy, dwdy;
    uint8_t mem[TEX_BYTES];
    uint8_t known[TEX_BYTES / 8]; /* bytes written since reset */
} t;

void tmu_reset(void)
{
    memset(&t, 0, sizeof t);
}

/* A raw IEEE single to a fixed-point value with `bits` fraction bits, as MAME's
 * float_to_int64 does: the mantissa shifted by the exponent, 0 below range, the maximum
 * above, then the sign. */
int64_t voodoo_float_fixed(uint32_t data, int bits)
{
    int exponent = (int)((data >> 23) & 0xFFu) - 127 - 23 + bits;
    uint64_t m = (data & 0x7FFFFFu) | 0x800000u;
    if (exponent < 0)
        m = exponent > -64 ? m >> -exponent : 0;
    else
        m = exponent < 64 ? m << exponent : 0x7FFFFFFFFFFFFFFFu;
    return (data & 0x80000000u) ? (int64_t)(0 - m) : (int64_t)m;
}

static int64_t shifted(uint32_t value, int by)
{
    return (int64_t)((uint64_t)(int64_t)(int32_t)value << by);
}

void tmu_reg_write(uint32_t n, uint32_t value)
{
    switch (n) {
    case R_STARTS: t.starts = shifted(value, 14); return;
    case R_STARTT: t.startt = shifted(value, 14); return;
    case R_DSDX: t.dsdx = shifted(value, 14); return;
    case R_DTDX: t.dtdx = shifted(value, 14); return;
    case R_DSDY: t.dsdy = shifted(value, 14); return;
    case R_DTDY: t.dtdy = shifted(value, 14); return;
    case R_STARTW: t.startw = shifted(value, 2); return;
    case R_DWDX: t.dwdx = shifted(value, 2); return;
    case R_DWDY: t.dwdy = shifted(value, 2); return;
    case R_FSTARTS: t.starts = voodoo_float_fixed(value, 32); return;
    case R_FSTARTT: t.startt = voodoo_float_fixed(value, 32); return;
    case R_FDSDX: t.dsdx = voodoo_float_fixed(value, 32); return;
    case R_FDTDX: t.dtdx = voodoo_float_fixed(value, 32); return;
    case R_FDSDY: t.dsdy = voodoo_float_fixed(value, 32); return;
    case R_FDTDY: t.dtdy = voodoo_float_fixed(value, 32); return;
    case R_FSTARTW: t.startw = voodoo_float_fixed(value, 32); return;
    case R_FDWDX: t.dwdx = voodoo_float_fixed(value, 32); return;
    case R_FDWDY: t.dwdy = voodoo_float_fixed(value, 32); return;
    default:
        if (n < NREGS_VOODOO)
            t.reg[n] = value;
    }
}

/* --- the texture layout (MAME's rasterizer_texture::recompute) --------------------------- */

struct layout {
    uint32_t lodoffset[9];
    uint32_t lodmask, wmask, hmask;
    int32_t lodmin, lodmax;
};

static uint32_t field(uint32_t v, int lo, int bits)
{
    return (v >> lo) & ((1u << bits) - 1u);
}

static void layout(struct layout *l)
{
    uint32_t lod = t.reg[R_TLOD], mode = t.reg[R_TEXMODE];
    uint32_t bppscale = field(mode, 8, 4) >> 3, base;
    l->lodmin = (int32_t)(field(lod, 0, 6) << 6);
    l->lodmax = (int32_t)(field(lod, 6, 6) << 6);
    l->lodmask = 0x1FFu;
    if (field(lod, 19, 1)) /* lod_tsplit: odd or even LODs only */
        l->lodmask = field(lod, 18, 1) ? 0x0AAu : 0x155u;
    l->wmask = l->hmask = 0xFFu;
    if (field(lod, 20, 1)) /* lod_s_is_wider */
        l->hmask >>= field(lod, 21, 2);
    else
        l->wmask >>= field(lod, 21, 2);
    base = (t.reg[R_TEXBASE] & BASE_MASK) << BASE_SHIFT;
    l->lodoffset[0] = base & TEX_MASK;
    if (field(lod, 24, 1) && field(lod, 28, 4) == 0) { /* tmultibaseaddr */
        for (int i = 1; i <= 3; i++)
            l->lodoffset[i] = ((t.reg[R_TEXBASE + (uint32_t)i] & BASE_MASK) << BASE_SHIFT) &
                              TEX_MASK;
        base = (t.reg[R_TEXBASE + 3] & BASE_MASK) << BASE_SHIFT;
    } else {
        for (int i = 1; i <= 3; i++) {
            if (l->lodmask & (1u << (i - 1)))
                base += (((l->wmask >> (i - 1)) + 1) * ((l->hmask >> (i - 1)) + 1)) << bppscale;
            l->lodoffset[i] = base & TEX_MASK;
        }
    }
    for (int i = 4; i <= 8; i++) {
        if (l->lodmask & (1u << (i - 1))) {
            uint32_t size = ((l->wmask >> (i - 1)) + 1) * ((l->hmask >> (i - 1)) + 1);
            base += (size < 4 ? 4 : size) << bppscale;
        }
        l->lodoffset[i] = base & TEX_MASK;
    }
}

/* --- texture memory writes (MAME's internal_texture_w) ------------------------------------ */

const char *tmu_tex_write(uint32_t off, uint32_t value)
{
    struct layout l;
    uint32_t w = off >> 2, lod = field(w, 15, 4), tt = field(w, 7, 8), mode = t.reg[R_TEXMODE];
    uint32_t tlod = t.reg[R_TLOD], bpt = field(mode, 8, 4) < 8 ? 1 : 2, ts, addr;
    if (field(w, 19, 2) != 0)
        return NULL; /* TMU1 or TMU2, which the board does not have: MAME drops it */
    if (field(tlod, 27, 1))
        return "a texture write under tLOD bit 27 (direct write), which MAME's model does "
               "not handle";
    ts = (w << ((field(mode, 31, 1) && bpt == 1) ? 2 : 1)) & 0xFFu;
    if (lod > 8)
        return NULL; /* MAME ignores it */
    if (field(tlod, 25, 1)) /* tdata_swizzle */
        value = (value >> 24) | ((value >> 8) & 0xFF00u) | ((value << 8) & 0xFF0000u) |
                (value << 24);
    if (field(tlod, 26, 1)) /* tdata_swap */
        value = (value >> 16) | (value << 16);
    layout(&l);
    addr = (l.lodoffset[lod] + ((bpt * (tt * ((l.wmask >> lod) + 1) + ts)) & ~3u)) & TEX_MASK;
    for (uint32_t i = 0; i < 4; i++) {
        t.mem[addr + i] = (uint8_t)(value >> (8 * i));
        t.known[(addr + i) / 8] |= (uint8_t)(1u << ((addr + i) % 8));
    }
    return NULL;
}

/* --- what a triangle may ask for ------------------------------------------------------------ */

int tmu_config_send(void)
{
    return (t.reg[R_TREXINIT1] & TREXINIT1_SEND_CONFIG) != 0;
}

/* The first feature the TMU modes enable that the model does not implement, or VFB_DRAWABLE.
 * Called only when the colour path uses the texel. */
int tmu_capability(void)
{
    uint32_t mode = t.reg[R_TEXMODE], lod = t.reg[R_TLOD];
    if (field(lod, 0, 6) >= 32)
        return DROP_TMU_OFF; /* lod_min >= 32: MAME runs no TMU and the texel is 0 */
    if (tmu_config_send())
        return VFB_DRAWABLE;
    if (field(mode, 0, 1))
        return DROP_PERSPECTIVE;
    if (field(mode, 30, 1))
        return DROP_TRILINEAR;
    if (field(lod, 0, 6) != field(lod, 6, 6))
        return DROP_MIPMAP;
    if (field(mode, 2, 1)) /* the one LOD is lod_min, so the magnification filter applies */
        return DROP_BILINEAR;
    if (field(mode, 8, 4) != 10)
        return DROP_TEX_FORMAT;
    /* the RGB texture combine must give the TMU's own texel: zero other, no subtraction,
     * factor zero not reversed, add c_local, not inverted (Glide's "local") */
    if (field(mode, 12, 9) != 0x41u) /* bits 12 to 20 */
        return DROP_TEX_COMBINE;
    return VFB_DRAWABLE;
}

/* --- the texel at a pixel (MAME's fetch_texel, point sampled) ------------------------------ */

void tmu_iter_start(struct tmu_iter *it, int32_t dx, int32_t dy)
{
    it->s = (double)(int64_t)((uint64_t)t.starts + (uint64_t)dy * (uint64_t)t.dsdy +
                              (uint64_t)dx * (uint64_t)t.dsdx);
    it->t = (double)(int64_t)((uint64_t)t.startt + (uint64_t)dy * (uint64_t)t.dtdy +
                              (uint64_t)dx * (uint64_t)t.dtdx);
    it->w = (double)(int64_t)((uint64_t)t.startw + (uint64_t)dy * (uint64_t)t.dwdy +
                              (uint64_t)dx * (uint64_t)t.dwdx);
    it->ds = (double)t.dsdx;
    it->dt = (double)t.dtdx;
    it->dw = (double)t.dwdx;
}

void tmu_iter_step(struct tmu_iter *it)
{
    it->s += it->ds;
    it->t += it->dt;
    it->w += it->dw;
}

/* A double to s32 as the x86-64 build of MAME converts it: truncation, and 0x80000000 for a
 * value out of range or not a number. */
static int32_t to_s32(double v)
{
    if (!(v > -2147483649.0 && v < 2147483648.0))
        return INT32_MIN;
    return (int32_t)v;
}

static int tex_known(uint32_t addr)
{
    return t.known[addr / 8] >> (addr % 8) & 1u;
}

static uint8_t expand(uint32_t v, int bits)
{
    return (uint8_t)(bits == 5 ? (v << 3) | (v >> 2) : (v << 2) | (v >> 4));
}

int tmu_texel(const struct tmu_iter *it, uint8_t rgb[3])
{
    struct layout l;
    uint32_t mode = t.reg[R_TEXMODE], addr, texel;
    int32_t s, tc, lod, ilod, smax, tmax;
    if (tmu_config_send()) {
        rgb[0] = rgb[1] = 0;
        rgb[2] = TMU_CONFIG_WORD;
        return 0;
    }
    layout(&l);
    s = to_s32(it->s * (1.0 / (1 << 24))); /* 32.32 to 24.8 */
    tc = to_s32(it->t * (1.0 / (1 << 24)));
    if (field(mode, 3, 1) && it->w < 0) /* clamp_neg_w */
        s = tc = 0;
    lod = l.lodmin; /* lod_min = lod_max (tmu_capability): the clamp leaves only it */
    ilod = lod >> 8;
    ilod += (int32_t)((~l.lodmask >> ilod) & 1u);
    smax = (int32_t)(l.wmask >> ilod);
    tmax = (int32_t)(l.hmask >> ilod);
    s >>= ilod + 8;
    tc >>= ilod + 8;
    if (field(mode, 6, 1))
        s = s < 0 ? 0 : s > smax ? smax : s;
    if (field(mode, 7, 1))
        tc = tc < 0 ? 0 : tc > tmax ? tmax : tc;
    s &= smax;
    tc &= tmax;
    tc *= smax + 1;
    addr = (l.lodoffset[ilod] + 2u * (uint32_t)(tc + s)) & TEX_MASK;
    if (!tex_known(addr) || !tex_known(addr + 1))
        return -1;
    texel = t.mem[addr] | (uint32_t)t.mem[(addr + 1) & TEX_MASK] << 8;
    rgb[0] = expand(texel >> 11, 5);
    rgb[1] = expand((texel >> 5) & 0x3Fu, 6);
    rgb[2] = expand(texel & 0x1Fu, 5);
    return 0;
}
