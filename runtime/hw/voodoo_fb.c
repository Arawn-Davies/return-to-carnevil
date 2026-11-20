/* voodoo_fb.c: the Voodoo's frame-buffer memory and what writes it: LFB reads and writes,
 * fastfillCMD, the triangle fill (triangleCMD and ftriangleCMD) and the buffer swap.
 *
 * docs/analysis/hardware-usage.md, "The Voodoo model", is the reference; each rule there is
 * marked measured or from MAME's model (MAME 0.276: voodoo.cpp, voodoo_render.cpp/.h,
 * poly.h). The memory is host memory, 2 MB of 16-bit pixels (set_fbmem(2) in MAME's
 * seattle.cpp). The register values come from voodoo_null.c (voodoo_reg).
 *
 * A triangle is drawn only if every pipeline feature its mode registers enable is one the
 * model implements (vfb_capability: flat colour1 or a TMU texel passed through, dither,
 * clipping, the y origin; nothing else). Any other triangle is counted under the first
 * feature missing and dropped, and the pixels it would have covered are marked; an LFB
 * read of a marked pixel, or of one never written since reset (the power-on content is not
 * measured), returns an error for the caller to stop on. A modelled write of a pixel clears
 * its mark.
 */
#include <math.h>
#include <string.h>

#include "hw.h"

#define FB_BYTES (2u << 20)
#define FB_PIXELS (FB_BYTES / 2)
#define LFB_STRIDE 10 /* x is the low 10 bits of an LFB pixel index (MAME: m_lfb_stride) */
#define TRIANGLE_SETUP_CLOCKS 100u /* MAME's voodoo.h */
#define NO_BUFFER 0xFFFFFFFFu

/* register numbers (offset / 4) */
#define R_VERTEX 0x02u /* to 0x07: Ax, Ay, Bx, By, Cx, Cy */
#define R_FBZCOLORPATH 0x41u
#define R_FOGMODE 0x42u
#define R_ALPHAMODE 0x43u
#define R_FBZMODE 0x44u
#define R_LFBMODE 0x45u
#define R_CLIPLR 0x46u
#define R_CLIPTB 0x47u
#define R_ZACOLOR 0x4Cu
#define R_COLOR1 0x52u
#define R_FBIINIT3 0x87u

/* The 4 x 4 and (MAME's) 2 x 2 dither matrices, by (y & 3) * 4 + (x & 3): a fact about the
 * card, reproduced as MAME's model gives it (voodoo_render.cpp, s_dither_matrix_4x4 and
 * _2x2). */
static const uint8_t DITHER4[16] = {0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5};
static const uint8_t DITHER2[16] = {8, 10, 8, 10, 11, 9, 11, 9, 8, 10, 8, 10, 11, 9, 11, 9};

static const char *const DROP_NAMES[DROP_COUNT] = {
    [DROP_SUBPIXEL] = "subpixel adjust",
    [DROP_GOURAUD] = "iterated (Gouraud) colour",
    [DROP_COLOR_COMBINE] = "colour combine",
    [DROP_CHROMA] = "chroma key",
    [DROP_STIPPLE] = "stipple",
    [DROP_DEPTH] = "depth test",
    [DROP_ALPHA_MASK] = "alpha mask",
    [DROP_AUX_WRITE] = "depth or alpha buffer write",
    [DROP_ALPHA_TEST] = "alpha test",
    [DROP_ALPHA_BLEND] = "alpha blending",
    [DROP_FOG] = "fog",
    [DROP_TMUS_DISABLED] = "TMUs disabled (fbiInit3 bit 6)",
    [DROP_TMU_OFF] = "TMU0 off (lod_min of 32 or more)",
    [DROP_PERSPECTIVE] = "perspective-corrected texturing",
    [DROP_TRILINEAR] = "trilinear filtering",
    [DROP_MIPMAP] = "more than one LOD (mipmapping)",
    [DROP_BILINEAR] = "bilinear filtering",
    [DROP_TEX_FORMAT] = "a texture format other than RGB 565",
    [DROP_TEX_COMBINE] = "a texture combine other than the TMU's own texel",
    [DROP_LFB_MODE] = "an LFB write mode other than raw RGB 565 or depth",
};

