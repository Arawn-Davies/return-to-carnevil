"""carnevil_tools.board: the CarnEvil board's memory map and register names."""
import unittest

from carnevil_tools import board, hwcensus


class Classify(unittest.TestCase):
    def test_classify_kseg1_alias(self):
        for x in (0x0, 0x10, 0x3C):
            self.assertEqual(board.classify(0xA8000000 + x), board.classify(0x88000000 + x))
        self.assertEqual(board.classify(0xA8000000), "voodoo")
        self.assertEqual(board.classify(0xB6000000), "ioasic")
        self.assertEqual(board.classify(0xAC000C18), "gt64010")
        self.assertEqual(board.classify(0xBFC00000), "boot_rom")
        self.assertEqual(board.classify(0x80001000), "ram")

    def test_narrowest_region_wins(self):
        self.assertEqual(board.classify(0xAA0001F0), "ide")
        self.assertEqual(board.classify(0xAA000000), "pci_io")
        self.assertEqual(board.classify(0xB7200000), "cs3")
        self.assertEqual(board.classify(0xA0900000), "unknown")

    def test_regions_well_formed(self):
        names = [r.name for r in board.REGIONS]
        self.assertEqual(len(names), len(set(names)))
        for r in board.REGIONS:
            self.assertLess(r.start, r.end, r.name)
            self.assertLess(r.end, 0x20000001, r.name)

    def test_register_names(self):
        self.assertEqual(board.register_name(0xA8000128), "swapbufferCMD")
        self.assertEqual(board.register_name(0xAC000C18), "INTR_CAUSE")
        # CarnEvil's IOASIC address order once unlocked: bus word 5 is the PIC output port.
        self.assertEqual(board.register_name(0xB6000014), "PICOUT")
        self.assertEqual(board.register_name(0xB6100010), "cmos+0x10")
        # An unaligned GT64010 address is not named after the register it falls in.
        self.assertEqual(board.register_name(0xAC000C14), "PCI_CS_3_BOOT")
        self.assertEqual(board.register_name(0xAC000C17), "reg_0xC17")

    def test_hwcensus_keeps_the_planned_names(self):
        # The plan promises hwcensus.REGIONS and hwcensus.classify.
        self.assertIs(hwcensus.REGIONS, board.REGIONS)
        self.assertIs(hwcensus.classify, board.classify)


if __name__ == "__main__":
    unittest.main()
