"""config/boot_state.toml: machine state at GAME.EXE's entry (docs/analysis/boot-handoff.md).

Reads only committed files, so it runs without the game data.
"""
import contextlib
import os
import subprocess
import tempfile
import tomllib
import unittest

from carnevil_tools import bootstate, cli, functions, loader
from tests import charness

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOT_STATE = os.path.join(REPO, "config", "boot_state.toml")
FUNCTIONS = os.path.join(REPO, "config", "game.functions.toml")

ENTRY = 0x800C4000
STUB_START, STUB_END = 0x800C408C, 0x800C42CC  # the stub block, end exclusive
RAM_START, RAM_END = 0x80000000, 0x80800000
SOURCES = {"boot_rom_copy", "diag_leftover", "loader_data", "unknown"}
REGIONS = {"boot_rom", "stub_block", "other"}
REGISTERS = ("zero at v0 v1 a0 a1 a2 a3 t0 t1 t2 t3 t4 t5 t6 t7 s0 s1 s2 s3 s4 s5 s6 s7 t8 t9 "
             "k0 k1 gp sp fp ra").split()


class BootState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(BOOT_STATE, "rb") as f:
            cls.state = tomllib.load(f)
        cls.analysis = functions.read_toml(FUNCTIONS)

    def test_registers(self):
        s = self.state
        self.assertEqual(s["entry"], ENTRY)
        self.assertTrue(RAM_START <= s["sp"] < RAM_END, hex(s["sp"]))
        for key in ("gp", "status", "config", "fcr31", "ra"):
            self.assertIsInstance(s[key], int, key)
            self.assertTrue(0 <= s[key] <= 0xFFFFFFFF, key)

    def test_entry_leftover_registers(self):
        # the loader's leftovers at entry: general registers and lo, each a 32-bit value
        known = {"entry", "sp", "gp", "status", "config", "fcr31", "ra"}
        leftovers = {k: v for k, v in self.state.items()
                     if k not in known and not isinstance(v, (dict, list))}
        self.assertEqual(sorted(leftovers),
                         sorted("at v0 v1 a0 a1 a3 t0 t1 t2 t4 t5 s0 s1 s2 t9 k0 k1 lo".split()))
        for name, value in leftovers.items():
            self.assertIn(name, REGISTERS + ["lo"], name)
            self.assertIsInstance(value, int, name)
            self.assertTrue(0 < value <= 0xFFFFFFFF, name)

    def test_first_loader_call(self):
        call = self.state["first_loader_call"]
        # the slot and every register firstcall.lua recorded at the call: the general
        # registers but zero, hi and lo
        self.assertEqual(sorted(call), sorted(["slot", "hi", "lo"] + REGISTERS[1:]))
        for name, value in call.items():
            self.assertTrue(0 <= value <= 0xFFFFFFFF, name)
        self.assertIn(call["slot"], loader.game_slots(BOOT_STATE))
        self.assertTrue(RAM_START <= call["sp"] < RAM_END, hex(call["sp"]))
        text = range(self.analysis.text_start, self.analysis.text_end)
        self.assertIn(call["ra"], text, hex(call["ra"]))
        # a3 is the entry's leftover, passed through unchanged
        self.assertEqual(call["a3"], self.state["a3"])

    def test_ram_preload_below_the_image(self):
        preload = self.state["ram_preload"]
        self.assertTrue(preload)
        previous_end = RAM_START
        for r in preload:
            self.assertIn(r["source"], SOURCES, r)
            self.assertGreater(r["size"], 0, r)
            self.assertGreaterEqual(r["addr"], previous_end, "ranges sorted, not overlapping")
            self.assertLessEqual(r["addr"] + r["size"], ENTRY, hex(r["addr"]))
            previous_end = r["addr"] + r["size"]

    def test_external_calls_lie_outside_the_text(self):
        calls = self.state["external_call"]
        self.assertTrue(calls)
        text = range(self.analysis.text_start, self.analysis.text_end)
        for c in calls:
            self.assertNotIn(c["addr"], text, hex(c["addr"]))
            self.assertIn(c["region"], REGIONS, c)
            self.assertTrue(c["callers"], c)
            for caller in c["callers"]:
                self.assertIn(caller, text, "0x%08X calls 0x%08X" % (caller, c["addr"]))

    def test_game_slots(self):
        slots = loader.game_slots(BOOT_STATE)
        self.assertEqual(len(slots), 29)
        self.assertEqual(slots, sorted(set(slots)))
        self.assertIn(0x0C8, slots)  # ioctl
        self.assertIn(0x110, slots)  # the PCI lookup that returns the Voodoo base

    def test_every_stub_block_call_is_listed(self):
        # The 29 call targets inside the stub block 0x800C408C-0x800C42CC (functions.md).
        stubs = [c["stub"] for c in self.state["external_call"] if c["region"] == "stub_block"]
        self.assertEqual(len(stubs), 29)
        self.assertEqual(len(set(stubs)), 29)
        sources = {f.addr: f.sources for f in self.analysis.functions}
        self.assertEqual(self.analysis.stub_blocks, [(STUB_START, STUB_END)])
        for c in self.state["external_call"]:
            if c["region"] != "stub_block":
                self.assertNotIn("stub", c, c)
                continue
            stub = c["stub"]
            self.assertTrue(STUB_START <= stub < STUB_END, hex(stub))
            self.assertEqual(stub, STUB_START + c["slot"], hex(stub))
            self.assertEqual(sources.get(stub), {"stub"}, "0x%08X in game.functions.toml" % stub)