static struct {
    uint16_t px[FB_PIXELS];
    uint8_t known[FB_PIXELS / 8];   /* written by a modelled operation since reset */
    uint8_t dropped[FB_PIXELS / 8]; /* may hold drawing the model dropped */
    uint32_t rgboffs[3], auxoffs;   /* pixel offsets, NO_BUFFER for none */
    uint32_t rowpixels;
    int front, back;
    uint32_t drops[DROP_COUNT];
} f;

const char *vfb_drop_name(int reason)
{
    return reason >= 0 && reason < DROP_COUNT ? DROP_NAMES[reason] : "?";
}

uint32_t vfb_dropped(int reason)
{
    return reason >= 0 && reason < DROP_COUNT ? f.drops[reason] : 0;
}

/* The layout at power-on, before any fbiInit1 or fbiInit2 write (MAME's device_start): every
 * colour buffer at 0, no aux buffer, 0 pixels per row, front 0, back 1. */
void vfb_reset(void)
{
    memset(&f, 0, sizeof f);
    f.auxoffs = NO_BUFFER;
    f.back = 1;
}

/* recompute_video_memory: fbiInit1 bits 4 to 7 are the row in 64-pixel tiles; fbiInit2 bits
 * 11 to 19 the size of a colour buffer in 4 KB pages; fbiInit2 bit 4 three colour buffers
 * and no aux, else two and an aux buffer after them; offsets past the memory are clamped to
 * its last byte. */
void vfb_layout(uint32_t fbiinit1, uint32_t fbiinit2)
{
    uint32_t pages = (fbiinit2 >> 11) & 0x1FFu, bytes[4];
    int triple = (fbiinit2 >> 4) & 1;
    bytes[0] = 0;
    bytes[1] = pages * 0x1000u;
    bytes[2] = triple ? 2 * pages * 0x1000u : NO_BUFFER;
    bytes[3] = triple ? NO_BUFFER : 2 * pages * 0x1000u;
    for (int i = 0; i < 4; i++) {
        if (bytes[i] != NO_BUFFER && bytes[i] > FB_BYTES - 1)
            bytes[i] = FB_BYTES - 1;
        if (i < 3)
            f.rgboffs[i] = bytes[i] == NO_BUFFER ? NO_BUFFER : bytes[i] / 2;
    }
    f.auxoffs = bytes[3] == NO_BUFFER ? NO_BUFFER : bytes[3] / 2;
    if (!triple) {
        if (f.front == 2)
            f.front = 0;
        if (f.back == 2)
            f.back = 0;
    }
    f.rowpixels = ((fbiinit1 >> 4) & 0xFu) * 64;
}

void vfb_swap(void)
{
    int buffers = f.rgboffs[2] == NO_BUFFER ? 2 : 3;
    f.front = (f.front + 1) % buffers;
    f.back = (f.front + 1) % buffers;
}

int vfb_front(void)
{
    return f.front;
}

/* --- pixels ------------------------------------------------------------------------------ */

static int bit(const uint8_t *map, uint32_t p)
{
    return map[p / 8] >> (p % 8) & 1;
}

static void put(uint32_t p, uint16_t v)
{
    f.px[p] = v;
    f.known[p / 8] |= (uint8_t)(1u << (p % 8));
    f.dropped[p / 8] &= (uint8_t)~(1u << (p % 8));
}

static void mark(uint32_t p)
{
    f.dropped[p / 8] |= (uint8_t)(1u << (p % 8));
}

/* draw_buffer_indirect / lfb_buffer_indirect: 0 front, 1 back, 2 the aux buffer (LFB reads
 * only), NO_BUFFER otherwise */
static uint32_t buffer(uint32_t select, int aux)
{
    if (select == 0)
        return f.rgboffs[f.front];
    if (select == 1)
        return f.rgboffs[f.back];
    if (select == 2 && aux)
        return f.auxoffs;
    return NO_BUFFER;
}

/* The pixel index of (x, row) in the buffer at base, or -1 outside the memory. */
static int64_t at(uint32_t base, int64_t row, int64_t x)
{
    int64_t p = (int64_t)base + row * (int64_t)f.rowpixels + x;
    return p >= 0 && p < (int64_t)FB_PIXELS ? p : -1;
}

