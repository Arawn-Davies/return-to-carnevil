"""The DCS sound board's responder (runtime/hw/dcs_stub.c) against
docs/analysis/dcs-handshake.md and the measured exchange, `mame/m2-facts/dcs-trace.txt`.

The C runs in tests/harness/hw_driver.c (its top comment lists the commands; `dcsreplay`
replays the trace through the `ioasic:` ioctls at its recorded times). The replay test skips
without the trace; the others use synthetic exchanges.
"""
import unittest

from tests import test_hw
from tests import voodoo_timing as vt
from tests.gamedata import requires_file, work_path
from tests.test_loader import BUF, CALLER_RA, ioctl

SOUNDCTL_W, SOUNDCTL_R, SOUNDOUT, SOUNDSTAT, SOUNDIN, SOUNDIN_W = (
    0x6906, 0x6907, 0x6908, 0x6909, 0x690A, 0x6911)
INTSTAT, INTCTL_W, INTCTL_R = 0x690E, 0x690F, 0x6910
FIFO = 0xB3000000
# hardware-usage.md, "By GAME.EXE's own code": the game's sound FIFO writes in attract mode
FIFO_WRITES = 512
RESET_REPLY_NS = 2027000  # dcs_stub.c, DCS_RESET_REPLY_NS


def read(cmd):
    return [ioctl(4, cmd, BUF), f"dump {BUF:08X} 1"]


def write(cmd, value):
    return [ioctl(4, cmd, value)]


def out_words(*words):
    return [c for w in words for c in write(SOUNDOUT, w)]


def board_reset(at):
    """SOUNDCTL 0 then 1, as the game resets the board (dcs-handshake.md, step 3), from `at` ns,
    then the 0x000A reply read and acknowledged once it is due."""
    return ([f"at {at}"] + write(SOUNDCTL_W, 0) + [f"at {at + 1000000}"] + write(SOUNDCTL_W, 1)
            + [f"at {at + 1000000 + RESET_REPLY_NS}"] + read(SOUNDIN) + write(SOUNDIN_W, 0))


