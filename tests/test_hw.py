"""The bus (runtime/core/bus.c), the devices GAME.EXE touches directly (runtime/hw/hw.h) and
the loader stand-ins that reach hardware (the PCI configuration slots, the disk slots and
`pci:`'s GT64010 DMA channel 0: runtime/loader), against docs/analysis/hardware-usage.md
("GAME.EXE", the two "Dynamic check" sections, "Measured values the runtime answers with")
and docs/analysis/loader-services.md (sections 1, 3, 4 and 8, open questions 5 to 8).

The C runs in tests/harness/hw_driver.c (its top comment lists the commands), built like the
loader driver (tests/test_loader.py). The tests that compare with the MAME measurements of
`mame/m2-facts/` or read the game's disk skip without them; the disk is opened read-only with
an overlay in a temporary directory.
"""
import collections
import os
import unittest

from tests import test_loader
from tests.gamedata import requires_file, work_path
from tests.test_disk_c import SECTOR, fs_partition, pattern, raw_header
from tests.test_loader import BUF, CALLER_RA, DATA, M1, call, ioctl, open_

HW_DRIVER = os.path.join(test_loader.REPO, "tests", "harness", "hw_driver.c")

PROBE = 0x80300000
SP_TOP = 0x80700000
PROC = 0x80206000
DEVP = 0x802374B0
VOODOO = 0xA8000000
STATUS_IDLE = 0x0FFFF03F       # hardware-usage.md, "Measured values ...": the idle status
STATUS_RETRACE = 0x0FFFF07F    # ... with the vertical-retrace bit
RETRACE_NS = 252737
VBLANK_PERIOD_NS = 18467220
PART_TABLE = 0x800B7160
CALLBACK_RA = 0x800124D4
COOKIE = 0x803F75E0
# the power-on values of fbiInit0 to fbiInit4 (registers 0x210, 0x214, 0x218, 0x21C, 0x200)
FBIINIT_POWER_ON = {0x210: 0x410, 0x214: 0x00201102, 0x218: 0x80000040, 0x21C: 0x001E4000,
                    0x200: 0x1}
# the registers whose reads hardware-usage.md classifies as plain storage
STORAGE = (0x104, 0x108, 0x10C, 0x110, 0x114, 0x118, 0x11C, 0x140, 0x144, 0x148,
           0x200, 0x210, 0x214, 0x218, 0x21C)
GUN = {0x00: 0x00, 0x04: 0x48, 0x08: 0x00, 0x0C: 0x02,
       0x10: 0x00, 0x14: 0x48, 0x18: 0x00, 0x1C: 0x02}
# the DMA channel 0 start sequence of dma0.txt: control, next record, control, enable
CHAIN = 0x802375C0


def rd(size, addr):
    return f"rd {size:X} {addr & M1:X}"


def wr(size, addr, value):
    return f"wr {size:X} {addr & M1:X} {value & M1:X}"


def mem(addr, *words):
    return [f"mem {addr + 4 * i:08X} {w & M1:08X}" for i, w in enumerate(words)]


def gt(offset, value, at=BUF):
    """ioctl 0x7301 on fd 6 with its {offset, value} pair at `at`."""
    return mem(at, offset, value) + [ioctl(6, 0x7301, at)]


def start_chain(head):
    return (gt(0x840, 0x0DC0) + gt(0x830, head) + gt(0x840, 0x2DC0) + gt(0x840, 0x1DC0))


def cfg_write(reg, value, dev=8):
    return mem(DATA, value) + [call(0x0F8, reg, dev, DATA)]


