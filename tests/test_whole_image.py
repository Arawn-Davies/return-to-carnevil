"""The real GAME.EXE through recomp.recompile and `make linkcheck` (game data and `cc`).

The generated C goes to a temporary folder, never to the repository. Recompiling takes about
2 s and `make linkcheck` about 5 s on the Mac (10 cores), so neither test is gated behind
CARNEVIL_SLOW.
"""
import filecmp
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from carnevil_tools import exe, functions, recomp
from tests import charness
from tests.gamedata import requires_file, work_path
from tests.test_recomp import _make, linkcheck

COMMITTED = os.path.join(charness.REPO, "config", "game.functions.toml")
FUNCTIONS_MD = os.path.join(charness.REPO, "docs", "analysis", "functions.md")


def documented_unresolved_jr():
    """The count in the heading "Unresolved indirect jumps (N)" of docs/analysis/functions.md:
    the `jr`s neither the analysis nor the manual file resolves, left to rt_jump."""
    with open(FUNCTIONS_MD, encoding="utf-8") as f:
        found = re.findall(r"^## Unresolved indirect jumps \((\d+)\)$", f.read(), re.M)
    if len(found) != 1:
        raise AssertionError("functions.md has no single 'Unresolved indirect jumps (N)' heading")
    return int(found[0])


@requires_file("part0_files", "GAME.EXE")
class WholeImageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = exe.load_exe(work_path("part0_files", "GAME.EXE"))
        cls.analysis = functions.read_toml(COMMITTED)
        cls.tmp = tempfile.mkdtemp()
        cls.gen = os.path.join(cls.tmp, "generated")
        cls.manifest = recomp.recompile(cls.image, cls.analysis, cls.gen)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_whole_image_recompiles(self):
        m = self.manifest
        self.assertEqual(m.exe_sha256, exe.GAME_EXE_SHA256)
        self.assertEqual(m.functions, len(self.analysis.functions))
        self.assertEqual(m.files, -(-m.functions // recomp.FUNCTIONS_PER_FILE))
        self.assertEqual(m.stubs, sum(f.sources == {"stub"} for f in self.analysis.functions))
        self.assertEqual(m.interpreted, len(self.analysis.interpreted))
        self.assertEqual(m.jump_tables, len(self.analysis.jump_tables))
        self.assertEqual(m.unresolved_jr, documented_unresolved_jr())
        self.assertEqual(sorted(os.listdir(self.gen)),
                         sorted(["funcs.h", "func_table.c", "manifest.toml"]
                                + ["funcs_%03d.c" % k for k in range(m.files)]))

    def test_no_interpreted_functions(self):
        """With the task switch's three transfers as host-switch points, nothing is left to
        the interpreter: the yield 0x80151AC0, the exit 0x80151B50 and the scheduler
        0x80151BC0 are recompiled functions in the lookup, and rt_switch_kind knows the three
        points."""
        self.assertEqual(self.manifest.interpreted, 0)
        self.assertEqual(self.analysis.interpreted, [])
        with open(os.path.join(self.gen, "func_table.c"), encoding="utf-8") as f:
            table = f.read()
        lookup = dict(re.findall(r"\{0x([0-9A-F]{8})u, (fn_[0-9A-F]{8})\}", table))
        for addr in ("80151AC0", "80151B50", "80151BC0"):
            self.assertEqual(lookup.get(addr), "fn_" + addr)
        self.assertEqual(re.findall(r"\{0x([0-9A-F]{8})u, (RT_SWITCH_[A-Z]+)\}", table),
                         [("80151B48", "RT_SWITCH_YIELD"), ("80151B78", "RT_SWITCH_EXIT"),
                          ("80151E88", "RT_SWITCH_RESUME")])
        bodies = ""
        for name in sorted(os.listdir(self.gen)):
            if name.startswith("funcs_"):
                with open(os.path.join(self.gen, name), encoding="utf-8") as f:
                    bodies += f.read()
        self.assertNotIn("interp_run(", bodies)

    def test_recompile_is_deterministic(self):
        again = os.path.join(self.tmp, "again")
        recomp.recompile(self.image, self.analysis, again)
        names = sorted(os.listdir(self.gen))
        self.assertEqual(sorted(os.listdir(again)), names)
        match, mismatch, errors = filecmp.cmpfiles(self.gen, again, names, shallow=False)
        self.assertEqual((mismatch, errors), ([], []))
        shutil.rmtree(again)

    def test_whole_image_compiles_and_links(self):
        make = _make()
        build = os.path.join(self.tmp, "build")
        res = linkcheck(make, self.gen, build)
        self.assertEqual(res.returncode, 0, res.stdout[-4000:] + res.stderr[-4000:])
        stub = os.path.join(build, "game_stub")
        self.assertTrue(os.path.isfile(stub))
        self.assertNotIn("ndefined", res.stdout + res.stderr)
        # every symbol the program still imports comes from the C library
        nm = subprocess.run(["nm", "-u", stub], capture_output=True, text=True)
        self.assertEqual(nm.returncode, 0, nm.stderr)
        ours = [s for s in nm.stdout.split()
                if re.match(r"_?(fn_|rt_|bus_|interp_|recomp_)", s)]
        self.assertEqual(ours, [])


if __name__ == "__main__":
    unittest.main()