class BootStateHeader(unittest.TestCase):
    """carnevil_tools/bootstate.py: boot_state.h for the runtime's main."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def toml(self, text):
        path = os.path.join(self.tmp.name, "state.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_state_from_the_config(self):
        with open(BOOT_STATE, "rb") as f:
            raw = tomllib.load(f)
        s = bootstate.read(BOOT_STATE)
        self.assertEqual((s["entry"], s["status"], s["fcsr"], s["hi"], s["lo"]),
                         (ENTRY, raw["status"], raw["fcr31"], 0, raw["lo"]))
        self.assertEqual(len(s["gpr"]), 32)
        for i, name in enumerate(REGISTERS):
            self.assertEqual(s["gpr"][i], raw.get(name, 0), name)
        # the tables are evidence, not state: [first_loader_call]'s sp is not the entry's
        self.assertEqual(s["gpr"][29], raw["sp"])
        self.assertNotEqual(s["gpr"][29], raw["first_loader_call"]["sp"])

    def test_header_compiles_with_the_values(self):
        """The header, included in C and printed back, gives the file's values."""
        out = os.path.join(self.tmp.name, "boot_state.h")
        self.assertEqual(cli.main(["bootstate", BOOT_STATE, "-o", out]), 0)
        self.assertEqual(os.listdir(self.tmp.name), ["boot_state.h"])  # no temporary left
        src = os.path.join(self.tmp.name, "probe.c")
        with open(src, "w") as f:
            f.write('#include <stdio.h>\n#include "boot_state.h"\n'
                    "static const unsigned gpr[32] = BOOT_GPR;\n"
                    "int main(void)\n{\n"
                    '    printf("%X %X %X %X %X", BOOT_ENTRY, BOOT_STATUS, BOOT_FCSR, BOOT_HI, '
                    "BOOT_LO);\n"
                    '    for (int i = 0; i < 32; i++)\n        printf(" %X", gpr[i]);\n'
                    "    return 0;\n}\n")
        exe = os.path.join(self.tmp.name, "probe")
        subprocess.run([charness._cc(), *charness.CFLAGS, *charness.RUNTIME_WARNINGS, "-I",
                        self.tmp.name, src, "-o", exe], check=True)
        got = [int(x, 16) for x in subprocess.run([exe], capture_output=True, text=True,
                                                  check=True).stdout.split()]
        s = bootstate.read(BOOT_STATE)
        self.assertEqual(got, [s["entry"], s["status"], s["fcsr"], s["hi"], s["lo"]] + s["gpr"])

    def test_refusals(self):
        base = "entry = 0x800C4000\nstatus = 0x30010401\nfcr31 = 0x01000400\n"
        for extra, why in (("pc = 1\n", "unknown key 'pc'"), ("zero = 0\n", "r0"),
                           ("a0 = 0x100000000\n", "not a 32-bit value"),
                           ("a0 = -1\n", "not a 32-bit value"),
                           ('a0 = "1"\n', "not a 32-bit value")):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(bootstate.BootStateError, why):
                    bootstate.read(self.toml(base + extra))
        with self.assertRaisesRegex(bootstate.BootStateError, "no fcr31"):
            bootstate.read(self.toml("entry = 0x800C4000\nstatus = 0x30010401\n"))
        # the tables are ignored; registers not given are 0
        s = bootstate.read(self.toml(base + "[first_loader_call]\nslot = 0x38\n"))
        self.assertEqual(s["gpr"], [0] * 32)
        self.assertIsNone(s["config"])

    def test_cli_error_leaves_no_header(self):
        out = os.path.join(self.tmp.name, "boot_state.h")
        bad = self.toml("entry = 0x800C4000\n")
        with open(os.devnull, "w") as null, contextlib.redirect_stderr(null):
            self.assertEqual(cli.main(["bootstate", bad, "-o", out]), 1)
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