/* The card's dither (MAME's dither_helper, "hardware-verified"): 8-bit red and blue to 5
 * bits as (2v - v/16 + v/128 + d) / 16, green to 6 bits as (4v - v/16 + v/64 + d) / 16,
 * with d from the matrix fbzMode bit 11 selects; without dither (fbzMode bit 8 clear) the
 * top bits. */
static uint16_t dither(uint32_t fbzmode, int64_t x, int64_t y, const uint8_t rgb[3])
{
    uint32_t r = rgb[0], g = rgb[1], b = rgb[2], d;
    if (!(fbzmode & 0x100u))
        return (uint16_t)((r >> 3) << 11 | (g >> 2) << 5 | (b >> 3));
    d = ((fbzmode >> 11) & 1 ? DITHER2 : DITHER4)[(y & 3) * 4 + (x & 3)];
    r = ((r << 1) - (r >> 4) + (r >> 7) + d) >> 4;
    g = ((g << 2) - (g >> 4) + (g >> 6) + d) >> 4;
    b = ((b << 1) - (b >> 4) + (b >> 7) + d) >> 4;
    return (uint16_t)(r << 11 | g << 5 | b);
}

static void color1(uint8_t rgb[3])
{
    uint32_t c = voodoo_reg(R_COLOR1);
    rgb[0] = (uint8_t)(c >> 16), rgb[1] = (uint8_t)(c >> 8), rgb[2] = (uint8_t)c;
}

static int32_t yorigin(void)
{
    return (int32_t)(voodoo_reg(R_FBIINIT3) >> 22);
}

/* --- LFB ---------------------------------------------------------------------------------- */

/* internal_lfb_r: the word offset doubled is the pixel index (x the low 10 bits, y the rest,
 * & 0x3FF), lfbMode bit 13 flips y about the y origin, bits 6 and 7 select the buffer; the
 * pair at (x, x + 1) is read, and 0xFFFFFFFF if the pair's second pixel is at or past the
 * end of the memory or there is no buffer; bits 15 and 16 swap the words and bytes. A
 * halfword access returns the half that address bit 1 selects (little-endian). */
int vfb_lfb_read(uint32_t off, int size, uint32_t *value, const char **why)
{
    uint32_t lfb = voodoo_reg(R_LFBMODE), pix = (off >> 2) << 1, base, data, used;
    int64_t x = pix & ((1u << LFB_STRIDE) - 1), y = (pix >> LFB_STRIDE) & 0x3FFu, p;
    int lane = (off >> 1) & 1, ws = (lfb >> 15) & 1, bs = (lfb >> 16) & 1;
    if (lfb & 0x2000u)
        y = yorigin() - y;
    base = buffer((lfb >> 6) & 3u, 1);
    p = base == NO_BUFFER ? -1 : (int64_t)base + y * (int64_t)f.rowpixels + x;
    if (p < 0 && base != NO_BUFFER) {
        *why = "an LFB read below the frame-buffer memory (the y origin flip gave a negative "
               "row), which MAME's model does not define";
        return -1;
    }
    if (base == NO_BUFFER || p + 1 >= (int64_t)FB_PIXELS) {
        data = 0xFFFFFFFFu;
    } else {
        for (int k = 0; k < 2; k++) {
            used = (uint32_t)p + (uint32_t)(size == 4 ? k : (lane ^ ws ^ bs));
            if (bit(f.dropped, used)) {
                *why = "an LFB read of a pixel that holds drawing the model does not render "
                       "(a triangle or LFB write it counted and dropped, or a texel never "
                       "written since reset)";
                return -1;
            }
            if (!bit(f.known, used)) {
                *why = "an LFB read of a pixel not written since reset (the frame buffer's "
                       "power-on content is not measured)";
                return -1;
            }
            if (size != 4)
                break;
        }
        data = f.px[p] | (uint32_t)f.px[p + 1] << 16;
    }
    if (ws)
        data = (data << 16) | (data >> 16);
    if (bs)
        data = (data >> 24) | ((data >> 8) & 0xFF00u) | ((data << 8) & 0xFF0000u) | (data << 24);
    *value = size == 4 ? data : (data >> (16 * lane)) & 0xFFFFu;
    return 0;
}

