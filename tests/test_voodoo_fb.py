"""The Voodoo model's frame buffer, drawing and texture unit (runtime/hw/voodoo_fb.c and
voodoo_tmu.c, through voodoo_null.c), against docs/analysis/hardware-usage.md, "The Voodoo
model", and against MAME: `voodoo-ramp.txt` replayed at its recorded times, with the PCI
configuration writes of `voodoo-reads.txt`, must give every read the value MAME returned.

The C runs in tests/harness/hw_driver.c, as for tests/test_hw.py. The expected pixels of the
synthetic tests come from the card's rules written out here (the fill rule in single
precision, the dither), not from the C.
"""
import collections
import struct
import unittest

from tests import voodoo_timing as vt
from tests.gamedata import requires_file
from tests.test_hw import CHAIN, HwTestCase, cfg_write, fact_lines, mem, rd, start_chain, wr
from tests.test_loader import open_

VOODOO = 0xA8000000
LFB = 0xA8400000
TEX = 0xA8800000
FBZCOLORPATH, FOGMODE, ALPHAMODE, FBZMODE, LFBMODE = 0x104, 0x108, 0x10C, 0x110, 0x114
CLIPLR, CLIPTB, COLOR1, ZACOLOR = 0x118, 0x11C, 0x148, 0x130
TRIANGLE, FASTFILL = 0x080, 0x124
TEXMODE, TLOD, TEXBASE = 0x300, 0x304, 0x30C
DITHER4 = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5]
# hw.h's DROP_ reasons, in order
REASONS = ["subpixel", "gouraud", "color_combine", "chroma", "stipple", "depth", "alpha_mask",
           "aux_write", "alpha_test", "alpha_blend", "fog", "tmus_disabled", "tmu_off",
           "perspective", "trilinear", "mipmap", "bilinear", "tex_format", "tex_combine",
           "lfb_mode", "no_buffer"]
RGB565_LOCAL = 0x08241A00  # the TMU check's textureMode: RGB 565, point, the TMU's texel


def reg(off, value):
    return wr(4, VOODOO + off, value)