class DcsTest(test_hw.HwTestCase):
    def words(self, out):
        return [d[0] for d in out.dumps()]

    def test_no_reply_without_command(self):
        """At power-on one word of the board's own waits (bit 0x40, measured: the trace's first
        SOUNDSTAT read, 0x00C8); once acknowledged, with no command sent, bit 0x40 stays clear
        and 0x80 set, in SOUNDSTAT and INTSTAT, however long the game polls. The power-on word's
        value was never measured, so reading it stops."""
        polls = [c for t in (1000, 1000000, 30000000, 1000000000)
                 for c in [f"at {t}"] + read(SOUNDSTAT) + read(INTSTAT)]
        out, _ = self.drive(read(SOUNDSTAT) + write(SOUNDIN_W, 0) + polls + ["dcs"])
        stat = self.words(out)
        self.assertEqual(stat[0] & 0xC0, 0xC0)
        for s, i in zip(stat[1::2], stat[2::2]):
            self.assertEqual(s & 0xC0, 0x80, hex(s))
            self.assertEqual(i & 0xC0, 0x80, hex(i))
        self.assertEqual(out.all("dcs")[0][0], "0")
        self.stops([ioctl(4, SOUNDIN, BUF)], "0x690A", "not measured", f"ra 0x{CALLER_RA:08X}")

    def test_unknown_command_stops(self):
        """A word the state machine does not know where a command is expected stops, naming
        the word, the board's state and ra; the same words inside a block are data, taken by
        the block's length, counted, and summed into its reply (followed by 0x000A for a boot
        block: dcs-handshake.md, steps 5 and the replies list)."""
        self.stops(out_words(0x1234), "0x1234", "entry", f"ra 0x{CALLER_RA:08X}")
        self.stops(board_reset(0) + out_words(0x55D4), "0x55D4", "boot",
                   f"ra 0x{CALLER_RA:08X}")
        self.stops(board_reset(0) + out_words(0x002A, 0x9999), "0x9999", "program")
        out, _ = self.drive(board_reset(0) + out_words(0x001A, 0, 1, 1, 0x55D4, 0x1234)
                            + read(SOUNDSTAT) + read(SOUNDIN) + write(SOUNDIN_W, 0)
                            + read(SOUNDIN) + write(SOUNDIN_W, 0) + ["dcs"])
        w = self.words(out)
        self.assertEqual(w[0], 0x000A)
        self.assertEqual(w[1] & 0xC0, 0xC0)
        self.assertEqual(w[2:], [(0x55D4 + 0x1234) & 0xFFFF, 0x000A])
        self.assertEqual(out.all("dcs")[0][:2], ["0", "2"])

    def test_block_rules(self):
        """0x001A takes (end - start + 1) data words with space 1 and twice that with space 0;
        0x55D0 and 0x55D1 take (end - start + 1) words with 32-bit addresses; each is answered
        by the 16-bit sum of its data words. 0x002A gets 0x000C and starts the program state,
        where 0x55D4 gets 0x0223 (recorded constants). A guest-sized length is only a count: a
        block of 2^32 words takes words without a reply; end before start or another space
        stops."""
        prog = (board_reset(0) + out_words(0x001A, 5, 6, 0, 1, 2, 3, 0xFFFF)
                + read(SOUNDIN) + write(SOUNDIN_W, 0) + read(SOUNDIN) + write(SOUNDIN_W, 0)
                + out_words(0x002A) + ["at 10000000"] + read(SOUNDIN) + write(SOUNDIN_W, 0))
        out, _ = self.drive(prog + out_words(0x55D0, 0, 0, 0, 2, 0x8000, 0x8000, 7)
                            + read(SOUNDIN) + write(SOUNDIN_W, 0)
                            + out_words(0x55D1, 1, 0xFFFF, 2, 0, 9, 9)
                            + read(SOUNDIN) + write(SOUNDIN_W, 0)
                            + out_words(0x55D4) + ["at 20000000"] + read(SOUNDIN)
                            + write(SOUNDIN_W, 0) + read(SOUNDSTAT) + ["dcs"])
        w = self.words(out)
        self.assertEqual(w[:-1], [0x000A, 0x0005, 0x000A, 0x000C, 0x0007, 0x0012, 0x0223])
        self.assertEqual(w[-1] & 0x8040, 0x8000)
        self.assertEqual(out.all("dcs")[0][:2], ["0", "9"])
        out, _ = self.drive(prog + out_words(0x55D0, 0, 0, 0xFFFF, 0xFFFF, 1, 2, 3)
                            + read(SOUNDSTAT) + ["dcs"])
        self.assertEqual(self.words(out)[-1] & 0x40, 0)
        self.assertEqual(out.all("dcs")[0][:2], ["0", "7"])
        self.stops(board_reset(0) + out_words(0x001A, 6, 5, 1), "0x001A", "end")
        self.stops(board_reset(0) + out_words(0x001A, 5, 6, 2), "0x001A", "space")
        self.stops(prog + out_words(0x55D1, 0, 9, 0, 8), "0x55D1", "end")

    def test_reply_timing(self):
        """The reset reply appears DCS_RESET_REPLY_NS after the releasing SOUNDCTL write and
        no earlier; a reset drops a waiting word; SOUNDCTL reads back what was written."""
        out, _ = self.drive(read(SOUNDCTL_R) + write(SOUNDCTL_W, 0) + read(SOUNDCTL_R)
                            + read(SOUNDSTAT) + ["at 1000000"] + write(SOUNDCTL_W, 0x10001)
                            + read(SOUNDCTL_R) + [f"at {1000000 + RESET_REPLY_NS - 1}"]
                            + read(SOUNDSTAT) + [f"at {1000000 + RESET_REPLY_NS}"]
                            + read(SOUNDSTAT))
        w = self.words(out)
        self.assertEqual(w[:2], [0x0001, 0x0000])
        self.assertEqual(w[2] & 0xC0, 0x80)
        self.assertEqual(w[3], 0x0001)
        self.assertEqual(w[4] & 0x40, 0)
        self.assertEqual(w[5] & 0xC0, 0xC0)

    def test_fifo_and_ip3(self):
        """The sound FIFO's fill shows in SOUNDSTAT (0x08 empty, 0x10 from half full, 0x20
        full at 512 words) and INTSTAT (0x08 empty); IP3 follows INTCTL bit 0 with an INTSTAT
        bit enabled by the same INTCTL bit: INTCTL 0x4008 raises nothing, 0x4009 raises it
        once, the first FIFO word lowers it. A word to a full FIFO stops."""
        out, _ = self.drive(read(INTSTAT) + write(SOUNDIN_W, 0) + read(INTSTAT)
                            + write(INTCTL_W, 0x4008) + ["dcs"] + write(INTCTL_W, 0x4009)
                            + ["dcs"] + [test_hw.wr(4, FIFO, 0)] + ["dcs"] + read(INTSTAT)
                            + read(SOUNDSTAT) + [test_hw.wr(4, FIFO, 0)] * 255
                            + read(SOUNDSTAT) + [test_hw.wr(4, FIFO, 0)] * 256
                            + read(SOUNDSTAT) + read(INTCTL_R))
        w = self.words(out)
        self.assertEqual(w[:3], [0x20C9, 0x2089, 0x2081])
        self.assertEqual([x & 0x38 for x in w[3:6]], [0x00, 0x10, 0x30])
        self.assertEqual(w[6], 0x4009)
        dcs = out.all("dcs")
        self.assertEqual([d[2:] for d in dcs],
                         [["0", "00000000"], ["1", "00000800"], ["1", "00000000"]])
        self.stops([test_hw.wr(4, FIFO, 0)] * 513, "physical 0x13000000", "full")


