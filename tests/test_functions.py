import io
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from carnevil_tools import cli, exe, functions, mips
from tests.gamedata import requires_file, work_path

BASE = 0x80010000
ZERO, AT, V0, A0, A1, SP, RA = 0, 1, 2, 4, 5, 29, 31
DATA_WORD = 0x7FFFFFFF  # reserved encoding: decodes as unknown


def enc_r(op, rs, rt, rd, sa, funct):
    return (op << 26) | (rs << 21) | (rt << 16) | (rd << 11) | (sa << 6) | funct


def enc_i(op, rs, rt, imm):
    return (op << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)


class Asm:
    """A tiny assembler: words or callables (labels, addr) -> word, with labels."""

    def __init__(self, base=BASE):
        self.base = base
        self.items = []
        self.labels = {}

    def here(self):
        return self.base + 4 * len(self.items)

    def label(self, name):
        self.labels[name] = self.here()

    def emit(self, *words):
        self.items.extend(words)

    def image(self):
        words = []
        for i, item in enumerate(self.items):
            addr = self.base + 4 * i
            words.append(item(self.labels, addr) if callable(item) else item)
        data = struct.pack("<%dI" % len(words), *words)
        return exe.ExeImage(0, self.base, data, "")


NOP = 0


def jr(reg):
    return enc_r(0, reg, 0, 0, 0, 0x08)


def addiu(rt, rs, imm):
    return enc_i(0x09, rs, rt, imm)


def sltiu(rt, rs, imm):
    return enc_i(0x0B, rs, rt, imm)


def sll(rd, rt, sa):
    return enc_r(0, 0, rt, rd, sa, 0x00)


def addu(rd, rs, rt):
    return enc_r(0, rs, rt, rd, 0, 0x21)


def lw(rt, off, base):
    return enc_i(0x23, base, rt, off)


def jal(name):
    return lambda labels, addr: (0x03 << 26) | ((labels[name] >> 2) & 0x03FFFFFF)


def j(name):
    return lambda labels, addr: (0x02 << 26) | ((labels[name] >> 2) & 0x03FFFFFF)


def beq(rs, rt, name):
    return lambda labels, addr: enc_i(0x04, rs, rt, (labels[name] - addr - 4) >> 2)


def lui_hi(rt, name):
    return lambda labels, addr: enc_i(0x0F, 0, rt, (labels[name] + 0x8000) >> 16)


def addiu_lo(rt, rs, name):
    return lambda labels, addr: enc_i(0x09, rs, rt, labels[name] & 0xFFFF)


def lw_lo(rt, base, name):
    return lambda labels, addr: enc_i(0x23, base, rt, labels[name] & 0xFFFF)


def word(name):
    return lambda labels, addr: labels[name]