class HwTestCase(test_loader.LoaderTestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = test_loader.build_driver(HW_DRIVER, "hw_driver")

    def rds(self, out):
        return [int(v[0], 16) for v in out.all("rd")]

    def disk(self, nblocks=40):
        """A synthetic one-partition image in the temporary directory: (commands, bytes)."""
        part = fs_partition([("A.BIN", 0x1008, 4)], [([4, 5], pattern(0x1008, 5))],
                            nblocks=nblocks)
        sectors = 8 + len(part) // SECTOR
        image = raw_header([len(part) // SECTOR], sectors) + bytes(7 * SECTOR) + part
        raw = self.path("disk.raw")
        with open(raw, "wb") as f:
            f.write(image)
        return [f"img {raw} {self.path('disk.ovl')}"], image


class PciTest(HwTestCase):
    def test_pci_probe(self):
        """The four probes of loader-calls.md (devices 6 to 9, vendor 0x121A) find the Voodoo
        only at device 8 (open question 7); slot 0x110 with index 0 returns 0xA8000000 and
        stores 8, with index 1 returns 0 and stores 10."""
        cmds = [call(0x108, 0x121A, 0xFFFF, 0, dev) for dev in (6, 7, 8, 9)]
        cmds += [f"r 1D {SP_TOP:X}"] + mem(SP_TOP + 16, 0) + [call(0x110, 0x121A, 0xFFFF, 0,
                                                                   DEVP), f"dump {DEVP:X} 1"]
        cmds += mem(SP_TOP + 16, 1) + [call(0x110, 0x121A, 0xFFFF, 0, DEVP), f"dump {DEVP:X} 1"]
        out, _ = self.drive(cmds)
        self.assertEqual(out.v0s(), [0, 0, 1, 0, 0xA8000000, 0])
        self.assertEqual(out.dumps(), [[8], [10]])

    def test_pci_config(self):
        """Slots 0x0F0 and 0x0F8 on device 8 (section 8): words 0, 2 and 4 read as MAME
        returned them (word 4, BAR0, once slot 0x110 has mapped it); writes of the words the
        game wrote (byte offsets 0x04, 0x40, 0x44, 0x48, 0xC0, 0xE0) are stored and return 1;
        another device, an unread word, a write of any other word (an ID word, BAR0 through
        slot 0x0F8, a BAR-sizing 0xFFFFFFFF, word 0x3F) and BAR0 before the mapping stop."""
        out, _ = self.drive([f"r 1D {SP_TOP:X}"] + mem(SP_TOP + 16, 0)
                            + [call(0x110, 0x121A, 0xFFFF, 0, DEVP)]
                            + [call(0x0F0, reg, 8, DATA + 4 * i)
                               for i, reg in enumerate((0x10, 0x08, 0x02, 0x00))]
                            + [f"dump {DATA:X} 4"]
                            + [c for reg, v in ((0x04, 2), (0xE0, 0), (0x40, 1), (0x44, 0),
                                                (0x48, 0), (0xC0, 0))
                               for c in cfg_write(reg, v)])
        self.assertEqual(out.v0s(), [0xA8000000, 1, 1, 1, 1] + [1] * 6)
        self.assertEqual(out.dumps(), [[0x08000008, 0x00000002, 0x0001121A, 0x0001121A]])
        self.stops([call(0x0F0, 0, 7, DATA)], "0x0F0", "device 7")
        self.stops(cfg_write(0x40, 1, dev=9), "0x0F8", "device 9")
        self.stops([call(0x0F0, 0x40, 8, DATA)], "0x0F0", "word 0x10")
        self.stops(cfg_write(0x00, 0x12345678), "0x0F8", "word 0x00", "0x12345678")
        self.stops(cfg_write(0x10, 0xFFFFFFFF), "0x0F8", "word 0x04", "0xFFFFFFFF")
        self.stops(cfg_write(0x10, 0x08000000), "0x0F8", "word 0x04", "0x08000000")
        self.stops(cfg_write(0xFC, 1), "0x0F8", "word 0x3F")
        self.stops([call(0x0F0, 0x10, 8, DATA)], "0x0F0", "BAR0", "before")

    def test_pci_probe_other(self):
        """A probe or a mapping for another vendor, or of a device other than 6 to 9, stops:
        what an absent device answers was measured for vendor 0x121A only."""
        self.stops([call(0x108, 0x1234, 0, 0, 6)], "0x108", "vendor 0x1234")
        self.stops([call(0x108, 0x121A, 0, 0, 3)], "0x108", "device 3")
        self.stops([f"r 1D {SP_TOP:X}"] + mem(SP_TOP + 16, 0)
                   + [call(0x110, 0x1234, 0, 0, DEVP)], "0x110", "vendor 0x1234")


class VoodooTest(HwTestCase):
    def test_lfb_readback(self):
        """LFB writes of halfwords and words are accepted; an LFB read stops, even of a pixel
        just written, because the card's frame-buffer addresses alias (the start-up memory
        test read 0xFFFF where it had written 0xDEAD: hardware-usage.md, "Voodoo frame buffer
        reads") and the sink does not model its memory layout; a texture write is dropped and
        counted; a texture read and a byte access stop."""
        out, _ = self.drive([wr(2, 0xA8432064, 0xDEAD), wr(4, 0xA8400000, 0x12345678),
                             wr(4, 0xA8800000, 0x11111111), wr(4, 0xA8FFFFFC, 2), "counts 0"])
        self.assertEqual(out.all("counts"), [["0", "0", "2", "0"]])
        self.stops([wr(2, 0xA8432064, 0xDEAD), rd(2, 0xA8432064)], "physical 0x08432064",
                   "LFB read", "alias")
        self.stops([wr(4, 0xA8800000, 1), rd(4, 0xA8800000)], "physical 0x08800000",
                   "texture")
        self.stops([wr(1, 0xA8432064, 1)], "physical 0x08432064", "1 byte")

    def test_voodoo_status(self):
        """status (kind 2): FIFO free at its maximum, not busy, no swap pending, and the
        vertical-retrace bit for the first RETRACE_NS of each VBLANK period, by the virtual
        clock (from time 0 until VBLANK starts, then from each VBLANK); a write stops; after a
        swapbufferCMD write, written directly or by a DMA record, a status read stops (the
        displayed-buffer field is not modelled yet)."""
        after = RETRACE_NS // 100 + 1  # check points of 100 ns
        to_next = (VBLANK_PERIOD_NS - after * 100) // 100 + 1
        out, _ = self.drive([rd(4, VOODOO), f"checks {after - 1:X}", rd(4, VOODOO),
                             "checks 1", rd(4, VOODOO), f"checks {to_next:X}", rd(4, VOODOO),
                             "checks 9C40", "vblank", rd(4, VOODOO), f"checks {after:X}",
                             rd(4, VOODOO), f"checks {to_next:X}", rd(4, VOODOO)])
        self.assertEqual(self.rds(out), [STATUS_RETRACE, STATUS_RETRACE, STATUS_IDLE,
                                         STATUS_RETRACE, STATUS_RETRACE, STATUS_IDLE,
                                         STATUS_RETRACE])
        self.stops([wr(4, VOODOO, 0)], "physical 0x08000000", "status")
        self.stops([rd(4, VOODOO), wr(4, VOODOO + 0x128, 0), rd(4, VOODOO)],
                   "physical 0x08000000", "swapbufferCMD", "displayed-buffer")
        swap = mem(CHAIN, 4, 0x002375D0, 0x08000128, 0, 0)
        self.stops(swap + open_("pci:", 2) + start_chain(0x002375C0) + [rd(4, VOODOO)],
                   "physical 0x08000000", "displayed-buffer")

    def test_voodoo_registers(self):
        """Kind 1: a register reads back what was last written, and counts its writes; the
        fbiInit registers start at their measured power-on values, and a write of one while
        PCI initEnable bit 0 is clear stops; fbiInit2 reads the DAC while initEnable bit 2 is
        set (DacTest; with no DAC read latched it stops) and fbiInit3 then stops; both read
        their storage when it is clear; a read before any write, of a register the game never
        read, through chip bits or the alternate map, and after an alternate-map write, stops."""
        out, _ = self.drive([rd(4, VOODOO + r) for r in sorted(FBIINIT_POWER_ON)]
                            + [wr(4, VOODOO + 0x104, 0x0C000035), rd(4, VOODOO + 0x104),
                               wr(4, VOODOO + 0x104, 1), rd(4, VOODOO + 0x104), "counts 41"]
                            + cfg_write(0x40, 3) + [wr(4, VOODOO + 0x218, 0x186200E0),
                                                    rd(4, VOODOO + 0x218),
                                                    wr(4, VOODOO + 0xB00, 5), "counts C0",
                                                    wr(4, VOODOO + 0x200088, 7), "counts 22"])
        self.assertEqual(self.rds(out), [FBIINIT_POWER_ON[r] for r in sorted(FBIINIT_POWER_ON)]
                         + [0x0C000035, 1, 0x186200E0])
        self.assertEqual(out.all("counts"), [["2", "0", "0", "0"], ["1", "0", "0", "0"],
                                             ["0", "1", "0", "0"]])
        self.stops([rd(4, VOODOO + 0x104)], "physical 0x08000104", "never written")
        self.stops([wr(4, VOODOO + 0x128, 0), rd(4, VOODOO + 0x128)], "physical 0x08000128",
                   "not one the game read")
        for reg in (0x218, 0x21C):
            with self.subTest(reg=hex(reg)):
                self.stops(cfg_write(0x40, 5) + [rd(4, VOODOO + reg)],
                           f"physical 0x{0x08000000 + reg:08X}", "DAC")
        self.stops([wr(4, VOODOO + 0x104, 1), wr(4, VOODOO + 0x200088, 7),
                    rd(4, VOODOO + 0x104)], "physical 0x08000104", "alternate")
        self.stops([wr(4, VOODOO + 0x904, 1), rd(4, VOODOO + 0x904)], "physical 0x08000904",
                   "chip")
        self.stops([rd(4, VOODOO + 0x200104)], "physical 0x08200104", "alternate")
        self.stops([wr(4, VOODOO + 0x100104, 1)], "physical 0x08100104", "bit 20")
        self.stops([wr(4, VOODOO + 0x4104, 1)], "physical 0x08004104", "bits 14 to 19")
        self.stops([wr(2, VOODOO + 0x104, 1)], "physical 0x08000104", "2 bytes")
        self.stops([rd(1, VOODOO + 0x210)], "physical 0x08000210", "1 byte")
        for reg in sorted(FBIINIT_POWER_ON):
            with self.subTest(write=hex(reg)):
                self.stops([wr(4, VOODOO + reg, 1)], f"physical 0x{0x08000000 + reg:08X}",
                           "initEnable bit 0")
        self.stops(cfg_write(0x40, 1) + cfg_write(0x40, 0) + [wr(4, VOODOO + 0x210, 1)],
                   "initEnable bit 0")


DACDATA = 0x22C
FBIINIT2 = 0x218
# The dacData writes and fbiInit2 reads of the game's DAC routines (hardware-usage.md, "The
# DAC"; read): None is a read of fbiInit2. ICS detect 0x8015906C: DAC register 7 = 0x0B, 0x01,
# 0x07 in turn, each followed by two read requests of register 5, each with a read.
ICS_DETECT = [0x70B, 0xD00, None, 0xD00, None, 0x701, 0xD00, None, 0xD00, None,
              0x707, 0xD00, None, 0xD00, None]


def pll_set(r4, m, n):
    """The PLL routines 0x8015A1D0 (register 4 = 0x0A) and 0x80159A28 (register 4 = 0x00):
    register 7 = 0x0E, a read request of register 5 and a read, then register 4, register 5
    twice (m and n, from a table), register 4 = 0x0E and register 5 again, with the byte read
    & 0xEF (or'ed with more bits in 0x80159A28); here that byte is the measured 0."""
    return [0x70E, 0xD00, None, 0x400 | r4, 0x500 | m, 0x500 | n, 0x40E, 0x500]


def dac_cmds(seq):
    return [rd(4, VOODOO + FBIINIT2) if w is None else wr(4, VOODOO + DACDATA, w) for w in seq]


class DacTest(HwTestCase):
    def test_dac_readback(self):
        """dacData stores its byte in the DAC register of bits 8 to 10 without the read flag
        (bit 11), and with it latches the register's value, register 5 answering 0x55, 0x71 or
        0x79 while register 7 holds 0x01, 0x07 or 0x0B; fbiInit2 reads the latch while
        initEnable bit 2 is set and its own value when it is clear. The game's ICS detect and
        PLL sequences give the measured 0x79, 0x79, 0x55, 0x55, 0x71, 0x71, then 0 (register
        5's power-on value) and 0 again after the PLL routine wrote register 5; a written
        register reads back; dacData writes are counted."""
        out, _ = self.drive(cfg_write(0x40, 5) + dac_cmds(ICS_DETECT) + cfg_write(0x40, 3)
                            + [rd(4, VOODOO + FBIINIT2)] + cfg_write(0x40, 5)
                            + dac_cmds(pll_set(0x0A, 0x12, 0x34)) + cfg_write(0x40, 3)
                            + cfg_write(0x40, 5) + dac_cmds(pll_set(0x00, 0x56, 0x78))
                            + dac_cmds([0x303, 0xB00, None]) + cfg_write(0x40, 3)
                            + ["counts 8B"])
        self.assertEqual(self.rds(out), [0x79, 0x79, 0x55, 0x55, 0x71, 0x71, 0x80000040,
                                         0x00, 0x00, 0x03])
        self.assertEqual(out.all("counts"), [["25", "0", "0", "0"]])
        self.stops(cfg_write(0x40, 5) + [rd(4, VOODOO + FBIINIT2)], "DAC", "no DAC read")
        self.stops(cfg_write(0x40, 5) + dac_cmds([0xB00, None]), "DAC register 3",
                   "never written")
        self.stops(cfg_write(0x40, 5) + dac_cmds([0x70B, 0xD00]) + [rd(4, VOODOO + 0x21C)],
                   "fbiInit3", "DAC")
        # register 5 before register 7 has a value: register 7's power-on value is unmeasured
        self.stops(cfg_write(0x40, 5) + dac_cmds([0xD00]), "DAC register 5", "register 7")


class DmaTest(HwTestCase):
    def test_dma0_chain(self):
        """A two-record chain in guest memory, started as dma0.txt's sequence does, copies
        both records (one to a Voodoo register, one to RAM) at the enable write, and the bit-4
        handler runs once after dma0_delay_ns of the chain, with a0 = 1."""
        recs = mem(CHAIN, 4, 0x002375D0, 0x0800010C, 0x002375E0, 0x12345678) + \
            mem(CHAIN + 0x20, 4, 0x002375F0, 0x001B31B8, 0, 0xCAFEF00D)
        out, _ = self.drive(recs + open_("pci:", 2) + [call(0x1E0, 4, PROBE)]
                            + start_chain(0x002375C0)
                            + [rd(4, VOODOO + 0x10C), "dump 801B31B8 1", "state",
                               "delay 2 0", "advance 1000000", "advance 3000000", "state"])
        self.assertEqual(self.rds(out), [0x12345678])
        self.assertEqual(out.dumps(), [[0xCAFEF00D]])
        [delay] = [int(d[0]) for d in out.all("delay")]
        self.assertTrue(6000 < delay < 7000, delay)
        probes = out.all("probe")
        self.assertEqual(len(probes), 1, out.text)
        a0, _, _, now = probes[0]
        self.assertEqual(int(a0, 16), 1)
        self.assertTrue(delay <= int(now) < delay + 300, (delay, now))
        before, after = out.all("m")
        self.assertEqual(int(before[2], 16) & 0x10, 0)
        self.assertEqual(int(after[2], 16) & 0x10, 0)  # cleared by the dispatcher
        self.assertEqual(out.v0s()[-4:], [0, 0, 0, 0])

    def test_dma0_stops(self):
        """A record outside RAM, a chain that loops, one longer than 4,096 records, a start
        while a chain is pending, an unmeasured control value or offset, and ioctl 0x7300
        stop, naming the record or the request."""
        pci = open_("pci:", 2)
        a1 = "a1 0x00007301"
        self.stops(pci + start_chain(0x00900000), a1, "record 0x00900000", "outside RAM")
        loop = mem(CHAIN, 4, 0x002375D0, 0x0800010C, 0x002375C0)
        self.stops(loop + pci + start_chain(0x002375C0), "record 0x002375C0", "loops")
        long_chain = []
        for i in range(4097):
            addr = 0x00400000 + 16 * i
            long_chain += mem(0x80000000 + addr, 4, 0x00500000, 0x001B31B8, addr + 16)
        self.stops(long_chain + pci + start_chain(0x00400000), "4096 records",
                   "record 0x00410000")
        ok = mem(CHAIN, 4, 0x002375D0, 0x001B31B8, 0)
        self.stops(ok + pci + start_chain(0x002375C0) + gt(0x830, 0x002375C0)
                   + gt(0x840, 0x1DC0), a1, "pending")
        self.stops(pci + gt(0x840, 0x1000), a1, "0x00001000")
        self.stops(pci + gt(0x844, 0), a1, "offset 0x844")
        self.stops(pci + mem(BUF, 0x840, 0) + [ioctl(6, 0x7300, BUF)], "0x7300")
        self.stops(pci + [ioctl(6, 0x7302, BUF)], "0x7302")
        self.stops(pci + gt(0x840, 0x1DC0), a1, "record 0x00000000")

    def test_dma0_delay_model(self):
        """The delay grows by the fit's rate per record and per frame-buffer byte."""
        out, _ = self.drive(["delay 0 0", "delay 1 0", "delay 0 1600", "delay 130 409600"])
        d = [int(x[0]) for x in out.all("delay")]
        self.assertEqual(d[0], 2663)
        self.assertEqual(d[1] - d[0], 1940)  # 2,663.89 + 1,939.40 ns, rounded down
        self.assertEqual(d[2] - d[0], 27114)  # 1,600 bytes of 16.946 ns
        self.assertTrue(abs(d[3] - 7195876) <= 10, d[3])


class DiskTest(HwTestCase):
    def test_raw_read_callback(self):
        """Slot 0x158 with a callback set copies the sectors, calls the callback with 0 before
        returning (ra the IDE completion path's, registers other than v0 as they were) and
        returns 0; without callback or hook it returns the count; an LBA beyond the image
        transfers nothing and calls the callback with the count."""
        img, image = self.disk()
        sectors = len(image) // SECTOR
        out, _ = self.drive(img + [f"r 1D {SP_TOP:X}", call(0x158, 1, BUF, 2), f"peek {BUF:X} 400",
                                   call(0x1D8, PROBE), call(0x158, 3, BUF, 1), "regs",
                                   f"peek {BUF:X} 200", f"fill {BUF:X} 400 AA",
                                   call(0x158, sectors, BUF, 2), call(0x158, sectors - 2, BUF, 2),
                                   f"peek {BUF:X} 400", call(0x1D8, 0), call(0x158, 0, BUF, 1)])
        # slot 0x1D8 leaves v0 as it was
        self.assertEqual(out.v0s(), [2, 2, 0, 2, 2, 2, 1])
        peeks = out.peeks()
        self.assertEqual(peeks[0], image[SECTOR:3 * SECTOR])
        self.assertEqual(peeks[1], image[3 * SECTOR:4 * SECTOR])
        self.assertEqual(peeks[2], b"\xAA" * 0x400)  # sectors - 2 + 2 is at the end: refused
        probes = out.all("probe")
        self.assertEqual([int(p[0], 16) for p in probes], [0, 2, 2])
        self.assertEqual({int(p[2], 16) for p in probes}, {CALLBACK_RA})
        self.assertEqual({int(p[1], 16) for p in probes}, {SP_TOP})
        regs = [int(x, 16) for x in out.all("regs")[0]]
        self.assertEqual(regs[4:8], [3, BUF, 1, 0])
        self.assertEqual(regs[31], CALLER_RA)
        self.assertNotIn(0xDEAD0001, regs)

    def test_raw_read_wake(self):
        """With the disk hooks installed (slot 0x220) and no callback, a read with a non-zero
        cookie calls wake(cookie) and returns 0, never wait; with cookie 0 it returns the
        count; a callback set as well takes precedence (section 8)."""
        img, image = self.disk()
        out, _ = self.drive(img + mem(PROC, COOKIE)
                            + [call(0x220, PROC, 0x80310000, PROBE), call(0x158, 0, BUF, 1),
                               f"peek {BUF:X} 200"] + mem(PROC, 0)
                            + [call(0x158, 0, BUF, 1), call(0x1D8, PROBE)] + mem(PROC, COOKIE)
                            + [call(0x158, 0, BUF, 1), call(0x220, 0, 0, 0),
                               call(0x1D8, 0), call(0x158, 0, BUF, 1)])
        # slots 0x220 and 0x1D8 leave v0 as it was
        self.assertEqual(out.v0s(), [0, 0, 1, 1, 0, 0, 0, 1])
        self.assertEqual([int(p[0], 16) for p in out.all("probe")], [COOKIE, 0])
        self.assertEqual(out.peeks(), [image[:SECTOR]])

    def test_disk_hooks_store(self):
        """Slots 0x1D8 and 0x220 store their arguments, never wait, and leave v0 as it was."""
        out, _ = self.drive(["r 2 1234", call(0x1D8, PROBE), call(0x220, PROC, 1, 2),
                             call(0x220, 0, 0, 0), call(0x1D8, 0)])
        self.assertEqual(out.v0s(), [0x1234] * 4)

    def test_partition_table(self):
        """Slot 0x128 returns 0x800B7160, where it has put the disk header (magic, count, 12-byte
        entries) with the total sector count at + 0xC; slot 0x118 accepts 0 to the count and
        returns 2 above it."""
        img, image = self.disk()
        n = 8 + 12 * 2
        out, _ = self.drive(img + [call(0x128), f"peek {PART_TABLE:X} {n:X}",
                                   call(0x118, 0), call(0x118, 1), call(0x118, 2),
                                   call(0x118, 0xFFFFFFFF)])
        self.assertEqual(out.v0s(), [PART_TABLE, 0, 0, 2, 2])
        want = bytearray(image[:n])
        want[0xC:0x10] = (len(image) // SECTOR).to_bytes(4, "little")
        self.assertEqual(out.peeks(), [bytes(want)])

    def test_disk_slots_stop(self):
        """Without an image the partition and sector slots stop; so do a count of 0 and a
        buffer that leaves RAM."""
        for cmd in (call(0x118, 1), call(0x128), call(0x158, 0, BUF, 1)):
            with self.subTest(cmd=cmd):
                self.stops([cmd], "no disk image")
        img, _ = self.disk()
        self.stops(img + [call(0x158, 0, BUF, 0)], "0x158", "count 0")
        self.stops(img + [call(0x158, 0, 0x807FFF00, 1)], "bus write", "0x80800000")


class BusTest(HwTestCase):
    def test_cmos_direct_read(self):
        """After a `cmos:` write of a byte at offset 5, mem_r8(0xB6100014) reads it, as does
        its kseg0 alias; a direct write, a word read and an unaligned byte stop."""
        out, _ = self.drive(mem(DATA, 0x5A) + [ioctl(3, 0x6300, 0xB6100014),
                                               call(0x0C0, 3, DATA, 1), rd(1, 0xB6100014),
                                               rd(1, 0x96100014), rd(1, 0xB6100018)])
        self.assertEqual(self.rds(out), [0x5A, 0x5A, 0xFF])
        self.stops([wr(1, 0xB6100014, 1)], "0xB6100014", "write", "CMOS")
        self.stops([rd(4, 0xB6100014)], "0xB6100014", "4 bytes")
        self.stops([rd(1, 0xB6100015)], "0xB6100015", "word")

    def test_unmodelled_bus_stops(self):
        """A read of 0xB6000000 (IOASIC) stops naming the address, size and pc; a write names
        its value too; kuseg outside RAM, kseg2 and an address past the CMOS stop."""
        _, msg = self.stops(["pc 800D4000", rd(4, 0xB6000000)], "bus read", "4 bytes",
                            "0xB6000000", "physical 0x16000000",
                            "pc as last stored 0x800D4000", "--trace-pc")
        _, msg = self.stops(["pc 800D4000", wr(2, 0xB7000000, 0x1234)], "bus write",
                            "2 bytes", "0x1234", "0xB7000000", "pc as last stored 0x800D4000")
        self.stops([rd(4, 0x08000000)], "0x08000000", "kuseg")
        self.stops([rd(4, 0xC0000000)], "0xC0000000", "kseg2")
        self.stops([rd(1, 0xB6120000)], "0xB6120000", "physical 0x16120000")

    def test_gun(self):
        """The gun's eight byte registers read the measured no-input values; byte writes of
        offsets 0 and 4 are accepted; any other offset or size stops."""
        out, _ = self.drive([rd(1, 0xB6800000 + off) for off in sorted(GUN)]
                            + [wr(1, 0xB6800000, 0), wr(1, 0xB6800004, 0x10), rd(1, 0x96800004)])
        self.assertEqual(self.rds(out), [GUN[off] for off in sorted(GUN)] + [0x48])
        self.stops([wr(1, 0xB6800008, 0)], "physical 0x16800008", "write")
        self.stops([rd(4, 0xB6800004)], "physical 0x16800004", "4 bytes")
        self.stops([rd(1, 0xB6800001)], "physical 0x16800001")
        self.stops([wr(4, 0xB6800000, 0)], "physical 0x16800000", "4 bytes")

    def test_sound_fifo(self):
        """Word writes of the sound FIFO are counted and dropped; a read, another size or
        another address stops."""
        out, _ = self.drive([wr(4, 0xB3000000, 1), wr(4, 0x93000000, 2), "counts 0"])
        self.assertEqual(out.all("counts"), [["0", "0", "0", "2"]])
        self.stops([rd(4, 0xB3000000)], "0xB3000000", "read", "sound FIFO")
        self.stops([wr(2, 0xB3000000, 1)], "physical 0x13000000", "2 bytes")
        self.stops([wr(4, 0xB3000004, 1)], "0xB3000004")

    def test_asic_reset_register(self):
        """0x17F00000 reads its current value, initially the measured 0x0E, and stores a byte
        written to it; hw_reset restores 0x0E; a word access stops."""
        out, _ = self.drive([rd(1, 0xB7F00000), wr(1, 0xB7F00000, 0x1E), rd(1, 0xB7F00000),
                             "reset", rd(1, 0xB7F00000)])
        self.assertEqual(self.rds(out), [0x0E, 0x1E, 0x0E])
        self.stops([rd(4, 0xB7F00000)], "physical 0x17F00000", "4 bytes")

    def test_hw_reset(self):
        """hw_reset returns the registers, the DAC, the counters and the PCI space to
        power-on."""
        self.stops(cfg_write(0x40, 5) + dac_cmds([0x303, 0xB00]) + ["reset"]
                   + cfg_write(0x40, 5) + dac_cmds([0xB00, None]), "DAC register 3")
        self.stops([wr(4, VOODOO + 0x104, 1), "reset", rd(4, VOODOO + 0x104)], "never written")
        out, _ = self.drive([wr(4, 0xB3000000, 1), wr(4, 0xA8800000, 1), wr(4, VOODOO + 0x104, 1),
                             "reset", "counts 41", rd(4, VOODOO + 0x210)])
        self.assertEqual(out.all("counts"), [["0", "0", "0", "0"]])
        self.assertEqual(self.rds(out), [0x410])
        self.stops([f"r 1D {SP_TOP:X}"] + mem(SP_TOP + 16, 0)
                   + [call(0x110, 0x121A, 0xFFFF, 0, DEVP), "reset",
                      call(0x0F0, 0x10, 8, DATA)], "BAR0")


def facts(name):
    return work_path("mame", "m2-facts", name)


def fact_lines(name):
    with open(facts(name)) as f:
        head = f.readline()
        assert head.startswith("#"), name
        for line in f:
            yield line.split()


@requires_file("mame", "m2-facts", "voodoo-reads.txt")
class VoodooFactsTest(HwTestCase):
    def test_voodoo_reads_like_mame(self):
        """For every register voodoo-reads.txt shows the game reading: status reads only
        values MAME returned (idle, and idle with retrace, both of them over a frame); every
        other register is plain storage (kind 1) or fbiInit2 under PCI initEnable bit 2 (the
        DAC, kind 3, which stops here only because no DAC read is latched; DacTest and
        test_dac_reads_like_mame cover it): each value MAME returned reads back after being written,
        the fbiInit registers' power-on values are their first measured reads, and exactly
        the reads made while initEnable bit 2 was set are the remapped fbiInit2 ones.

        voodoo-reads.txt has no writes. For the first 2 s after the entry,
        test_voodoo_replay_like_mame replays the captured writes (voodoo-ramp.txt) before each
        read. This weaker form covers the 15 s and so is all that checks the reads after that
        span (the DAC windows at 7.93 s, fbiInit reads from 7.93 s and the mode registers at
        9.29 s); test_dac_reads_like_mame covers every DAC read."""
        values = collections.defaultdict(list)
        status = set()
        remap = 0
        remapped = []
        for f in fact_lines("voodoo-reads.txt"):
            if len(f) == 10:
                if f[1] == "0x0F8" and int(f[2], 16) == 0x40:
                    remap = int(f[8], 16) & 4
                continue
            addr, value = int(f[1], 16), int(f[2], 16)
            if addr >= 0x08400000:
                continue
            reg = addr - 0x08000000
            if reg == 0:
                status.add(value)
                continue
            if remap:
                remapped.append((reg, value))
            else:
                values[reg].append(value)
        self.assertEqual(sorted(set(values) | {r for r, _ in remapped}), sorted(STORAGE))
        self.assertEqual(sorted({r for r, _ in remapped}), [0x218])
        self.assertEqual(sorted({v for _, v in remapped}), [0x00, 0x55, 0x71, 0x79])
        for reg, power_on in FBIINIT_POWER_ON.items():
            self.assertEqual(values[reg][0], power_on, hex(reg))
        cmds = cfg_write(0x40, 3)
        for reg in sorted(values):
            for v in sorted(set(values[reg])):
                cmds += [wr(4, VOODOO + reg, v), rd(4, VOODOO + reg)]
        frame = [rd(4, VOODOO)] + [f"checks {VBLANK_PERIOD_NS // 100 // 50:X}",
                                   rd(4, VOODOO)] * 50
        out, _ = self.drive(cmds + frame)
        got = self.rds(out)
        want = [v for reg in sorted(values) for v in sorted(set(values[reg]))]
        self.assertEqual(got[:len(want)], want)
        sink_status = set(got[len(want):])
        self.assertEqual(sink_status, {STATUS_IDLE, STATUS_RETRACE})
        self.assertLessEqual(sink_status, status)
        for reg in (0x218, 0x21C):
            with self.subTest(reg=hex(reg)):
                self.stops(cfg_write(0x40, 5) + [rd(4, VOODOO + reg)], "DAC")

    def test_pci_calls_like_mame(self):
        """The 44 PCI slot calls of voodoo-reads.txt, replayed in order, return what MAME
        returned and store the same words."""
        cmds, want = [f"r 1D {SP_TOP:X}"], []
        for f in fact_lines("voodoo-reads.txt"):
            if len(f) != 10:
                continue
            slot, a0, a1, a2, a3, a4, v0 = (int(x, 16) for x in f[1:8])
            thru = None if f[8] == "-" else int(f[8], 16)
            if slot == 0x0F0:
                cmds += [call(0x0F0, a0, a1, DATA), f"dump {DATA:X} 1"]
            elif slot == 0x0F8:
                cmds += mem(DATA, thru) + [call(0x0F8, a0, a1, DATA)]
            elif slot == 0x108:
                cmds += [call(0x108, a0, a1, a2, a3)]
            else:
                cmds += mem(SP_TOP + 16, a4) + [call(0x110, a0, a1, a2, DEVP),
                                                f"dump {DEVP:X} 1"]
            want.append(("ret", v0))
            if slot in (0x0F0, 0x110):
                want.append(("dump", thru))
        out, _ = self.drive(cmds)
        got = [("ret" if line[0] == "ret" else "dump", int(line[1], 16))
               for line in out.lines if line[0] in ("ret", "dump")]
        self.assertEqual(len(want), 44 + 21)
        self.assertEqual(got, want)


    def test_dac_reads_like_mame(self):
        """Every fbiInit2 read the game made under initEnable bit 2 (voodoo-reads.txt) is
        reproduced, in order, by the dacData sequences read from the routine each window
        belongs to (the return address of its initEnable write of 5): the ICS detect, the two
        PLL routines, and 0x8015A900, which writes DAC register 6 and reads nothing."""
        routines = {0x8015858C: ICS_DETECT, 0x8015A1F8: pll_set(0x0A, 0x12, 0x34),
                    0x80159A20: pll_set(0x00, 0x56, 0x78), 0x8015A92C: [0x650]}
        cmds, measured, remap = [], [], False
        for f in fact_lines("voodoo-reads.txt"):
            if len(f) == 10 and f[1] == "0x0F8" and int(f[2], 16) == 0x40:
                value = int(f[8], 16)
                cmds += cfg_write(0x40, value)
                remap = bool(value & 4)
                if remap:
                    cmds += dac_cmds(routines[int(f[9], 16)])
            elif len(f) == 4 and remap and int(f[1], 16) == VOODOO_PHYS + FBIINIT2:
                measured.append(int(f[2], 16))
        self.assertEqual(len(measured), 9)
        out, _ = self.drive(cmds)
        self.assertEqual(self.rds(out), measured)


VOODOO_PHYS = 0x08000000


@requires_file("mame", "m2-facts", "voodoo-ramp.txt")
@requires_file("mame", "m2-facts", "voodoo-reads.txt")
class VoodooReplayTest(HwTestCase):
    def test_voodoo_replay_like_mame(self):
        """The game's own Voodoo writes and reads of voodoo-ramp.txt (its first 2 s after the
        entry), merged in time order with the PCI configuration writes of voodoo-reads.txt,
        replayed into the sink. Every register read returns what MAME returned, except status
        (the virtual clock is not MAME's: test_voodoo_reads_like_mame covers it). Every LFB
        read stops: the first, of the start-up memory test, is checked to stop; the replay then
        goes on without the LFB reads (9 of the memory test, 8,228 of the triangle-drawn
        ramp)."""
        events = []
        for f in fact_lines("voodoo-reads.txt"):
            if len(f) == 10 and f[1] == "0x0F8":
                events.append((int(f[0]), 0, cfg_write(int(f[2], 16), int(f[8], 16))))
        last = 0
        for f in fact_lines("voodoo-ramp.txt"):
            last = int(f[0])
            events.append((last, 1, (f[1], int(f[2], 16), int(f[3], 16))))
        events = [e for e in sorted(events, key=lambda e: (e[0], e[1])) if e[0] <= last]
        cmds, want, lfb_reads, first_lfb, before_lfb = [], [], 0, None, None
        for t, _, e in events:
            if isinstance(e, list):
                cmds += e
                continue
            kind, addr, value = e
            lfb = VOODOO_PHYS + 0x400000 <= addr < VOODOO_PHYS + 0x800000
            if kind == "W":
                # the game's LFB stores are halfwords (hardware-usage.md, "Voodoo LFB")
                cmds.append(wr(2 if lfb else 4, 0xA0000000 | addr, value))
            elif lfb:
                lfb_reads += 1
                if first_lfb is None:
                    first_lfb, before_lfb = addr, list(cmds)
            elif addr != VOODOO_PHYS:
                cmds.append(rd(4, 0xA0000000 | addr))
                want.append(value)
        self.assertEqual(lfb_reads, 9 + 8228)
        # the same register reads as voodoo-reads.txt shows for that span
        same = [int(f[2], 16) for f in fact_lines("voodoo-reads.txt")
                if len(f) == 4 and int(f[0]) <= last
                and VOODOO_PHYS < int(f[1], 16) < VOODOO_PHYS + 0x400000]
        self.assertEqual(want, same)
        out, _ = self.drive(cmds)
        self.assertEqual(self.rds(out), want)
        self.stops(before_lfb + [rd(2, 0xA0000000 | first_lfb)],
                   f"physical 0x{first_lfb:08X}", "LFB read")


@requires_file("mame", "m2-facts", "dma0.txt")
class DmaFactsTest(HwTestCase):
    def test_dma0_delay_fits_mame(self):
        """For every chain shape of dma0.txt (records not writing the frame buffer, bytes of
        those that do), dma0_delay_ns is within the fit's stated error, 5 ns (dev_sysctrl.c and
        hardware-usage.md), of every measured start-to-interrupt time; every chain
        ends with next = 0 and every record is word aligned, as the model assumes."""
        shapes = collections.defaultdict(set)
        cur = None
        for f in fact_lines("dma0.txt"):
            if f[1] == "chain":
                cur = [0, 0]
                self.assertEqual(int(f[4], 16), 0)
            elif f[1] == "rec":
                count, dest = int(f[3], 16), int(f[5], 16)
                self.assertEqual(int(f[2], 16) % 4 + int(f[4], 16) % 4 + dest % 4 + count % 4, 0)
                if 0x08400000 <= dest < 0x08800000:
                    cur[1] += count
                else:
                    cur[0] += 1
            elif f[1] == "b4":
                shapes[tuple(cur)].add(int(f[2]))
        self.assertEqual(len(shapes), 8)
        out, _ = self.drive([f"delay {n} {b}" for n, b in sorted(shapes)])
        for (shape, measured), (d,) in zip(sorted(shapes.items()), out.all("delay")):
            for m in measured:
                with self.subTest(shape=shape, measured=m):
                    self.assertLessEqual(abs(int(d) - m), 5)


@requires_file("mame", "m2-facts", "inputs.txt")
class InputFactsTest(HwTestCase):
    def test_gun_and_asic_like_mame(self):
        """The gun registers read the one value inputs.txt gives each; 0x17F00000 reads the
        first of its two values (the read at 0x800D4BA4) and, after the game's write of the
        second (0x800D4BC8), that one."""
        gun, arst = {}, []
        for f in fact_lines("inputs.txt"):
            src = int(f[0], 16)
            if 0x16800000 <= src < 0x16800020:
                self.assertNotIn(src, gun)
                gun[src] = int(f[1], 16)
            elif src == 0x17F00000:
                arst.append((int(f[3]), int(f[1], 16)))
        self.assertEqual({a - 0x16800000: v for a, v in gun.items()}, GUN)
        (_, first), (_, second) = sorted(arst)
        out, _ = self.drive([rd(1, 0xB6800000 + off) for off in sorted(GUN)]
                            + [rd(1, 0xB7F00000), wr(1, 0xB7F00000, second), rd(1, 0xB7F00000)])
        self.assertEqual(self.rds(out), [GUN[off] for off in sorted(GUN)] + [first, second])


@requires_file("carnevil.raw")
@requires_file("mame", "m2-facts", "disk-hooks.txt")
class GameDiskHwTest(HwTestCase):
    def test_movie_frame_read(self):
        """On the game's disk, slot 0x128's table gives partition 13 the start the movie
        player's reads used (open question 6), and the first movie frame read through slot
        0x158 with the movie callback set copies the image's sectors and calls it with 0."""
        movs = [f for f in fact_lines("disk-hooks.txt") if f[1] == "mov"]
        first = dict(x.split("=") for x in movs[0][3:])
        part, frame, spf = (int(first[k], 16) for k in ("part", "frame", "spf"))
        start = int(movs[0][2], 16)
        self.assertEqual((part, frame, spf), (13, 0, 0xD0))
        raw = work_path("carnevil.raw")
        with open(raw, "rb") as f:
            f.seek(start * SECTOR)
            want = f.read(spf * SECTOR)
        out, _ = self.drive([f"img {raw} {self.path('game.ovl')}", call(0x128),
                             f"dump {PART_TABLE + 8 + 12 * part:X} 1", call(0x118, 1),
                             call(0x1D8, PROBE), call(0x158, start + frame * spf, 0x803F87B0, spf),
                             f"peek 803F87B0 {spf * SECTOR:X}"])
        self.assertEqual(out.all("img"), [["0"]])
        self.assertEqual(out.v0s(), [PART_TABLE, 0, 0, 0])
        self.assertEqual(out.dumps(), [[start]])
        self.assertEqual([int(p[0], 16) for p in out.all("probe")], [0])
        self.assertEqual(out.peeks(), [want])


if __name__ == "__main__":
    unittest.main()