def f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def round_coord(v):
    fl = int(v // 1)
    return fl + (1 if v - fl > 0.5 else 0)


def coverage(verts):
    """The pixels MAME's render_triangle covers (poly.h), in single precision."""
    vs = sorted(((f32(x / 16), f32(y / 16)) for x, y in verts), key=lambda v: v[1])
    (x1, y1), (x2, y2), (x3, y3) = vs

    def slope(ax, ay, bx, by):
        return 0.0 if ay == by else f32(f32(bx - ax) / f32(by - ay))

    d12, d13, d23 = slope(x1, y1, x2, y2), slope(x1, y1, x3, y3), slope(x2, y2, x3, y3)
    out = set()
    for y in range(round_coord(y1), round_coord(y3)):
        fy = f32(y + 0.5)
        sx = f32(x1 + f32(f32(fy - y1) * d13))
        ex = f32(x1 + f32(f32(fy - y1) * d12)) if fy < y2 else f32(x2 + f32(f32(fy - y2) * d23))
        a, b = sorted((round_coord(sx), round_coord(ex)))
        out |= {(x, y) for x in range(a, b)}
    return out


def dither565(color, x, y):
    """The card's 4 x 4 dither of an 8-bit RGB colour at (x, y) (hardware-usage.md)."""
    r, g, b = color >> 16 & 255, color >> 8 & 255, color & 255
    d = DITHER4[(y & 3) * 4 + (x & 3)]
    return ((((r << 1) - (r >> 4) + (r >> 7) + d) >> 4) << 11 |
            (((g << 2) - (g >> 4) + (g >> 6) + d) >> 4) << 5 |
            (((b << 1) - (b >> 4) + (b >> 7) + d) >> 4))


def plain565(color):
    return (color >> 19 & 31) << 11 | (color >> 10 & 63) << 5 | (color >> 3 & 31)


def setup(tiles=8, pages=64, fbiinit3=0):
    """fbiInit writes enabled; rows of tiles x 64 pixels, buffers of `pages` 4 KB pages."""
    return cfg_write(0x40, 1) + [reg(0x214, tiles << 4), reg(0x218, pages << 11),
                                 reg(0x21C, fbiinit3), reg(LFBMODE, 0), reg(ALPHAMODE, 0),
                                 reg(FOGMODE, 0)]


def fill(color, w=64, h=64, fbz=0x200):
    return [reg(FBZMODE, fbz), reg(CLIPLR, w), reg(CLIPTB, h), reg(COLOR1, color),
            reg(FASTFILL, 0)]


def triangle(verts, color=None, fbz=0x200, colorpath=0x2):
    cmds = [reg(FBZMODE, fbz), reg(FBZCOLORPATH, colorpath)]
    if color is not None:
        cmds.append(reg(COLOR1, color))
    for i, (x, y) in enumerate(verts):
        cmds += [reg(0x008 + 8 * i, x), reg(0x00C + 8 * i, y)]
    return cmds + [reg(TRIANGLE, 0)]


def pixel(x, y):
    return rd(2, LFB + 2 * (y << 10 | x))


def buffers(front, back, depth):
    """The front and back buffers and the aux buffer filled whole (512 x 256, the layout of
    setup()), so that every pixel of each is known."""
    return (fill(front, 512, 256) + fill(back, 512, 256, fbz=0x4200)
            + [reg(ZACOLOR, depth)] + fill(0, 512, 256, fbz=0x400))


def read_in(buffer, x, y):
    """lfbMode read select 0 front, 1 back, 2 aux, then the pixel."""
    return [reg(LFBMODE, buffer << 6), pixel(x, y)]


RAMP_TRIANGLE = [(0, 0), (0x240, 0), (0, 0x240)]  # (0,0), (36,0), (0,36) in 12.4


class FrameBufferTest(HwTestCase):
    def test_flat_triangle(self):
        """The ramp's triangle with color1 0x808080 and no dither covers exactly the pixels
        of the card's fill rule (630; (34,0) and (0,34) in, (35,0) and (0,35) out) with
        0x8410; under 4 x 4 dither the block at (4,4) holds the card's pattern."""
        cover = coverage(RAMP_TRIANGLE)
        self.assertEqual(len(cover), 630)
        for p, inside in (((34, 0), True), ((35, 0), False), ((0, 34), True), ((0, 35), False)):
            self.assertEqual(p in cover, inside, p)
        cmds = setup() + fill(0) + triangle(RAMP_TRIANGLE, 0x808080)
        cmds += [reg(LFBMODE, 0)] + [pixel(x, y) for y in range(40) for x in range(40)]
        out, _ = self.drive(cmds)
        got = self.rds(out)
        drawn = {(x, y) for y in range(40) for x in range(40) if got[y * 40 + x] == 0x8410}
        self.assertEqual(drawn, cover)
        self.assertEqual({v for v in got if v != 0x8410}, {0})
        out, _ = self.drive(setup() + fill(0) + triangle(RAMP_TRIANGLE, 0x808080, fbz=0x300)
                            + [pixel(x, y) for y in range(4, 8) for x in range(4, 8)])
        self.assertEqual(self.rds(out), [dither565(0x808080, x, y)
                                         for y in range(4, 8) for x in range(4, 8)])

    def test_lfb_aliasing(self):
        """Address bit 21 (row + 1,024) maps to the same pixel; the front buffer's row 256 is
        the back buffer's row 0 (256 rows of 512 pixels a buffer); a pixel beyond the 2 MB
        reads 0xFFFF and a write there is lost (the memory test's layout: 13 tiles, 247
        pages, the aux buffer's row 100); a pixel never written stops."""
        a = LFB + 2 * (5 << 10 | 10)
        out, _ = self.drive(setup() + [wr(2, a, 0x1234), rd(2, a + 0x200000), reg(LFBMODE, 0x10),
                                       wr(2, LFB, 0xBEEF), reg(LFBMODE, 0), pixel(0, 256)])
        self.assertEqual(self.rds(out), [0x1234, 0xBEEF])
        out, _ = self.drive(cfg_write(0x40, 1) + [reg(0x214, 0x002011D8), reg(0x218, 0x1877B8E2),
                                                  reg(LFBMODE, 0x8F), wr(2, 0xA8432100, 0xDEAD),
                                                  rd(2, 0xA8432100), rd(4, 0xA8432100)])
        self.assertEqual(self.rds(out), [0xFFFF, 0xFFFFFFFF])
        self.stops(setup() + [pixel(3, 3)], "physical 0x08401806", "not written since reset")

    def test_non_flat_triangle_dropped(self):
        """A triangle whose modes enable a feature the model does not implement (a depth
        test, alpha blending, bilinear filtering: modes attract mode uses) is counted under
        that reason and dropped, and its draw buffer is flagged (decision 2: per buffer): any
        LFB read of that buffer stops, also far outside the triangle, while the other colour
        buffer reads; a fastfill of the whole buffer clears the flag, a partial one does not.
        A depth-writing mode (fbzMode bit 10) flags the aux buffer too."""
        tex = [reg(TLOD, 0), reg(TEXBASE, 0), wr(4, TEX, 0x22221111)]
        cases = [("depth", [], 0x210, 0x2),
                 ("alpha_blend", [reg(ALPHAMODE, 0x10)], 0x200, 0x2),
                 ("bilinear", tex + [reg(TEXMODE, RGB565_LOCAL | 0x4)], 0x200, 0x08000001)]
        for reason, modes, fbz, colorpath in cases:
            with self.subTest(reason=reason):
                before = setup() + buffers(0x204080, 0x408020, 0x5555) + modes
                draw = triangle(RAMP_TRIANGLE, 0x808080, fbz=fbz, colorpath=colorpath)
                out, _ = self.drive(before + draw + ["dropped"] + read_in(1, 40, 40)
                                    + read_in(2, 40, 40))
                counts = [int(n) for n in out.all("dropped")[0]]
                self.assertEqual(counts, [int(r == reason) for r in REASONS])
                self.assertEqual(self.rds(out), [plain565(0x408020), 0x5555])
                self.stops(before + draw + read_in(0, 300, 200), "does not render")
                after = [reg(ALPHAMODE, 0)]
                out, _ = self.drive(before + draw + after + fill(0x204080, 512, 256)
                                    + read_in(0, 1, 1))
                self.assertEqual(self.rds(out), [plain565(0x204080)])
                self.stops(before + draw + after + fill(0x204080) + read_in(0, 1, 1),
                           "does not render")
        before = setup() + buffers(0x204080, 0x408020, 0x5555)
        draw = triangle(RAMP_TRIANGLE, 0x808080, fbz=0x610)
        self.stops(before + draw + read_in(2, 40, 40), "does not render")
        # draw buffer 2: nothing is written, the drop is counted under its own reason
        out, _ = self.drive(before + triangle(RAMP_TRIANGLE, 0x808080, fbz=0x8200)
                            + ["dropped"] + read_in(0, 1, 1))
        self.assertEqual([int(n) for n in out.all("dropped")[0]],
                         [int(r == "no_buffer") for r in REASONS])
        self.assertEqual(self.rds(out), [plain565(0x204080)])

    def test_dropped_lfb_write_flags_buffers(self):
        """An LFB write in a mode the model does not implement (format 1, 16-bit RGB 555;
        format 4, 32-bit RGB; format 12, depth and RGB 565) flags its write-select buffer and
        the aux buffer: reads of both stop anywhere in them; the other colour buffer reads."""
        before = setup() + buffers(0x204080, 0x408020, 0x5555)
        for fmt, size in ((1, 2), (4, 4), (12, 4)):
            with self.subTest(fmt=fmt):
                value = 0x5678 if size == 2 else 0x12345678
                write = [reg(LFBMODE, fmt), wr(size, LFB + 0x100, value)]
                out, _ = self.drive(before + write + ["dropped"] + read_in(1, 40, 40))
                self.assertEqual(int(out.all("dropped")[0][REASONS.index("lfb_mode")]), 1)
                self.assertEqual(self.rds(out), [plain565(0x408020)])
                self.stops(before + write + read_in(0, 300, 200), "does not render")
                self.stops(before + write + read_in(2, 300, 200), "does not render")

    def test_textured_triangle(self):
        """A point-sampled RGB 565 texel passed through by the texture combine is the pixel:
        S steps one texel a pixel with dSdX = 1.0; a texel never written marks its pixel; with
        trexInit1 bit 18 the TMU sends its configuration word (0x11 in blue) instead."""
        base = (setup() + fill(0) + [reg(TEXMODE, RGB565_LOCAL), reg(TLOD, 0), reg(TEXBASE, 0),
                                     wr(4, TEX, 0x22221111), reg(0x054, 0x40000)])
        draw = triangle([(0, 0), (0x40, 0), (0, 0x40)], fbz=0x200, colorpath=0x08000001)
        out, _ = self.drive(base + draw + [pixel(0, 0), pixel(1, 0)])
        self.assertEqual(self.rds(out), [0x1111, 0x2222])
        self.stops(base + draw + [pixel(2, 0)], "texel not written since reset")
        out, _ = self.drive(base + [reg(0x320, 0x0004F420)] + draw + [pixel(0, 0), pixel(2, 0)])
        self.assertEqual(self.rds(out), [0x0002, 0x0002])

    def test_alternate_map(self):
        """With fbiInit3 bit 0 set, a write through the alternate map (address bit 21) reaches
        the register of the alternate order: raw index 0x18 is dSdX; with the bit clear it is
        the raw register (dRdY), so S does not step. Counted as alternate-map writes."""
        def run(fbiinit3):
            cmds = (setup(fbiinit3=fbiinit3) + fill(0)
                    + [reg(TEXMODE, RGB565_LOCAL), reg(TLOD, 0), reg(TEXBASE, 0),
                       wr(4, TEX, 0x22221111), reg(0x054, 0), wr(4, VOODOO + 0x200060, 0x40000)]
                    + triangle([(0, 0), (0x40, 0), (0, 0x40)], colorpath=0x08000001)
                    + [pixel(0, 0), pixel(1, 0), "counts 18"])
            out, _ = self.drive(cmds)
            self.assertEqual(out.all("counts")[0][:2], ["0", "1"])
            return self.rds(out)
        self.assertEqual(run(1), [0x1111, 0x2222])
        self.assertEqual(run(0), [0x1111, 0x1111])
        # the same write as a DMA record's destination (GT64010 channel 0) reaches dSdX too
        dma = mem(CHAIN, 4, 0x002375D0, 0x08200060, 0, 0x40000) + open_("pci:", 2) \
            + start_chain(0x002375C0)
        out, _ = self.drive(setup(fbiinit3=1) + fill(0)
                            + [reg(TEXMODE, RGB565_LOCAL), reg(TLOD, 0), reg(TEXBASE, 0),
                               wr(4, TEX, 0x22221111), reg(0x054, 0)] + dma
                            + triangle([(0, 0), (0x40, 0), (0, 0x40)], colorpath=0x08000001)
                            + [pixel(0, 0), pixel(1, 0), "counts 18"])
        self.assertEqual(out.all("counts")[0][:2], ["0", "1"])
        self.assertEqual(self.rds(out), [0x1111, 0x2222])


@requires_file("mame", "m2-facts", "voodoo-ramp.txt")
@requires_file("mame", "m2-facts", "voodoo-reads.txt")
class FrameBufferFactsTest(HwTestCase):
    """voodoo-ramp.txt (GAME.EXE's own Voodoo accesses from its entry to 2 s after it) merged
    in time order with voodoo-reads.txt's PCI configuration writes and replayed at the
    recorded times (virtual time = capture time - the entry's, hardware-usage.md). Every read
    is compared with what MAME returned."""

    replay = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        events = []
        for f in fact_lines("voodoo-reads.txt"):
            if len(f) == 10 and f[1] == "0x0F8":
                events.append((int(f[0]), 0, ("C", int(f[2], 16), int(f[8], 16), 0)))
        last = 0
        frames = []
        for f in fact_lines("voodoo-ramp.txt"):
            last = int(f[0])
            events.append((last, 1, (f[1], int(f[2], 16), int(f[3], 16), int(f[4], 16))))
            frames.append((last, int(f[5])))
        events = [e for e in sorted(events, key=lambda e: (e[0], e[1])) if e[0] <= last]
        cls.frames = frames
        cls.events = events

    def run_replay(self):
        if FrameBufferFactsTest.replay is not None:
            return FrameBufferFactsTest.replay
        cmds, want = [], []
        for t, _, (kind, a, b, pc) in self.events:
            cmds.append(f"at {t - vt.ENTRY_NS}")
            if kind == "C":
                cmds += cfg_write(a, b)
                continue
            lfb = 0x08400000 <= a < 0x08800000
            # the memory test's LFB accesses are halfwords, every other is a word (read:
            # 0x8015C0A8 to 0x8015C224 use lh and sh)
            size = 2 if lfb and 0x8015C000 <= pc < 0x8015C300 else 4
            if kind == "W":
                cmds.append(wr(size, 0xA0000000 | a, b))
            else:
                cmds.append(rd(size, 0xA0000000 | a))
                want.append((t, a, b, pc))
        cmds.append("vsync")
        out, _ = self.drive(cmds)
        FrameBufferFactsTest.replay = (want, self.rds(out), out.all("vsync"))
        return FrameBufferFactsTest.replay

    def mismatches(self, pick):
        want, got, _ = self.run_replay()
        self.assertEqual(len(got), len(want))
        rows = [(t, a, m, g) for (t, a, m, pc), g in zip(want, got) if pick(t, a, pc)]
        bad = [(t, f"0x{a:08X}", f"0x{m:X}", f"0x{g:X}") for t, a, m, g in rows if m != g]
        return len(rows), bad

    @staticmethod
    def lfb(a):
        return 0x08400000 <= a < 0x08800000

    def test_memory_test_like_mame(self):
        """The 9 LFB reads of the start-up memory test (0x8015C0D4 to 0x8015C224, 6.553 s)."""
        n, bad = self.mismatches(lambda t, a, pc: self.lfb(a) and 0x8015C000 <= pc < 0x8015C300)
        self.assertEqual((n, bad), (9, []))

    def test_dither_ramp_like_mame(self):
        """The 8,192 LFB reads of the dither ramp (0x8015B384 to 0x8015B390): 256 flat
        triangles, 32 reads each."""
        n, bad = self.mismatches(lambda t, a, pc: self.lfb(a) and 0x8015B384 <= pc <= 0x8015B390)
        self.assertEqual((n, bad[:5]), (8192, []))

    def test_tmu_check_like_mame(self):
        """The 36 LFB reads of the TMU check after the ramp (0x8015B664 to 0x8015B670, the
        TMU's configuration word; 0x8015B948 to 0x8015B954, the texture-memory size)."""
        n, bad = self.mismatches(lambda t, a, pc: self.lfb(a) and (
            0x8015B664 <= pc <= 0x8015B670 or 0x8015B948 <= pc <= 0x8015B954))
        self.assertEqual((n, bad), (36, []))

    def test_reads_like_mame_full(self):
        """Every read of voodoo-ramp.txt returns its measured value: registers, status (busy,
        FIFO free, retrace, displayed buffer) and LFB; 274,967 reads, 8,237 of them LFB."""
        n, bad = self.mismatches(lambda t, a, pc: True)
        kinds = collections.Counter(
            "lfb" if self.lfb(a) else "status" if a == 0x08000000 else "register"
            for t, a, m, pc in self.run_replay()[0])
        self.assertEqual(n, 274967)
        self.assertEqual(kinds["lfb"], 9 + 8192 + 36)
        self.assertEqual(bad[:10], [])

    def test_retrace_phase_like_mame(self):
        """The model's retrace, derived from the video timing and MAME's default screen,
        agrees with the capture's frame counter: MAME's screen counts a frame at its VBLANK
        end, 21 rows after each retrace start (hardware-usage.md), and every frame step after
        the timing is programmed falls in the interval between the two accesses around it."""
        _, _, vsync = self.run_replay()
        next_ps, length, frame = (int(x) for x in vsync[0])
        programmed = 6567466366
        rows21 = 21 * (frame // 291)
        steps = [(t0, t1) for (t0, f0), (t1, f1) in zip(self.frames, self.frames[1:])
                 if f1 != f0 and t1 > programmed]
        self.assertGreater(len(steps), 20)
        first, _, _ = vt.retrace_after(programmed * 1000)
        self.assertEqual((next_ps - first) % frame, 0)
        for t0, t1 in steps:
            k = (t1 * 1000 - first) // frame
            step = first + k * frame + rows21
            if step > t1 * 1000:
                step -= frame
            self.assertTrue(t0 * 1000 < step <= t1 * 1000 + 1000, (t0, t1, step))
        self.assertTrue(251354 <= length // 1000 <= 254120, length)


if __name__ == "__main__":
    unittest.main()