@requires_file("mame", "m2-facts", "dcs-trace.txt")
class DcsReplayTest(test_hw.HwTestCase):
    def test_replays_mame_exchange(self):
        """Every event of the trace at its time (less the entry's, hardware-usage.md's
        convention), with the IP3 handler's 512 sound FIFO writes after its first INTSTAT
        read: every SOUNDCTL, SOUNDIN, INTSTAT and INTCTL result equals the recorded one; in
        every SOUNDSTAT result the state bits (0x38, the FIFO) equal the recorded ones and each
        timing bit (0x40, 0x80, 0x8000) recorded set is modelled set (the model is never slower
        than the board); 0x40 is modelled only while a word is queued; the model raises IP3
        once, at the event just before the recorded interrupt."""
        path = work_path("mame", "m2-facts", "dcs-trace.txt")
        out, _ = self.drive([f"dcsreplay {path} {vt.ENTRY_NS} {FIFO_WRITES}"])
        reads = {r[0]: r[1:] for r in out.all("dcsread")}
        for kind, total in (("6907", 10), ("690A", 112), ("690E", 2), ("6910", 8)):
            with self.subTest(kind=kind):
                self.assertEqual(reads[kind][:2], [str(total), str(total)], reads[kind])
        self.assertEqual(out.all("dcsstate"), [["1679832", "1679832", "0"]])
        for bit, late, _early, first in out.all("dcsbit"):
            with self.subTest(bit=bit):
                self.assertEqual(late, "0", f"first at trace line {first}")
        self.assertEqual(out.all("dcsorphan"), [["0"]])
        ip3 = out.all("dcsip3")
        self.assertEqual(len(ip3), 1)
        self.assertEqual(out.all("dcsraise"), [[ip3[0][1]]])
        self.assertEqual(len(out.all("dcsfifo")), 1)


if __name__ == "__main__":
    unittest.main()