def ascii_words(text):
    raw = text.encode("ascii") + b"\0"
    raw += b"\0" * (-len(raw) % 4)
    return list(struct.unpack("<%dI" % (len(raw) // 4), raw))


def fname(addr):
    return "func_%08X" % addr


class AsmSanity(unittest.TestCase):
    def test_data_word_is_unknown(self):
        self.assertEqual(mips.decode(DATA_WORD, BASE).kind, "unknown")


class Discovery(unittest.TestCase):
    def by_addr(self, analysis):
        return {f.addr: f for f in analysis.functions}

    def test_call_graph(self):
        a = Asm()
        a.label("start")
        a.emit(addiu(SP, SP, -24), jal("f1"), NOP, addiu(SP, SP, 24), jr(RA), NOP)
        a.label("f1")
        a.emit(addiu(SP, SP, -24), jal("f2"), NOP, addiu(SP, SP, 24), jr(RA), NOP)
        a.label("f2")
        a.emit(jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD, 0x12345678)
        res = functions.analyse(a.image(), BASE)
        L = a.labels
        self.assertEqual(res.text_start, BASE)
        self.assertEqual(res.text_end, L["data"])
        self.assertEqual(
            res.functions,
            [
                functions.Function(L["start"], 24, "_start", {"entry"}),
                functions.Function(L["f1"], 24, fname(L["f1"]), {"call"}),
                functions.Function(L["f2"], 8, fname(L["f2"]), {"call"}),
            ],
        )
        self.assertEqual(res.unreached, [])
        self.assertEqual(res.jump_tables, [])

    def test_delay_slot_is_code(self):
        a = Asm()
        a.emit(addiu(V0, ZERO, 1), jr(RA), addiu(V0, V0, 1))
        a.label("data")
        a.emit(DATA_WORD, DATA_WORD)
        res = functions.analyse(a.image(), BASE)
        self.assertEqual(res.text_end, a.labels["data"])
        self.assertEqual(res.functions, [functions.Function(BASE, 12, "_start", {"entry"})])

    def _switch(self, base_form):
        a = Asm()
        a.emit(sltiu(AT, A0, 4), beq(AT, ZERO, "default"), sll(V0, A0, 2), lui_hi(AT, "table"))
        if base_form == "addiu":
            a.emit(addiu_lo(AT, AT, "table"), addu(AT, AT, V0), lw(V0, 0, AT))
        else:  # GCC's form: the low half of the table address is the lw offset
            a.emit(addu(AT, AT, V0), lw_lo(V0, AT, "table"))
        a.label("jr")
        a.emit(jr(V0), NOP)
        for k in range(4):
            a.label("case%d" % k)
            a.emit(beq(ZERO, ZERO, "done"), addiu(V0, ZERO, 10 + k))
        a.label("default")
        a.emit(addiu(V0, ZERO, -1))
        a.label("done")
        a.emit(jr(RA), NOP)
        a.label("table")
        a.emit(*[word("case%d" % k) for k in range(4)])
        a.emit(DATA_WORD)
        return a

    def test_jump_table(self):
        for form in ("addiu", "lw"):
            with self.subTest(form=form):
                a = self._switch(form)
                L = a.labels
                res = functions.analyse(a.image(), BASE)
                self.assertEqual(
                    res.jump_tables,
                    [functions.JumpTable(L["jr"], L["table"], [L["case%d" % k] for k in range(4)])],
                )
                self.assertEqual(res.text_end, L["table"])
                self.assertEqual(res.unreached, [])  # every case block is code
                self.assertEqual([f.addr for f in res.functions], [BASE])

    def test_function_pointer_in_data(self):
        a = Asm()
        a.emit(jal("g"), NOP, jr(RA), NOP)
        a.label("f")
        a.emit(addiu(SP, SP, -8), addiu(SP, SP, 8), jr(RA), NOP)
        a.label("g")
        a.emit(jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD, word("f"))
        L = a.labels
        res = functions.analyse(a.image(), BASE)
        found = self.by_addr(res)
        self.assertEqual(sorted(found), [BASE, L["f"], L["g"]])
        self.assertEqual(found[L["f"]].sources, {"pointer"})
        self.assertEqual(res.unreached, [])

    def test_function_pointer_in_code(self):
        a = Asm()
        a.emit(lui_hi(A0, "f"), addiu_lo(A0, A0, "f"), jal("g"), NOP, jr(RA), NOP)
        a.label("f")
        a.emit(addiu(SP, SP, -8), addiu(SP, SP, 8), jr(RA), NOP)
        a.label("g")
        a.emit(jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        res = functions.analyse(a.image(), BASE)
        self.assertEqual(self.by_addr(res)[a.labels["f"]].sources, {"pointer"})

    def test_data_jal_lookalike_ignored(self):
        a = Asm()
        a.label("start")
        a.emit(addiu(V0, ZERO, 1))
        a.label("mid")
        a.emit(addiu(V0, V0, 2), jr(RA), NOP)
        a.label("data")
        a.emit(jal("data2"), word("mid"))
        a.label("data2")
        a.emit(DATA_WORD)
        res = functions.analyse(a.image(), BASE)
        self.assertEqual([f.addr for f in res.functions], [BASE])
        self.assertEqual(res.text_end, a.labels["data"])

    def test_tail_call(self):
        a = Asm()
        a.emit(jal("f"), NOP, j("g"), NOP)
        a.label("f")
        a.emit(addiu(SP, SP, -8), j("f_tail"), addiu(SP, SP, 8))
        a.label("f_tail")
        a.emit(jr(RA), NOP)
        a.label("g")
        a.emit(addiu(SP, SP, -8), addiu(SP, SP, 8), jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        L = a.labels
        res = functions.analyse(a.image(), BASE)
        found = self.by_addr(res)
        # j into the current function is a branch; j to another function start is a tail call
        self.assertEqual(sorted(found), [BASE, L["f"], L["g"]])
        self.assertEqual(found[L["g"]].sources, {"call"})
        self.assertEqual(found[L["f"]].size, L["g"] - L["f"])

    def test_data_island_unreached(self):
        a = Asm()
        a.emit(jal("f"), NOP, jr(RA), NOP)
        a.label("island")
        a.emit(DATA_WORD)
        a.label("f")
        a.emit(addiu(SP, SP, -8), addiu(SP, SP, 8), jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        L = a.labels
        res = functions.analyse(a.image(), BASE)
        self.assertEqual(res.unreached, [(L["island"], L["f"])])
        self.assertEqual([(f.addr, f.size) for f in res.functions], [(BASE, 16), (L["f"], 16)])

    def test_prologue_in_gap(self):
        a = Asm()
        a.emit(jal("g"), NOP, jr(RA), NOP)
        a.label("pad")
        a.emit(NOP, NOP)
        a.label("f")
        a.emit(addiu(SP, SP, -8), addiu(SP, SP, 8), jr(RA), NOP)
        a.label("g")
        a.emit(jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        L = a.labels
        res = functions.analyse(a.image(), BASE)
        self.assertEqual(self.by_addr(res)[L["f"]].sources, {"prologue"})
        self.assertEqual(res.unreached, [(L["pad"], L["f"])])

    def test_hoisted_prologue(self):
        # GCC schedules loads above the stack adjustment: the function starts at the lui
        a = Asm()
        a.emit(jal("g"), NOP, jr(RA), NOP)
        a.label("dead_leaf")
        a.emit(addiu(V0, ZERO, 1), jr(RA), NOP)
        a.label("f")
        a.emit(lui_hi(V0, "data"), lw_lo(V0, V0, "data"), addiu(SP, SP, -8),
               addiu(SP, SP, 8), jr(RA), NOP)
        a.label("island")
        a.emit(lw(3, -4, 3))  # reads v1, which holds nothing at a function entry
        a.label("h")
        a.emit(addiu(SP, SP, -8), addiu(SP, SP, 8), jr(RA), NOP)
        a.label("g")
        a.emit(jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        L = a.labels
        res = functions.analyse(a.image(), BASE)
        found = self.by_addr(res)
        self.assertEqual(sorted(found), [BASE, L["f"], L["h"], L["g"]])
        self.assertEqual(found[L["f"]].sources, {"prologue"})
        self.assertEqual(res.unreached, [(L["dead_leaf"], L["f"]), (L["island"], L["h"])])

    def test_zero_register_write_is_not_hoisted(self):
        a = Asm()
        a.emit(jal("g"), NOP, jr(RA), NOP)
        a.label("pad")
        a.emit(0x0000003F)  # dsra32 zero,zero,0: padding, not code
        a.label("f")
        a.emit(addiu(SP, SP, -8), addiu(SP, SP, 8), jr(RA), NOP)
        a.label("g")
        a.emit(jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        res = functions.analyse(a.image(), BASE)
        self.assertIn(a.labels["f"], self.by_addr(res))
        self.assertEqual(res.unreached, [(a.labels["pad"], a.labels["f"])])

    def test_code_island_in_data(self):
        # A stub far out in the data, called from the text, is a function but
        # does not move text_end.
        a = Asm()
        a.emit(jal("stub"), NOP, jr(RA), NOP)
        a.label("data")
        a.emit(*([DATA_WORD] * 16))
        a.label("stub")
        a.emit(jr(RA), NOP)
        a.emit(DATA_WORD)
        L = a.labels
        res = functions.analyse(a.image(), BASE)
        self.assertEqual(res.text_end, L["data"])
        self.assertEqual([(f.addr, f.size) for f in res.functions], [(BASE, 16), (L["stub"], 8)])
        self.assertEqual(res.unreached, [])

    def test_manual_entry(self):
        a = Asm()
        a.emit(jal("g"), NOP, jr(RA), NOP)
        a.label("f")
        a.emit(addiu(V0, ZERO, 1), jr(RA), NOP)
        a.label("g")
        a.emit(jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        res = functions.analyse(a.image(), BASE, manual=[a.labels["f"]])
        self.assertEqual(self.by_addr(res)[a.labels["f"]].sources, {"manual"})
        self.assertEqual(res.unreached, [])

    def test_string_naming(self):
        a = Asm()
        a.emit(jal("f"), NOP, jal("g"), NOP, jal("h"), NOP, jr(RA), NOP)
        a.label("f")
        a.emit(lui_hi(A0, "s1"), addiu_lo(A0, A0, "s1"), jr(RA), NOP)
        a.label("g")
        a.emit(lui_hi(A0, "s2"), addiu_lo(A0, A0, "s2"), jr(RA), NOP)
        a.label("h")
        a.emit(lui_hi(A1, "s2"), addiu_lo(A1, A1, "s2"), jr(RA), NOP)
        a.label("s1")
        a.emit(*ascii_words("load_level(): bad file %s\n"))
        a.label("s2")
        a.emit(*ascii_words("sound: init failed"))
        L = a.labels
        res = functions.analyse(a.image(), BASE)
        names = {f.addr: f.name for f in res.functions}
        self.assertEqual(names[BASE], "_start")
        self.assertEqual(names[L["f"]], "load_level")
        # "sound" is referenced from two functions, so neither is renamed
        self.assertEqual(names[L["g"]], fname(L["g"]))
        self.assertEqual(names[L["h"]], fname(L["h"]))

    def test_toml_roundtrip(self):
        a = self._switch("lw")
        res = functions.analyse(a.image(), BASE)
        res.functions[0].sources.add("manual")
        res.unreached.append((0x80010100, 0x80010108))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.toml")
            functions.write_toml(res, path)
            self.assertEqual(functions.read_toml(path), res)
            with open(path, encoding="utf-8") as f:
                first = f.read()
            functions.write_toml(functions.read_toml(path), path)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), first)


class Cli(unittest.TestCase):
    def test_missing_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with redirect_stderr(err):
                status = cli.main(["functions", os.path.join(tmp, "GAME.EXE"), "-o",
                                   os.path.join(tmp, "f.toml")])
            self.assertEqual(status, 2)
            self.assertIn("not found", err.getvalue())

    @requires_file("part0_files", "GAME.EXE")
    def test_writes_analysis(self):
        path = work_path("part0_files", "GAME.EXE")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "f.toml")
            with redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cli.main(["functions", path, "-o", out]), 0)
            self.assertEqual(functions.read_toml(out),
                             functions.analyse(exe.load_exe(path), 0x800C4000))
            self.assertIn("unknown words reached as code 0", printed.getvalue())


class GameExe(unittest.TestCase):
    @requires_file("part0_files", "GAME.EXE")
    def test_game_exe(self):
        image = exe.load_exe(work_path("part0_files", "GAME.EXE"))
        res = functions.analyse(image, 0x800C4000)
        self.assertGreaterEqual(len(res.functions), 1800)
        self.assertLessEqual(len(res.functions), 2600)
        addrs = [f.addr for f in res.functions]
        self.assertEqual(addrs, sorted(addrs))
        for f, nxt in zip(res.functions, res.functions[1:]):
            self.assertLessEqual(f.addr + f.size, nxt.addr)
        self.assertLess(res.text_end, 0x801A4224)
        # The one code island in the data: a `break` stub called from the text
        islands = [(f.addr, f.size) for f in res.functions if f.addr >= res.text_end]
        self.assertEqual(islands, [(0x801A355C, 16)])
        starts = set(addrs)
        unreached = set()
        for lo, hi in res.unreached:
            unreached.update(range(lo, hi, 4))
        for addr in range(res.text_start, res.text_end, 4):
            if addr in unreached:
                continue
            insn = mips.decode(image.word(addr), addr)
            self.assertNotEqual(insn.kind, "unknown", "0x%08X" % addr)
            if insn.mnemonic == "jal" and image.contains(insn.target):
                self.assertIn(insn.target, starts, "jal at 0x%08X" % addr)
        gap = sum(hi - lo for lo, hi in res.unreached)
        self.assertLess(gap, 0.05 * (res.text_end - res.text_start))
        for lo in (0x800C81B4, 0x800CBB6C, 0x800D6EEC, 0x800DB144,
                   0x800E7184, 0x800EC53C, 0x800F1D84):
            self.assertIn(lo, unreached, "data island 0x%08X" % lo)
            inside = [f.name for f in res.functions if f.addr <= lo < f.addr + f.size]
            self.assertEqual(inside, [], "data island 0x%08X" % lo)


if __name__ == "__main__":
    unittest.main()
