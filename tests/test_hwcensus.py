"""carnevil_tools.hwcensus: constant hardware addresses in reached code (docs/analysis/hardware-usage.md)."""
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from carnevil_tools import cli, exe, functions, hwcensus
from tests.gamedata import requires_file, work_path

BOOT_ROM = os.environ.get("CARNEVIL_BOOT_ROM",
                          "/Volumes/archive/Gaming/Consoles/ROMs/mame/carnevil/boot.u32")
requires_boot_rom = unittest.skipUnless(
    os.path.isfile(BOOT_ROM), "boot ROM not found: " + BOOT_ROM + " (set CARNEVIL_BOOT_ROM)")
FUNCTIONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "config", "game.functions.toml")

# Slots of the loader's table that GAME.EXE calls (config/boot_state.toml).
GAME_SLOTS = (0x018, 0x028, 0x038, 0x088, 0x090, 0x0C0, 0x0C8, 0x0D8, 0x0E0, 0x0E8, 0x0F0,
              0x0F8, 0x108, 0x110, 0x118, 0x128, 0x158, 0x190, 0x198, 0x1A8, 0x1B8, 0x1C0,
              0x1D8, 0x1E0, 0x1E8, 0x1F0, 0x1F8, 0x200, 0x220)

BASE = 0x80100000
ZERO, AT, V0, V1, A0, A1, T0, T1, S0, RA = 0, 1, 2, 3, 4, 5, 8, 9, 16, 31


def _i(op, rs, rt, imm):
    return (op << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)


def lui(rt, imm):
    return _i(0x0F, 0, rt, imm)


def addiu(rt, rs, imm):
    return _i(0x09, rs, rt, imm)


def ori(rt, rs, imm):
    return _i(0x0D, rs, rt, imm)


def lw(rt, off, base):
    return _i(0x23, base, rt, off)


def sw(rt, off, base):
    return _i(0x2B, base, rt, off)


def sh(rt, off, base):
    return _i(0x29, base, rt, off)


def addu(rd, rs, rt):
    return (rs << 21) | (rt << 16) | (rd << 11) | 0x21


def beq(rs, rt, words):
    return _i(0x04, rs, rt, words)


def jal(target):
    return (0x03 << 26) | ((target >> 2) & 0x03FFFFFF)


JR_RA = (RA << 21) | 0x08
NOP = 0


def _image(words):
    data = b"".join(w.to_bytes(4, "little") for w in words)
    image = exe.ExeImage(0, BASE, data, "")
    analysis = functions.Analysis(
        BASE, BASE + len(data),
        [functions.Function(BASE, len(data), "func_test", {"entry"})], [], [])
    return image, analysis


def _site(k):
    return BASE + 4 * k


