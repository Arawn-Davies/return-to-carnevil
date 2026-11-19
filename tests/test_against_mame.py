"""Real game code against MAME (game data, `cc` and `make`, and MAME's captures).

Two checks, documented in tools/mame/README.md:

- the smoke run: the recompiled game from GAME.EXE's entry, on the RAM MAME had there, to
  its first loader call (`runtime/stub/smoke.c`), against the state `firstcall.lua` recorded
  in MAME at the same call (`config/boot_state.toml` [first_loader_call] and
  `mame/dumps/ram-at-first-loader-call.bin` on the share);
- function replay: every pure function (`pure-functions`) `funcreplay.lua` captured in MAME
  during attract mode, run from MAME's before-state by `runtime/stub/replay.c` and compared
  with MAME's after-state.

The programs are built once for the module from a fresh recompile in a temporary folder.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import tomllib
import unittest

from carnevil_tools import board, cli, exe, functions, recomp
from tests import charness
from tests.gamedata import requires_file, work_path
from tests.test_recomp import CODE, A, JR_RA, Asm, image_of, _make

BOOT_STATE = os.path.join(charness.REPO, "config", "boot_state.toml")
FUNCTIONS = os.path.join(charness.REPO, "config", "game.functions.toml")
ENTRY_RAM = ("mame", "dumps", "ram-at-game-entry.bin")
FIRST_CALL_RAM = ("mame", "dumps", "ram-at-first-loader-call.bin")
CAPTURES = ("mame", "replay")
MAIN = 0x800C4524
RAM_BASE, RAM_SIZE = 0x80000000, 0x800000
GAME_ENTRY = 0x800C4000
MIN_CAPTURES = 20  # clean replays
EXIT_BUS = 3  # replay stopped at its first bus access
REGISTERS = ("zero at v0 v1 a0 a1 a2 a3 t0 t1 t2 t3 t4 t5 t6 t7 s0 s1 s2 s3 s4 s5 s6 s7 t8 t9 "
             "k0 k1 gp sp fp ra").split()

_build = {}


def requires_capture(rel, how):
    """Skip unless a MAME capture under the work directory exists, saying how to make it."""
    return unittest.skipUnless(work_path(*rel) is not None,
                               f"MAME capture not found: $CARNEVIL_WORK/{'/'.join(rel)}; "
                               f"produce it with {how}")


def programs():
    """(smoke, replay): the two programs, built from GAME.EXE once per module."""
    if not _build:
        make = _make()
        tmp = tempfile.mkdtemp()
        _build["tmp"] = tmp
        gen, build = os.path.join(tmp, "generated"), os.path.join(tmp, "build")
        recomp.recompile(exe.load_exe(work_path("part0_files", "GAME.EXE")),
                         functions.read_toml(FUNCTIONS), gen)
        res = subprocess.run([make, "-C", charness.REPO, "smoke", "replay", f"GENERATED={gen}",
                              f"BUILD={build}"], capture_output=True, text=True)
        if res.returncode != 0:
            raise AssertionError("make smoke replay failed:\n" + res.stdout[-4000:]
                                 + res.stderr[-4000:])
        _build["smoke"] = os.path.join(build, "smoke")
        _build["replay"] = os.path.join(build, "replay")
    return _build["smoke"], _build["replay"]


def tearDownModule():
    if "tmp" in _build:
        shutil.rmtree(_build["tmp"])
        _build.clear()


def boot_state():
    with open(BOOT_STATE, "rb") as f:
        return tomllib.load(f)


def parse_state(text):
    """The smoke program's report: {"slot": n, "r": [32], "hi", "lo", "fpr": [32], "fcsr"}."""
    out = {}
    for line in text.splitlines():
        key, *values = line.split()
        nums = [int(v, 16) for v in values]
        if key in ("r", "fpr"):
            out[key] = nums
        elif key in ("slot", "hi", "lo", "fcsr"):
            out[key] = nums[0]
    return out


# --- the purity rule, on a synthetic image ---------------------------------------------

T0, T9 = 8, 25
BLOCK = CODE + 0x400  # a stub block of 4 slots


