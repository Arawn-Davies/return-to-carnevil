"""config/boot_state.toml: machine state at GAME.EXE's entry (docs/analysis/boot-handoff.md).

Reads only committed files, so it runs without the game data.
"""
import os
import tomllib
import unittest

from carnevil_tools import functions, loader

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
        # the loader's leftovers at entry: only general registers, each a 32-bit value
        known = {"entry", "sp", "gp", "status", "config", "fcr31", "ra"}
        leftovers = {k: v for k, v in self.state.items()
                     if k not in known and not isinstance(v, (dict, list))}
        self.assertEqual(sorted(leftovers),
                         sorted("at v0 v1 a0 a1 a3 t0 t1 t2 t4 t5 s0 s1 s2 t9 k0 k1".split()))
        for name, value in leftovers.items():
            self.assertIn(name, REGISTERS, name)
            self.assertIsInstance(value, int, name)
            self.assertTrue(0 < value <= 0xFFFFFFFF, name)

    def test_first_loader_call(self):
        call = self.state["first_loader_call"]
        self.assertEqual(sorted(call), sorted(["slot", "a0", "a1", "a2", "a3", "ra", "sp"]))
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


if __name__ == "__main__":
    unittest.main()