class Pairs(unittest.TestCase):
    def test_lui_addiu_pair(self):
        image, analysis = _image([lui(A0, 0xB600), addiu(A0, A0, 0x0020), JR_RA, NOP])
        found = hwcensus.constant_addresses(image, analysis)
        self.assertEqual(found, {0xB6000020: [_site(1)]})

    def test_lui_ori_pair(self):
        image, analysis = _image([lui(A0, 0xB600), ori(A0, A0, 0x8020), JR_RA, NOP])
        self.assertEqual(hwcensus.constant_addresses(image, analysis), {0xB6008020: [_site(1)]})

    def test_lui_load_offset(self):
        # A negative offset borrows from the high half: 0xA8010000 - 0x8000.
        image, analysis = _image([lui(V0, 0xA801), lw(V1, -0x8000, V0), JR_RA, NOP])
        self.assertEqual(hwcensus.constant_addresses(image, analysis), {0xA8008000: [_site(1)]})
        (access,) = hwcensus.census(image, analysis).accesses
        self.assertEqual((access.addr, access.op, access.size, access.function),
                         (0xA8008000, "read", 4, BASE))

    def test_one_base_feeds_every_access(self):
        # One lui, then accesses at different offsets, across a branch join.
        words = [
            lui(S0, 0xB600),       # 0
            lw(T0, 0x04, S0),      # 1
            beq(T0, ZERO, 2),      # 2 -> 5
            NOP,                   # 3
            sw(T0, 0x08, S0),      # 4
            sw(ZERO, 0x0C, S0),    # 5 join
            JR_RA, NOP,
        ]
        image, analysis = _image(words)
        accesses = {(a.site, a.addr, a.op) for a in hwcensus.census(image, analysis).accesses}
        self.assertEqual(accesses, {(_site(1), 0xB6000004, "read"),
                                    (_site(4), 0xB6000008, "write"),
                                    (_site(5), 0xB600000C, "write")})

    def test_store_records_a_constant_value(self):
        image, analysis = _image([lui(V0, 0xB740), addiu(T0, ZERO, 0x4000),
                                  sw(T0, 0, V0), JR_RA, NOP])
        (access,) = hwcensus.census(image, analysis).accesses
        self.assertEqual((access.addr, access.op, access.value), (0xB7400000, "write", 0x4000))

    def test_call_clobbers_caller_saved_registers(self):
        words = [lui(T0, 0xB600), lui(S0, 0xB680), jal(BASE + 4 * 7), NOP,
                 lw(T1, 0, T0), lw(T1, 4, S0), JR_RA, JR_RA, NOP]
        image, analysis = _image(words)
        addrs = {a.addr for a in hwcensus.census(image, analysis).accesses}
        self.assertEqual(addrs, {0xB6800004})

    def test_conflicting_paths_drop_the_register(self):
        words = [
            lui(V0, 0xB600),       # 0
            beq(A0, ZERO, 2),      # 1 -> 4
            NOP,                   # 2
            lui(V0, 0xB700),       # 3
            lw(T0, 0, V0),         # 4 join: v0 differs
            JR_RA, NOP,
        ]
        image, analysis = _image(words)
        self.assertEqual(hwcensus.census(image, analysis).accesses, [])

    def test_constant_plus_register_is_indexed(self):
        words = [lui(AT, 0xA840), addu(AT, AT, A0), sh(ZERO, 0x10, AT), JR_RA, NOP]
        image, analysis = _image(words)
        (access,) = hwcensus.census(image, analysis).accesses
        self.assertEqual((access.addr, access.indexed, access.size), (0xA8400010, True, 2))

    def test_base_loaded_from_a_global(self):
        words = [lui(A0, 0xA800), lui(AT, 0x8020), sw(A0, 0x10, AT),
                 lw(T0, 0x10, AT), sw(ZERO, 0x128, T0), JR_RA, NOP]
        image, analysis = _image(words)
        c = hwcensus.census(image, analysis)
        self.assertEqual(hwcensus.holders(c)[0], {(0x80200010,): 0xA8000000})
        (access,) = hwcensus.resolve(c)
        self.assertEqual((access.site, access.addr, access.via),
                         (_site(4), 0xA8000128, (0x80200010,)))

    def test_base_in_a_structure_behind_a_global(self):
        words = [lui(AT, 0x8020), lw(T0, 0, AT),        # t0 = *0x80200000 (a struct)
                 lui(T1, 0xA800), sw(T1, 8, T0),        # t0->base = 0xA8000000
                 lw(T1, 8, T0), sw(ZERO, 0x100, T1),    # t0->base[0x100/4] = 0
                 JR_RA, NOP]
        image, analysis = _image(words)
        (access,) = hwcensus.resolve(hwcensus.census(image, analysis))
        self.assertEqual((access.addr, access.via), (0xA8000100, (0x80200000, 8)))

    def test_two_different_bases_in_one_global_resolve_to_nothing(self):
        words = [lui(A0, 0xA800), lui(A1, 0xB600), lui(AT, 0x8020), sw(A0, 0x10, AT),
                 sw(A1, 0x10, AT), lw(T0, 0x10, AT), sw(ZERO, 4, T0), JR_RA, NOP]
        image, analysis = _image(words)
        self.assertEqual(hwcensus.resolve(hwcensus.census(image, analysis)), [])

    def test_known_return_value(self):
        words = [jal(0x800C419C), NOP, sw(ZERO, 0x128, V0), JR_RA, NOP]
        image, analysis = _image(words)
        c = hwcensus.census(image, analysis, returns={0x800C419C: 0xA8000000})
        self.assertEqual([a.addr for a in c.accesses], [0xA8000128])
        self.assertEqual(hwcensus.census(image, analysis).accesses, [])

    def test_delay_slot_runs_before_the_target(self):
        # The base is set in the delay slot of the branch and used on both paths.
        words = [beq(A0, ZERO, 2), lui(V0, 0xB600), lw(T0, 0x10, V0),
                 lw(T0, 0x14, V0), JR_RA, NOP]
        image, analysis = _image(words)
        addrs = {a.addr for a in hwcensus.census(image, analysis).accesses}
        self.assertEqual(addrs, {0xB6000010, 0xB6000014})