def purity_asm():
    p = Asm()
    p.label("entry")
    for name in ("leaf", "calls_leaf", "calls_stub", "calls_calls_stub", "calls_jalr",
                 "has_break", "calls_interp", "hw", "jr_reg", "tail_to_stub_caller"):
        p.j("jal", name)
        p(0)
    p(JR_RA, 0)
    p.label("leaf")
    p(A("addu", rd=2, rs=4, rt=5), JR_RA, 0)
    p.label("calls_leaf")
    p.j("jal", "leaf")
    p(0, JR_RA, 0)
    p.label("calls_stub")
    p(A("jal", target=BLOCK + 8), 0, JR_RA, 0)
    p.label("calls_calls_stub")
    p.j("jal", "calls_stub")
    p(0, JR_RA, 0)
    p.label("calls_jalr")
    p(A("jalr", rd=31, rs=T9), 0, JR_RA, 0)
    p.label("has_break")
    p(A("break"), JR_RA, 0)
    p.label("interp")
    p(A("addu", rd=2, rs=4, rt=5), JR_RA, 0)
    p.label("calls_interp")
    p.j("jal", "interp")
    p(0, JR_RA, 0)
    p.label("hw")
    p(A("lui", rt=T0, imm=0xB600), A("lw", rt=2, rs=T0, imm=0), JR_RA, 0)
    p.label("jr_reg")
    p(A("jr", rs=T0), 0)
    p.label("tail_to_stub_caller")
    p.j("j", "calls_calls_stub")
    p(0)
    p.pad_to(BLOCK + 0x20)
    return p


class PurityRuleTest(unittest.TestCase):
    """pure-functions: a function is pure when nothing in its call closure calls a loader
    stub, touches hardware, breaks, makes an indirect call, jumps through an unresolved
    `jr` or is left to the interpreter."""

    def test_rule(self):
        p = purity_asm()
        image = image_of(p.words())
        analysis = functions.analyse(image, CODE, stub_blocks=[(BLOCK, BLOCK + 0x20)],
                                     interpreted=[p.labels["interp"]])
        pure, stats = cli.pure_functions(image, analysis)
        names = {v: k for k, v in p.labels.items()}
        self.assertEqual(sorted(names[a] for a in pure), ["calls_leaf", "leaf"])
        # the interpreted function and its caller are removed by that rule alone
        self.assertEqual(stats["removed_by_interpreted"], 2)
        self.assertEqual(stats["candidates"], 2)


@requires_file("part0_files", "GAME.EXE")
class PureFunctionListTest(unittest.TestCase):
    def test_pure_function_list_excludes_stub_callers(self):
        image = exe.load_exe(work_path("part0_files", "GAME.EXE"))
        analysis = functions.read_toml(FUNCTIONS)
        pure, _stats = cli.pure_functions(image, analysis)
        self.assertNotIn(MAIN, pure)
        self.assertNotIn(analysis.text_start, pure)  # _start calls slot 0x1A8
        for addr in analysis.interpreted:
            self.assertNotIn(addr, pure)
        self.assertTrue(pure)


# --- the smoke run -------------------------------------------------------------------------