/* internal_lfb_w in raw mode for the two formats the game writes: 0 (RGB 565, expanded to 8
 * bits by bit replication, then dithered as fbzMode says, into the buffer of lfbMode bits 4
 * and 5) and 15 (two 16-bit depths into the aux buffer, nothing under fbzMode bit 18). Two
 * pixels per word, the halfword's lane by address bit 1; a pixel past the end of the memory
 * is not written; write buffer 2 or 3 writes nothing. Any other mode is counted, dropped and
 * marked. */
const char *vfb_lfb_write(uint32_t off, int size, uint32_t value)
{
    uint32_t lfb = voodoo_reg(R_LFBMODE), fbz = voodoo_reg(R_FBZMODE), fmt = lfb & 0xFu;
    uint32_t pix = (off >> 2) << 1, base = buffer((lfb >> 4) & 3u, 0);
    int64_t x0 = pix & ((1u << LFB_STRIDE) - 1), y = (pix >> LFB_STRIDE) & 0x3FFu;
    int first = size == 4 ? 0 : (off >> 1) & 1, last = size == 4 ? 1 : first;
    if (lfb & 0x2000u)
        y = yorigin() - y;
    if (base == NO_BUFFER)
        return NULL;
    if ((fmt != 0 && fmt != 15) || (lfb & 0x1F00u)) { /* pipeline, lanes, swaps */
        f.drops[DROP_LFB_MODE]++;
        for (int k = first; k <= last; k++) {
            int64_t p = at(fmt == 15 && f.auxoffs != NO_BUFFER ? f.auxoffs : base, y, x0 + k);
            if (p >= 0)
                mark((uint32_t)p);
        }
        return NULL;
    }
    for (int k = first; k <= last; k++) {
        uint32_t v = size == 4 ? (value >> (16 * k)) & 0xFFFFu : value & 0xFFFFu;
        int64_t p;
        if (fmt == 0) {
            uint8_t rgb[3] = {(uint8_t)((v >> 11 << 3) | (v >> 13)),
                              (uint8_t)(((v >> 5) & 0x3Fu) << 2 | ((v >> 9) & 3u)),
                              (uint8_t)((v & 0x1Fu) << 3 | ((v >> 2) & 7u))};
            p = (int64_t)base + y * (int64_t)f.rowpixels + x0 + k;
            if (p < 0)
                return "an LFB write below the frame-buffer memory (the y origin flip gave a "
                       "negative row), which MAME's model does not define";
            if (p < (int64_t)FB_PIXELS)
                put((uint32_t)p, dither(fbz, x0 + k, y, rgb));
        } else if (f.auxoffs != NO_BUFFER && !(fbz & 0x40000u)) {
            p = (int64_t)f.auxoffs + y * (int64_t)f.rowpixels + x0 + k;
            if (p < 0)
                return "an LFB depth write below the frame-buffer memory, which MAME's model "
                       "does not define";
            if (p < (int64_t)FB_PIXELS)
                put((uint32_t)p, (uint16_t)v);
        }
    }
    return NULL;
}

/* --- fastfillCMD ---------------------------------------------------------------------------- */

/* reg_fastfill_w: the rectangle [clipLeft, clipRight) x [clipLowY, clipHighY) of the draw
 * buffer (fbzMode bits 14 and 15; 2 and 3 fill nothing), its RGB with color1 dithered by the
 * unflipped y if fbzMode bit 9, its aux pixels with zaColor's low half if bit 10 and there
 * is an aux buffer; rows flipped about the y origin under fbzMode bit 17. Nothing, in no
 * time, if neither bit is set. Returns the clocks: 2 pixels per clock. */