class Classify(unittest.TestCase):
    def test_classify_kseg1_alias(self):
        for x in (0x0, 0x10, 0x3C):
            self.assertEqual(hwcensus.classify(0xA8000000 + x), hwcensus.classify(0x88000000 + x))
        self.assertEqual(hwcensus.classify(0xA8000000), "voodoo")
        self.assertEqual(hwcensus.classify(0xB6000000), "ioasic")
        self.assertEqual(hwcensus.classify(0xAC000C18), "gt64010")
        self.assertEqual(hwcensus.classify(0xBFC00000), "boot_rom")
        self.assertEqual(hwcensus.classify(0x80001000), "ram")

    def test_narrowest_region_wins(self):
        self.assertEqual(hwcensus.classify(0xAA0001F0), "ide")
        self.assertEqual(hwcensus.classify(0xAA000000), "pci_io")
        self.assertEqual(hwcensus.classify(0xB7200000), "cs3")
        self.assertEqual(hwcensus.classify(0xA0900000), "unknown")

    def test_regions_well_formed(self):
        names = [r.name for r in hwcensus.REGIONS]
        self.assertEqual(len(names), len(set(names)))
        for r in hwcensus.REGIONS:
            self.assertLess(r.start, r.end, r.name)
            self.assertLess(r.end, 0x20000001, r.name)

    def test_register_names(self):
        self.assertEqual(hwcensus.register_name(0xA8000128), "swapbufferCMD")
        self.assertEqual(hwcensus.register_name(0xAC000C18), "INTR_CAUSE")
        # CarnEvil's IOASIC address order once unlocked: bus word 5 is the PIC output port.
        self.assertEqual(hwcensus.register_name(0xB6000014), "PICOUT")
        self.assertEqual(hwcensus.register_name(0xB6100010), "cmos+0x10")


def _strict(census):
    """Constants at or above physical 0x08000000 that classify as unknown, by kind."""
    used, other = set(), set()
    for a in hwcensus.all_accesses(census):
        if hwcensus.is_kseg01(a.addr) and a.addr & 0x1FFFFFFF >= 0x08000000 \
                and hwcensus.classify(a.addr) == "unknown":
            used.add(a.addr)
    for value, formed in census.formed.items():
        if hwcensus.is_kseg01(value) and value & 0x1FFFFFFF >= 0x08000000 \
                and hwcensus.classify(value) == "unknown":
            (used if formed.roles & {"base", "target"} else other).add(value)
    return used, other