@requires_file("part0_files", "GAME.EXE")
@requires_capture(ENTRY_RAM, "tools/mame/handoff.lua (tools/mame/README.md, handoff.lua)")
class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        smoke, _replay = programs()
        s = boot_state()
        cls.tmp = tempfile.mkdtemp()
        cls.ram_out = os.path.join(cls.tmp, "ram.bin")
        # every general register boot_state.toml gives (sp, gp, ra and the loader's
        # leftovers), and FCSR
        args = [f"{k}=0x{v:08X}" for k, v in s.items() if k in REGISTERS]
        args.append(f"fcsr=0x{s['fcr31']:08X}")
        cls.res = subprocess.run([smoke, work_path(*ENTRY_RAM), cls.ram_out, *args],
                                 capture_output=True, text=True, timeout=120)
        cls.state = parse_state(cls.res.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_smoke_reaches_the_first_loader_call(self):
        res = self.res
        self.assertEqual(res.returncode, 0, res.stderr[-4000:])
        # nothing but the run's own lines: no bus access, break or interpreter entry
        self.assertEqual([l for l in res.stderr.splitlines()
                          if re.search(r"bus_(read|write)|rt_break|interp_run|rt_interrupt", l)],
                         [])
        want = boot_state().get("first_loader_call")
        self.assertIsNotNone(want, "config/boot_state.toml has no [first_loader_call]")
        got = self.state
        self.assertEqual(got["slot"], want["slot"])
        for i, name in ((4, "a0"), (5, "a1"), (6, "a2"), (7, "a3"), (31, "ra"), (29, "sp")):
            self.assertEqual(got["r"][i], want[name], name)

    @requires_capture(FIRST_CALL_RAM, "tools/mame/firstcall.lua, then copy its RAM to "
                      "mame/dumps/ (tools/mame/README.md, firstcall.lua)")
    def test_ram_at_first_loader_call_matches_mame(self):
        self.assertEqual(self.res.returncode, 0, self.res.stderr[-4000:])
        lo = GAME_ENTRY
        hi = self.state["r"][29]
        with open(work_path(*FIRST_CALL_RAM), "rb") as f:
            want = f.read()
        with open(self.ram_out, "rb") as f:
            got = f.read()
        self.assertEqual((len(want), len(got)), (RAM_SIZE, RAM_SIZE))
        a, b = lo - RAM_BASE, hi - RAM_BASE
        if want[a:b] != got[a:b]:
            first = next(i for i in range(a, b) if want[i] != got[i])
            n = sum(want[i] != got[i] for i in range(a, b))
            self.fail(f"RAM differs from MAME's in 0x{lo:08X}-0x{hi:08X}: {n} bytes, the first "
                      f"at 0x{RAM_BASE + first:08X}")
        # reported, not asserted: the loader's exception handlers write outside the range
        outside = differing_ranges(want, got, 0, a) + differing_ranges(want, got, b, RAM_SIZE)
        print(f"\nRAM outside 0x{lo:08X}-0x{hi:08X} differing from MAME's: "
              + (", ".join(f"0x{RAM_BASE + x:08X}-0x{RAM_BASE + y - 1:08X} ({n} bytes)"
                           for x, y, n in outside) or "none"), file=sys.stderr)


def differing_ranges(want, got, lo, hi, gap=4096):
    """(start, end, bytes) of the ranges of [lo, hi) where want and got differ, merged across
    fewer than gap equal bytes."""
    out, i = [], lo
    while i < hi:
        j = min(i + 4096, hi)
        if want[i:j] != got[i:j]:
            for k in range(i, j):
                if want[k] != got[k]:
                    if out and k - out[-1][1] < gap:
                        out[-1] = [out[-1][0], k + 1, out[-1][2] + 1]
                    else:
                        out.append([k, k + 1, 1])
        i = j
    return [tuple(r) for r in out]


# --- function replay -----------------------------------------------------------------------

FAKE_REPLAY = """\
import json, os, sys
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "spec.json")) as f:
    spec = json.load(f)
code, out = spec["interp" if "--interp" in sys.argv else "recomp"]
sys.stdout.write(out)
sys.exit(code)
"""


def bus_stop(access, r4="00000004", ram="0123456789ABCDEF"):
    """What replay prints when it stops at its first bus access."""
    return (f"bus {access}\nr " + " ".join(["00000000"] * 4 + [r4] + ["00000000"] * 27)
            + "\nhi 00000000\nlo 00000000\nfpr " + " ".join(["00000000"] * 32)
            + f"\nfcsr 01000000\nram fnv1a64 {ram}\n")


class ClassifyReplayTest(unittest.TestCase):
    """classify_replay's verdicts, with a stand-in for the replay program (no game data and no
    C compiler needed): only a first bus access to a hardware region that the interpreter makes
    identically excuses a capture."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fake = os.path.join(self.tmp, "replay")
        with open(self.fake, "w", encoding="utf-8") as f:
            f.write(f"#!{sys.executable}\n" + FAKE_REPLAY)
        os.chmod(self.fake, 0o755)

    def verdict(self, recomp, interp=None):
        with open(os.path.join(self.tmp, "spec.json"), "w", encoding="utf-8") as f:
            json.dump({"recomp": recomp, "interp": interp or recomp}, f)
        return classify_replay(self.fake, self.tmp)

    def test_clean(self):
        self.assertEqual(self.verdict([0, "800C6A64: same as MAME\n"]), ("clean", None))

    def test_difference_fails(self):
        self.assertEqual(self.verdict([1, "a0: MAME 1, replay 2\n"])[0], "failure")

    def test_hardware_access_made_alike_by_both_modes_is_impure(self):
        out = bus_stop("write 0xA800021C size 4 value 0x00000001")
        self.assertEqual(self.verdict([EXIT_BUS, out]),
                         ("impure", ("write 0xA800021C size 4", "voodoo")))

    def test_non_hardware_addresses_fail(self):
        for addr in ("0x00237000",     # kuseg: RAM, never the bus
                     "0x80900000",     # kseg0 past RAM: no region
                     "0x80100000",     # RAM through kseg0
                     "0xA0200000"):    # RAM through kseg1
            verdict, detail = self.verdict([EXIT_BUS, bus_stop(f"read {addr} size 4")])
            self.assertEqual(verdict, "failure", addr)
            self.assertIn("no hardware region", detail, addr)

    def test_interpreter_disagreement_fails(self):
        out = bus_stop("read 0xA8000000 size 4")
        for other in (bus_stop("read 0xA8000000 size 4", r4="00000005"),  # a register
                      bus_stop("read 0xA8000000 size 4", ram="FEDCBA9876543210"),  # RAM
                      bus_stop("read 0xA8000004 size 4"),  # the address
                      bus_stop("write 0xA8000000 size 4 value 0x00000000"),  # the direction
                      bus_stop("read 0xA8000000 size 2")):  # the size
            verdict, detail = self.verdict([EXIT_BUS, out], [EXIT_BUS, other])
            self.assertEqual(verdict, "failure", other.splitlines()[0])
            self.assertIn("interpreter", detail)
        self.assertEqual(self.verdict([EXIT_BUS, out], [0, "same as MAME\n"])[0], "failure")


def capture_dirs():
    base = work_path(*CAPTURES)
    if base is None:
        return None
    return sorted(os.path.join(base, d) for d in os.listdir(base)
                  if re.fullmatch(r"[0-9A-F]{8}", d)
                  and os.path.isfile(os.path.join(base, d, "capture.txt")))


@requires_file("part0_files", "GAME.EXE")
@unittest.skipUnless(work_path(*CAPTURES) is not None,
                     "no function captures under $CARNEVIL_WORK/mame/replay: produce them with "
                     "tools/mame/funcreplay.lua in the Linux container (tools/mame/README.md)")
class ReplayTest(unittest.TestCase):
    def test_pure_functions_replay(self):
        """Every capture replays clean or is impure at run time: the replay's first bus access
        is to a hardware region (carnevil_tools.board), and the reference interpreter, run on
        the same capture, makes the same first access from the same register state. Anything
        else is an unexplained difference."""
        _smoke, replay = programs()
        dirs = capture_dirs()
        image = exe.load_exe(work_path("part0_files", "GAME.EXE"))
        pure = set(cli.pure_functions(image, functions.read_toml(FUNCTIONS))[0])
        clean, impure, failures = [], [], []
        for d in dirs:
            addr = int(os.path.basename(d), 16)
            if addr not in pure:
                failures.append(f"{addr:08X}: captured but not in the pure list")
                continue
            verdict, detail = classify_replay(replay, d)
            if verdict == "clean":
                clean.append(addr)
            elif verdict == "impure":
                impure.append((addr, detail))
            else:
                failures.append(f"{addr:08X}: {detail}")
        print(f"\nreplay: {len(dirs)} captures, {len(clean)} clean, {len(impure)} impure at run "
              f"time, {len(failures)} unexplained", file=sys.stderr)
        for addr, (access, region) in impure:
            print(f"  impure at run time: {addr:08X}, first bus access {access} ({region})",
                  file=sys.stderr)
        self.assertEqual(failures, [], f"{len(failures)} of {len(dirs)} captures differ:\n"
                         + "\n".join(failures))
        self.assertGreaterEqual(len(clean), MIN_CAPTURES)

    def test_replay_reports_a_difference(self):
        """A copy of a capture whose after-state has a0 and one RAM byte changed fails, naming
        both; the unchanged copy passes."""
        _smoke, replay = programs()
        src = next(d for d in capture_dirs()
                   if subprocess.run([replay, d], capture_output=True).returncode == 0)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        dst = os.path.join(tmp, os.path.basename(src))
        os.mkdir(dst)
        for name in os.listdir(src):  # copyfile: no metadata, which the share refuses
            shutil.copyfile(os.path.join(src, name), os.path.join(dst, name))
        with open(os.path.join(src, "capture.txt"), encoding="utf-8") as f:
            text = f.read()
        base = re.search(r"^base (\S+)$", text, re.M).group(1)
        shutil.copyfile(os.path.join(src, base), os.path.join(dst, base))
        self.assertEqual(subprocess.run([replay, dst], capture_output=True).returncode, 0)
        with open(os.path.join(dst, base), "rb") as f:
            ram = bytearray(f.read())
        for name in ("before.diff", "after.diff"):
            apply_diff(ram, os.path.join(dst, name))
        off = 0x7FFF00
        with open(os.path.join(dst, "after.diff"), "ab") as f:
            f.write(struct.pack("<II", off, 1) + bytes([ram[off] ^ 0xFF]))
        line = re.search(r"^after r .*$", text, re.M).group(0).split()
        line[2 + 4] = "%08X" % (int(line[2 + 4], 16) ^ 1)
        with open(os.path.join(dst, "capture.txt"), "w", encoding="utf-8") as f:
            f.write(re.sub(r"^after r .*$", " ".join(line), text, flags=re.M))
        res = subprocess.run([replay, dst], capture_output=True, text=True)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("a0: MAME", res.stdout)
        self.assertIn(f"RAM differs in 0x{RAM_BASE + off:08X}-0x{RAM_BASE + off:08X}", res.stdout)
        self.assertIn("RAM: 1 bytes differ in 1 ranges", res.stdout)


def classify_replay(replay, d):
    """("clean", None), ("impure", (access, region)) or ("failure", text) for one capture."""
    res = subprocess.run([replay, d], capture_output=True, text=True, timeout=120)
    if res.returncode == 0:
        return "clean", None
    if res.returncode != EXIT_BUS:
        return "failure", f"exit {res.returncode}\n{res.stdout[-1500:]}{res.stderr[-1500:]}"
    first = res.stdout.splitlines()[0]
    m = re.fullmatch(r"bus (read|write) 0x([0-9A-F]{8}) size (\d)(?: value 0x[0-9A-F]{8})?",
                     first)
    if not m:
        return "failure", f"unreadable replay output: {first!r}"
    addr = int(m.group(2), 16)
    region = board.classify(addr) if board.is_kseg01(addr) else "unknown"
    if region in ("ram", "unknown"):
        return "failure", f"bus {m.group(1)} at 0x{addr:08X}, which is no hardware region"
    interp = subprocess.run([replay, "--interp", d], capture_output=True, text=True,
                            timeout=120)
    if interp.returncode != EXIT_BUS or interp.stdout != res.stdout:
        return "failure", (f"first bus access {first} ({region}), but the interpreter "
                           f"(exit {interp.returncode}) does not make the same access from "
                           "the same state")
    return "impure", (f"{m.group(1)} 0x{addr:08X} size {m.group(3)}", region)


def apply_diff(ram, path):
    """Apply a capture diff (tools/mame/README.md) to a bytearray of RAM."""
    with open(path, "rb") as f:
        data = f.read()
    i = 0
    while i < len(data):
        off, n = struct.unpack_from("<II", data, i)
        ram[off:off + n] = data[i + 8:i + 8 + n]
        i += 8 + n


if __name__ == "__main__":
    unittest.main()
