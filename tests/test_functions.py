import io
import os
import shutil
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

    def test_jump_into_another_function_is_not_a_start(self):
        # h jumps to a label in the middle of f: reported, not made a function
        a = Asm()
        a.emit(jal("f"), NOP, jal("h"), NOP, jr(RA), NOP)
        a.label("f")
        a.emit(addiu(SP, SP, -8), addiu(V0, ZERO, 1))
        a.label("mid")
        a.emit(addiu(SP, SP, 8), jr(RA), NOP)
        a.label("h")
        a.emit(j("mid"), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        L = a.labels
        res, notes = functions.discover(a.image(), BASE)
        self.assertEqual([f.addr for f in res.functions], [BASE, L["f"], L["h"]])
        self.assertEqual(notes.jumps_out, [(L["h"], L["mid"])])

    def test_prologue_start_inside_a_later_found_function_is_withdrawn(self):
        # f runs 10 instructions before its stack adjustment, more than the hoist
        # limit, and is only found (by g's jal) after the prologue scan proposed f+8.
        a = Asm()
        a.emit(jal("h"), NOP, jr(RA), NOP)
        a.label("f")
        a.emit(*[addiu(V0, A0, k) for k in range(10)])
        a.emit(addiu(SP, SP, -8), addiu(SP, SP, 8), jr(RA), NOP)
        a.label("g")
        a.emit(addiu(SP, SP, -8), jal("f"), NOP, addiu(SP, SP, 8), jr(RA), NOP)
        a.label("h")
        a.emit(jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        L = a.labels
        res, notes = functions.discover(a.image(), BASE)
        found = self.by_addr(res)
        self.assertEqual(sorted(found), [BASE, L["f"], L["g"], L["h"]])
        self.assertEqual(found[L["f"]].sources, {"call"})
        self.assertEqual(found[L["f"]].size, L["g"] - L["f"])
        self.assertEqual(notes.withdrawn, [L["f"] + 8])

    def test_prologue_start_in_a_later_found_delay_slot_is_withdrawn(self):
        # f's stack adjustment sits in a branch delay slot; the prologue scan proposes
        # it before g's jal finds f, which then reaches it as a delay slot.
        a = Asm()
        a.emit(jal("h"), NOP, jr(RA), NOP)
        a.label("f")
        a.emit(beq(A0, ZERO, "out"))
        a.label("slot")
        a.emit(addiu(SP, SP, -8), addiu(SP, SP, 8))
        a.label("out")
        a.emit(jr(RA), NOP)
        a.label("g")
        a.emit(addiu(SP, SP, -8), jal("f"), NOP, addiu(SP, SP, 8), jr(RA), NOP)
        a.label("h")
        a.emit(jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        L = a.labels
        res, notes = functions.discover(a.image(), BASE)
        found = self.by_addr(res)
        self.assertEqual(sorted(found), [BASE, L["f"], L["g"], L["h"]])
        self.assertEqual(found[L["f"]].size, L["g"] - L["f"])
        self.assertEqual(notes.withdrawn, [L["slot"]])

    def test_hoist_over_padding_before_a_later_found_start_is_withdrawn(self):
        # The prologue scan hoists f's start over a padding word that reads only
        # live registers; a later pass finds f itself through a pointer.
        a = Asm()
        a.emit(jal("h"), NOP, jr(RA), NOP)
        a.label("pad")
        a.emit(addiu(6, ZERO, 15))
        a.label("f")
        a.emit(addiu(SP, SP, -8), addiu(SP, SP, 8), jr(RA), NOP)
        a.label("g")
        a.emit(addiu(SP, SP, -8), lui_hi(A0, "f"), addiu_lo(A0, A0, "f"),
               addiu(SP, SP, 8), jr(RA), NOP)
        a.label("h")
        a.emit(jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        L = a.labels
        res, notes = functions.discover(a.image(), BASE)
        found = self.by_addr(res)
        self.assertEqual(sorted(found), [BASE, L["f"], L["g"], L["h"]])
        self.assertEqual(found[L["f"]].sources, {"pointer"})
        self.assertEqual(res.unreached, [(L["pad"], L["f"])])
        self.assertEqual(notes.withdrawn, [L["pad"]])

    def test_refused_pointer_inside_unreached_range(self):
        # A pointer to a frameless leaf in the middle of an unreached range is reported
        a = Asm()
        a.emit(lui_hi(A0, "leaf2"), addiu_lo(A0, A0, "leaf2"), jal("g"), NOP, jr(RA), NOP)
        a.label("dead")
        a.emit(addiu(V0, ZERO, 1), jr(RA), NOP)
        a.label("leaf2")
        a.emit(addiu(V0, ZERO, 2), jr(RA), NOP)
        a.label("g")
        a.emit(jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        L = a.labels
        res, notes = functions.discover(a.image(), BASE)
        self.assertEqual(res.unreached, [(L["dead"], L["g"])])
        self.assertEqual(notes.rejected_pointers, [L["leaf2"]])

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

    def test_reached_words(self):
        analysis = functions.Analysis(BASE, BASE + 0x40, [
            functions.Function(BASE, 0x20, "_start", {"entry"}),
            functions.Function(BASE + 0x20, 0x20, fname(BASE + 0x20), {"call"}),
        ], [], [(BASE + 0x08, BASE + 0x10)])
        words = functions.reached_words(analysis)
        self.assertEqual(list(words), [BASE, BASE + 0x20])
        self.assertEqual(sorted(words[BASE]),
                         [BASE, BASE + 4] + list(range(BASE + 0x10, BASE + 0x20, 4)))
        self.assertEqual(sorted(words[BASE + 0x20]), list(range(BASE + 0x20, BASE + 0x40, 4)))

    def test_stub_block_is_not_code(self):
        # Calls and a jump into a block the program fills at run time: each target is a
        # one-slot "stub" function, and the block's words are never decoded.
        a = Asm()
        a.emit(jal("s1"), NOP, jal("s2"), NOP, jal("g"), NOP, j("s3"), NOP)
        a.label("block")
        a.emit(NOP, NOP)
        a.label("s1")
        a.emit(NOP, NOP)
        a.label("s2")
        a.emit(jr(RA), NOP)  # would be code if the block were descended into
        a.label("s3")
        a.emit(NOP, NOP)
        a.label("after")
        a.emit(addiu(V0, ZERO, 1))  # reachable only by running on from the block
        a.label("g")
        a.emit(jr(RA), NOP)
        a.label("data")
        a.emit(DATA_WORD)
        L = a.labels
        block = (L["block"], L["after"])
        res = functions.analyse(a.image(), BASE, stub_blocks=[block])
        self.assertEqual(
            [(f.addr, f.size, f.sources) for f in res.functions],
            [(BASE, L["block"] - BASE, {"entry"}), (L["s1"], 8, {"stub"}),
             (L["s2"], 8, {"stub"}), (L["s3"], 8, {"stub"}), (L["g"], 8, {"call"})])
        self.assertEqual(res.stub_blocks, [block])
        self.assertEqual(res.unreached, [(L["after"], L["g"])])
        words = functions.reached_words(res)
        for stub in ("s1", "s2", "s3"):
            self.assertEqual(words[L[stub]], set())
        self.assertEqual(sorted(words[BASE]), list(range(BASE, L["block"], 4)))

    def test_jump_table_with_an_entry_outside_the_text_is_unresolved(self):
        # One bad entry would shift every later case: no table is recorded.
        for bad in ("beyond the text", "outside the image"):
            with self.subTest(bad=bad):
                a = self._switch("lw")
                a.emit(*([DATA_WORD] * 15))
                L = a.labels
                k = (L["table"] - BASE) // 4 + 2
                a.items[k] = L["table"] + 4 * 19 if bad == "beyond the text" else 0x90000000
                res, notes = functions.discover(a.image(), BASE)
                self.assertEqual(res.jump_tables, [])
                self.assertEqual(notes.unresolved_jr, [L["jr"]])
                self.assertTrue(all(t < res.text_end for t in (L["case0"], L["jr"])))

    def test_entry_outside_the_image(self):
        a = Asm()
        a.emit(jr(RA), NOP)
        for entry in (BASE - 4, BASE + 8):
            with self.assertRaisesRegex(ValueError, "entry outside the image"):
                functions.analyse(a.image(), entry)

    def test_debug_string_never_names_a_second_start(self):
        a = Asm()
        a.emit(jal("f"), NOP, jr(RA), NOP)
        a.label("f")
        a.emit(lui_hi(A0, "s"), addiu_lo(A0, A0, "s"), jr(RA), NOP)
        a.label("s")
        a.emit(*ascii_words("_start: boot"))
        res = functions.analyse(a.image(), BASE)
        self.assertEqual([f.name for f in res.functions], ["_start", fname(a.labels["f"])])

    def test_toml_roundtrip(self):
        a = self._switch("lw")
        res = functions.analyse(a.image(), BASE)
        res.functions[0].sources.add("manual")
        res.unreached.append((0x80010100, 0x80010108))
        res.stub_blocks.append((0x80010200, 0x80010240))
        res.exe_sha256 = "ab" * 32
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.toml")
            functions.write_toml(res, path)
            self.assertEqual(functions.read_toml(path), res)
            with open(path, encoding="utf-8") as f:
                first = f.read()
            self.assertIn("\nformat = 1\n", first)
            self.assertIn('\nexe_sha256 = "%s"\n' % ("ab" * 32), first)
            functions.write_toml(functions.read_toml(path), path)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), first)

    def test_read_toml_rejects_a_missing_or_unknown_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.toml")
            for header in ("", "format = 2\n"):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(header + 'exe_sha256 = ""\ntext_start = 0\ntext_end = 0\n')
                with self.assertRaisesRegex(ValueError, "format"):
                    functions.read_toml(path)

    def test_read_manual(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.toml")
            with open(path, "w", encoding="utf-8") as f:
                f.write('[[stub_block]]\nstart = 0x80010000\nend = 0x80010010\nreason = "r"\n'
                        '[[manual]]\naddr = 0x80010020\nreason = "seen in MAME"\n')
            self.assertEqual(functions.read_manual(path),
                             ([0x80010020], [(0x80010000, 0x80010010)]))
            with open(path, "w", encoding="utf-8") as f:
                f.write("[[manual]]\naddr = 0x80010020\n")
            with self.assertRaisesRegex(ValueError, "reason"):
                functions.read_manual(path)


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMITTED = os.path.join(REPO, "config", "game.functions.toml")
MANUAL = os.path.join(REPO, "config", "game.manual.toml")
STUB_BLOCK = (0x800C408C, 0x800C42CC)  # [start, end): the boot-ROM stub block


class Cli(unittest.TestCase):
    def test_missing_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with redirect_stderr(err):
                status = cli.main(["functions", os.path.join(tmp, "GAME.EXE"), "-o",
                                   os.path.join(tmp, "f.toml")])
            self.assertEqual(status, 2)
            self.assertIn("not found", err.getvalue())

    def test_default_manual_is_named_after_the_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "game.functions.toml")
            self.assertIsNone(cli.default_manual(out))
            manual = os.path.join(tmp, "game.manual.toml")
            open(manual, "w").close()
            self.assertEqual(cli.default_manual(out), manual)
            self.assertIsNone(cli.default_manual(os.path.join(tmp, "other.toml")))

    def test_missing_manual_is_an_error(self):
        err = io.StringIO()
        with redirect_stderr(err):
            status = cli.main(["functions", "/nonexistent/GAME.EXE", "-o", "/nonexistent/f.toml",
                               "--manual", "/nonexistent/m.toml"])
        self.assertEqual(status, 1)
        self.assertIn("is not a file", err.getvalue())

    @requires_file("part0_files", "GAME.EXE")
    def test_regenerates_the_committed_file(self):
        # The CLI, given the committed manual file next to its output, writes
        # config/game.functions.toml byte for byte.
        path = work_path("part0_files", "GAME.EXE")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "game.functions.toml")
            shutil.copy(MANUAL, os.path.join(tmp, "game.manual.toml"))
            with redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cli.main(["functions", path, "-o", out]), 0)
            with open(out, "rb") as f, open(COMMITTED, "rb") as g:
                self.assertTrue(f.read() == g.read(), "config/game.functions.toml is stale")
        self.assertIn("unknown words reached as code 0", printed.getvalue())
        self.assertIn("3 starts, 1 stub blocks", printed.getvalue())


class GameExe(unittest.TestCase):
    @requires_file("part0_files", "GAME.EXE")
    def test_game_exe(self):
        image = exe.load_exe(work_path("part0_files", "GAME.EXE"))
        manual, stubs = functions.read_manual(MANUAL)
        res = functions.analyse(image, 0x800C4000, manual, stubs)
        # The committed file is this analysis (I2): format, hash, every entry.
        self.assertEqual(functions.read_toml(COMMITTED), res)
        self.assertEqual(res.exe_sha256, exe.GAME_EXE_SHA256)
        self.assertEqual(res.stub_blocks, [STUB_BLOCK])
        addrs = [f.addr for f in res.functions]
        self.assertEqual(addrs, sorted(addrs))
        for f, nxt in zip(res.functions, res.functions[1:]):
            self.assertLessEqual(f.addr + f.size, nxt.addr)
        self.assertLess(res.text_end, 0x801A4224)
        # The one code island in the data: a `break` stub called from the text
        islands = [(f.addr, f.size) for f in res.functions if f.addr >= res.text_end]
        self.assertEqual(islands, [(0x801A355C, 16)])
        # The stub block holds only one-slot stub functions, and nothing else is a stub.
        for f in res.functions:
            in_block = STUB_BLOCK[0] <= f.addr < STUB_BLOCK[1]
            self.assertEqual(f.sources == {"stub"}, in_block, hex(f.addr))
            if in_block:
                self.assertEqual(f.size, 8, hex(f.addr))
        for addr in manual:
            self.assertIn("manual", next(f for f in res.functions if f.addr == addr).sources)
        # No function runs straight into the next one: when they touch, the previous one
        # ends in a path end plus its delay slot. A stub has no code in the file.
        for f, nxt in zip(res.functions, res.functions[1:]):
            end = f.addr + f.size
            if end != nxt.addr or f.sources == {"stub"}:
                continue
            last = mips.decode(image.word(end - 8), end - 8) if f.size >= 8 else None
            self.assertTrue(last is not None and mips.ends_path(last),
                            "0x%08X runs into 0x%08X" % (f.addr, nxt.addr))
        # Labels inside a function are not taken as starts (review round 1)
        for addr in (0x80151BD4, 0x80151CE4, 0x80151F9C, 0x800D1588):
            self.assertNotIn(addr, addrs)
        self.assertIn(0x800C4524, addrs)  # main, reached by _start's tail `j`
        starts = set(addrs)
        unreached = set(range(*STUB_BLOCK, 4))
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
        # Every jump-table target lies inside the function that owns the `jr` (I10).
        for t in res.jump_tables:
            owner = next(f for f in res.functions if f.addr <= t.jr_addr < f.addr + f.size)
            for target in t.targets:
                self.assertTrue(owner.addr <= target < owner.addr + owner.size,
                                "0x%08X: case 0x%08X" % (t.jr_addr, target))


if __name__ == "__main__":
    unittest.main()