class GameExe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = work_path("part0_files", "GAME.EXE")
        if path is None:
            raise unittest.SkipTest("GAME.EXE not found")
        cls.image = exe.load_exe(path)
        cls.census = hwcensus.census(cls.image, functions.read_toml(FUNCTIONS),
                                     hwcensus.GAME_RETURNS)
        cls.accesses = hwcensus.all_accesses(cls.census)
        cls.regions = {hwcensus.classify(a.addr) for a in cls.accesses}

    def test_game_exe_regions(self):
        for name in ("voodoo", "voodoo_lfb", "voodoo_texture", "ioasic", "carnevil_gun",
                     "asic_reset"):
            self.assertIn(name, self.regions)
        # The game reaches the GT64010, IDE, CMOS and PIC only through the loader.
        for name in ("gt64010", "pci_io", "ide", "ide_cs1", "ide_bmdma", "cmos",
                     "cmos_protect"):
            self.assertNotIn(name, self.regions)
        self.assertNotIn("PICOUT", {hwcensus.register_name(a.addr) for a in self.accesses
                                    if hwcensus.classify(a.addr) == "ioasic"})
        used, other = _strict(self.census)
        self.assertEqual(used, set(), "unclassified addresses dereferenced")
        self.assertEqual(sorted(other), hwcensus.NOT_ADDRESSES["GAME.EXE"])

    def test_voodoo_base_held_in_memory(self):
        known, _rams = hwcensus.holders(self.census)
        self.assertEqual(known[(0x801E756C,)], 0xA8000000)  # set from the PCI set-up slot
        via = {hwcensus.register_name(a.addr) for a in hwcensus.resolve(self.census)}
        self.assertTrue({"lfbMode", "fbzMode", "textureMode", "vdstatus"} <= via, via)


@requires_boot_rom
class Loader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = hwcensus.loader_image(BOOT_ROM)
        cls.analysis, _notes = hwcensus.analyse_loader(cls.image)
        cls.census = hwcensus.census(cls.image, cls.analysis)
        cls.accesses = hwcensus.all_accesses(cls.census)
        cls.regions = {hwcensus.classify(a.addr) for a in cls.accesses}

    def test_loader_image(self):
        self.assertEqual(self.image.load_addr, 0x80000000)
        self.assertEqual(self.image.end_addr(), 0x80015C40)
        self.assertEqual(len(hwcensus.slot_targets(self.image)), 72)
        names = [name for name, _routines in hwcensus.loader_devices(self.image)]
        for name in ("cmos", "pic", "ioasic", "timer", "disk", "wdog"):
            self.assertIn(name, names)

    def test_loader_regions(self):
        for name in ("gt64010", "ide", "ide_cs1", "ioasic", "cmos", "cmos_protect",
                     "watchdog", "interrupt_enable", "vblank_clear"):
            self.assertIn(name, self.regions)
        used, other = _strict(self.census)
        self.assertEqual(used, set(), "unclassified addresses dereferenced")
        self.assertEqual(sorted(other), hwcensus.NOT_ADDRESSES["loader"])

    def test_pic_only_through_its_driver(self):
        reach = hwcensus.loader_reach(self.image, self.analysis, GAME_SLOTS)
        pic = {a.function for a in self.accesses
               if hwcensus.register_name(a.addr) in ("PICOUT", "PICIN")}
        self.assertTrue(pic)
        labels = set().union(*(reach.get(f, set()) for f in pic))
        self.assertIn("driver:pic", labels)
        self.assertNotIn("game", labels)


class Cli(unittest.TestCase):
    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(["hwcensus", *argv])
            except SystemExit as exc:  # argparse errors
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def test_needs_exactly_one_image(self):
        rc, _out, err = self._run()
        self.assertEqual(rc, 2)
        self.assertIn("--loader", err)

    def test_missing_file(self):
        rc, _out, err = self._run("--loader", "/nonexistent/boot.u32")
        self.assertEqual(rc, 2)
        self.assertIn("not found", err)

    @requires_file("part0_files", "GAME.EXE")
    def test_game_summary(self):
        rc, out, _err = self._run(work_path("part0_files", "GAME.EXE"))
        self.assertEqual(rc, 0)
        self.assertIn("voodoo:", out)
        self.assertIn("swapbufferCMD", out)
        self.assertNotIn("gt64010:", out)

    @requires_boot_rom
    def test_loader_summary(self):
        rc, out, _err = self._run("--loader", BOOT_ROM)
        self.assertEqual(rc, 0)
        self.assertIn("29 slots called by GAME.EXE", out)
        self.assertIn("PICOUT", out)
        self.assertIn("[driver:pic slot]", out)


class LoaderImage(unittest.TestCase):
    def test_wrong_rom_refused(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"\0" * 0x80000)
            f.flush()
            with self.assertRaises(exe.UnknownRevisionError):
                hwcensus.loader_image(f.name)


if __name__ == "__main__":
    unittest.main()