uint32_t vfb_fastfill(const char **why)
{
    uint32_t fbz = voodoo_reg(R_FBZMODE), lr = voodoo_reg(R_CLIPLR), tb = voodoo_reg(R_CLIPTB);
    int64_t left = (lr >> 16) & 0x3FFu, right = lr & 0x3FFu;
    int64_t top = (tb >> 16) & 0x3FFu, bottom = tb & 0x3FFu;
    uint32_t base = buffer((fbz >> 14) & 3u, 0), za = voodoo_reg(R_ZACOLOR) & 0xFFFFu, pixels;
    uint8_t rgb[3];
    if (base == NO_BUFFER || !(fbz & 0x600u))
        return 0;
    /* render_tile sorts the corners by y, then gives up when the first corner's x is
     * greater than the second's */
    if (bottom < top) {
        int64_t swap = top;
        top = bottom, bottom = swap;
        if (right > left)
            return 0;
        swap = left, left = right, right = swap;
    } else if (left > right) {
        return 0;
    }
    color1(rgb);
    for (int64_t y = top; y < bottom; y++) {
        int64_t row = (fbz & 0x20000u) ? yorigin() - y : y;
        for (int64_t x = left; x < right; x++) {
            if (fbz & 0x200u) {
                int64_t p = at(base, row, x);
                if (p < 0) {
                    *why = "a fastfill pixel outside the frame-buffer memory, which MAME's "
                           "model does not define";
                    return 0;
                }
                put((uint32_t)p, dither(fbz, x, y, rgb));
            }
            if ((fbz & 0x400u) && f.auxoffs != NO_BUFFER) {
                int64_t p = at(f.auxoffs, row, x);
                if (p < 0) {
                    *why = "a fastfill depth pixel outside the frame-buffer memory, which "
                           "MAME's model does not define";
                    return 0;
                }
                put((uint32_t)p, (uint16_t)za);
            }
        }
    }
    pixels = (uint32_t)((bottom - top) * (right - left));
    return pixels / 2;
}

/* --- triangles ------------------------------------------------------------------------------ */

/* The capability rule: the first pipeline feature the mode registers enable that the model
 * does not implement, in the order of MAME's pixel pipeline, or VFB_DRAWABLE. */
int vfb_capability(void)
{
    uint32_t cp = voodoo_reg(R_FBZCOLORPATH), fbz = voodoo_reg(R_FBZMODE);
    uint32_t alpha = voodoo_reg(R_ALPHAMODE), fog = voodoo_reg(R_FOGMODE);
    uint32_t rgbselect = cp & 3u;
    if (cp & (1u << 26))
        return DROP_SUBPIXEL;
    if (rgbselect == 0)
        return DROP_GOURAUD;
    /* other colour passed through: no zero, subtract, factor, reverse, add or invert
     * (bits 8 to 16), and a select that exists */
    if (rgbselect == 3 || (cp & 0x1FF00u))
        return DROP_COLOR_COMBINE;
    if (fbz & 0x2u)
        return DROP_CHROMA;
    if (fbz & 0x4u)
        return DROP_STIPPLE;
    if (fbz & 0x10u)
        return DROP_DEPTH;
    if (fbz & 0x2000u)
        return DROP_ALPHA_MASK;
    if (alpha & 0x1u)
        return DROP_ALPHA_TEST;
    if (fog & 0x1u)
        return DROP_FOG;
    if (alpha & 0x10u)
        return DROP_ALPHA_BLEND;
    if (fbz & 0x400u)
        return DROP_AUX_WRITE;
    if (rgbselect == 1) {
        if (voodoo_reg(R_FBIINIT3) & 0x40u)
            return DROP_TMUS_DISABLED;
        return tmu_capability();
    }
    return VFB_DRAWABLE;
}

/* poly.h's round_coordinate: down, and up only above one half */
static int32_t round_coord(float v)
{
    float fl = floorf(v);
    return (int32_t)fl + (v - fl > 0.5f ? 1 : 0);
}

static float vertex(uint32_t n)
{
    return (float)(int16_t)(uint16_t)voodoo_reg(n) * (1.0f / 16.0f);
}

/* reg_triangle_w: MAME's render_triangle in single precision without clipping in the
 * polygon manager: the vertices sorted by y; scanlines from round(y1) to round(y3), sampled
 * at y + 0.5; each from the long edge v1 to v3 against v1 to v2 or v2 to v3, the extent
 * [round(start), round(stop)) swapped if reversed, empty if equal. Then fbzMode bit 0 clips
 * to [clipLeft, clipRight) and [clipLowY, clipHighY) on the flipped row. Returns the clocks:
 * 100 for setup and one per pixel of the unclipped extents. */
uint32_t vfb_triangle(const char **why)
{
    uint32_t fbz = voodoo_reg(R_FBZMODE), lr = voodoo_reg(R_CLIPLR), tb = voodoo_reg(R_CLIPTB);
    uint32_t base = buffer((fbz >> 14) & 3u, 0), pixels = 0;
    int32_t ax = (int16_t)(uint16_t)voodoo_reg(R_VERTEX);
    int32_t ay = (int16_t)(uint16_t)voodoo_reg(R_VERTEX + 1);
    float v[3][2], *v1 = v[0], *v2 = v[1], *v3 = v[2], *tmp;
    float d12, d13, d23;
    int32_t y1, y3, cl = (lr >> 16) & 0x3FFu, cr = lr & 0x3FFu;
    int32_t ct = (tb >> 16) & 0x3FFu, cb = tb & 0x3FFu;
    int cap = vfb_capability(), textured = (voodoo_reg(R_FBZCOLORPATH) & 3u) == 1;
    uint8_t rgb[3];
    if (base == NO_BUFFER)
        return TRIANGLE_SETUP_CLOCKS;
    for (int i = 0; i < 3; i++) {
        v[i][0] = vertex(R_VERTEX + 2u * (uint32_t)i);
        v[i][1] = vertex(R_VERTEX + 2u * (uint32_t)i + 1);
    }
    if (v2[1] < v1[1])
        tmp = v1, v1 = v2, v2 = tmp;
    if (v3[1] < v2[1]) {
        tmp = v2, v2 = v3, v3 = tmp;
        if (v2[1] < v1[1])
            tmp = v1, v1 = v2, v2 = tmp;
    }
    y1 = round_coord(v1[1]);
    y3 = round_coord(v3[1]);
    d12 = v2[1] == v1[1] ? 0.0f : (v2[0] - v1[0]) / (v2[1] - v1[1]);
    d13 = v3[1] == v1[1] ? 0.0f : (v3[0] - v1[0]) / (v3[1] - v1[1]);
    d23 = v3[1] == v2[1] ? 0.0f : (v3[0] - v2[0]) / (v3[1] - v2[1]);
    if (cap != VFB_DRAWABLE)
        f.drops[cap]++;
    if (!textured)
        color1(rgb);
    for (int32_t y = y1; y < y3; y++) {
        float fy = (float)y + 0.5f, sx = v1[0] + (fy - v1[1]) * d13, ex;
        int32_t a, b, row;
        if (fy < v2[1])
            ex = v1[0] + (fy - v1[1]) * d12;
        else
            ex = v2[0] + (fy - v2[1]) * d23;
        a = round_coord(sx), b = round_coord(ex);
        if (a > b) {
            int32_t swap = a;
            a = b, b = swap;
        }
        if (a >= b)
            a = b = 0;
        pixels += (uint32_t)(b - a);
        row = (fbz & 0x20000u) ? yorigin() - y : y;
        if (fbz & 0x1u) {
            if (row < ct || row >= cb || a >= cr)
                continue;
            if (b > cr)
                b = cr;
            if (a < cl)
                a = cl;
        }
        if (!(fbz & 0x200u) && cap == VFB_DRAWABLE)
            continue; /* nothing written: no RGB write and no aux write (capability) */
        {
            struct tmu_iter it;
            if (textured && cap == VFB_DRAWABLE)
                tmu_iter_start(&it, a - (ax >> 4), y - (ay >> 4));
            for (int32_t x = a; x < b; x++) {
                int64_t p = at(base, row, x);
                if (cap != VFB_DRAWABLE) {
                    if ((fbz & 0x200u) && p >= 0)
                        mark((uint32_t)p);
                    if ((fbz & 0x400u) && f.auxoffs != NO_BUFFER &&
                        (p = at(f.auxoffs, row, x)) >= 0)
                        mark((uint32_t)p);
                    continue;
                }
                if (p < 0) {
                    *why = "a triangle pixel outside the frame-buffer memory, which MAME's "
                           "model does not define";
                    return TRIANGLE_SETUP_CLOCKS + pixels;
                }
                if (textured && tmu_texel(&it, rgb) != 0)
                    mark((uint32_t)p); /* the texel was never written since reset */
                else
                    put((uint32_t)p, dither(fbz, x, row, rgb));
                if (textured)
                    tmu_iter_step(&it);
            }
        }
    }
    return TRIANGLE_SETUP_CLOCKS + pixels;
}
