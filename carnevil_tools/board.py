"""Facts about the CarnEvil board (Midway Seattle): its physical memory map and the names
of the registers in it.

`REGIONS` is the board's physical memory map as MAME's driver lays it out
(src/mame/williams/seattle.cpp, the GT64010 windows of src/devices/machine/gt64xxx.cpp,
and the PCI devices behind them). Regions may nest; `classify` returns the narrowest.
docs/analysis/hardware-usage.md gives the evidence for each entry.
"""
from typing import NamedTuple

_M32 = 0xFFFFFFFF


class Region(NamedTuple):
    name: str
    start: int  # physical, inclusive
    end: int    # physical, exclusive


# Physical map of the CarnEvil board. Windows are the GT64010 decode set by the boot ROM
# (MAME seattle.cpp, "Carnevil config"); devices are MAME's handlers inside them. The
# Voodoo's PCI base (0x08000000) and the IDE's legacy I/O placement are what the boot
# software programs; docs/analysis/hardware-usage.md gives the evidence.
REGIONS = [
    Region("ram", 0x00000000, 0x00800000),
    Region("pci_mem", 0x08000000, 0x0A000000),
    Region("voodoo", 0x08000000, 0x08400000),
    Region("voodoo_lfb", 0x08400000, 0x08800000),
    Region("voodoo_texture", 0x08800000, 0x09000000),
    Region("pci_io", 0x0A000000, 0x0C000000),
    Region("ide", 0x0A0001F0, 0x0A0001F8),
    Region("ide_cs1", 0x0A0003F4, 0x0A0003F8),
    Region("ide2", 0x0A000170, 0x0A000178),
    Region("ide2_cs1", 0x0A000374, 0x0A000378),
    Region("ide_bmdma", 0x0A000F00, 0x0A000F10),
    Region("gt64010", 0x0C000000, 0x0C001000),
    Region("cs0", 0x10000000, 0x12000000),
    Region("cs1", 0x12000000, 0x14000000),
    Region("sound_fifo", 0x13000000, 0x13000004),
    Region("cs2", 0x14000000, 0x16000000),
    Region("analog_port", 0x14000000, 0x14000004),
    Region("cs3", 0x16000000, 0x18000000),
    Region("ioasic", 0x16000000, 0x16000040),
    Region("cmos", 0x16100000, 0x16120000),
    Region("carnevil_gun", 0x16800000, 0x16800020),
    Region("cmos_protect", 0x17000000, 0x17000004),
    Region("watchdog", 0x17100000, 0x17100004),
    Region("interrupt_enable", 0x17300000, 0x17300004),
    Region("interrupt_config", 0x17400000, 0x17400004),
    Region("interrupt_state", 0x17500000, 0x17500004),
    Region("interrupt_state2", 0x17600000, 0x17600004),
    Region("vblank_clear", 0x17700000, 0x17700004),
    Region("cs3_nop", 0x17800000, 0x17800004),
    Region("status_leds", 0x17900000, 0x17900004),
    Region("asic_reset", 0x17F00000, 0x17F00004),
    Region("boot_rom", 0x1FC00000, 0x1FC80000),
    Region("update_rom", 0x1FD00000, 0x1FE00000),
]


def is_kseg01(addr):
    return 0x80000000 <= addr & _M32 < 0xC0000000


def physical(addr):
    return addr & 0x1FFFFFFF if is_kseg01(addr) else addr & _M32


_BY_SIZE = sorted(REGIONS, key=lambda r: r.end - r.start)


def region_of(addr):
    """The narrowest Region holding addr (KSEG0/KSEG1 bits stripped), or None."""
    p = physical(addr)
    for r in _BY_SIZE:
        if r.start <= p < r.end:
            return r
    return None


def classify(addr):
    """Region name of addr after stripping the KSEG0/KSEG1 bits, or "unknown"."""
    r = region_of(addr)
    return r.name if r else "unknown"


# --- register names (facts from the MAME sources cited in the document) ---------------

_VOODOO = {
    0x000: "vdstatus", 0x008: "vertexAx", 0x00C: "vertexAy", 0x010: "vertexBx",
    0x014: "vertexBy", 0x018: "vertexCx", 0x01C: "vertexCy", 0x020: "startR",
    0x024: "startG", 0x028: "startB", 0x02C: "startZ", 0x030: "startA", 0x034: "startS",
    0x038: "startT", 0x03C: "startW", 0x040: "dRdX", 0x044: "dGdX", 0x048: "dBdX",
    0x04C: "dZdX", 0x050: "dAdX", 0x054: "dSdX", 0x058: "dTdX", 0x05C: "dWdX",
    0x060: "dRdY", 0x064: "dGdY", 0x068: "dBdY", 0x06C: "dZdY", 0x070: "dAdY",
    0x074: "dSdY", 0x078: "dTdY", 0x07C: "dWdY", 0x080: "triangleCMD",
    0x088: "fvertexAx", 0x08C: "fvertexAy", 0x090: "fvertexBx", 0x094: "fvertexBy",
    0x098: "fvertexCx", 0x09C: "fvertexCy", 0x0A0: "fstartR", 0x0A4: "fstartG",
    0x0A8: "fstartB", 0x0AC: "fstartZ", 0x0B0: "fstartA", 0x0B4: "fstartS",
    0x0B8: "fstartT", 0x0BC: "fstartW", 0x0C0: "fdRdX", 0x0C4: "fdGdX", 0x0C8: "fdBdX",
    0x0CC: "fdZdX", 0x0D0: "fdAdX", 0x0D4: "fdSdX", 0x0D8: "fdTdX", 0x0DC: "fdWdX",
    0x0E0: "fdRdY", 0x0E4: "fdGdY", 0x0E8: "fdBdY", 0x0EC: "fdZdY", 0x0F0: "fdAdY",
    0x0F4: "fdSdY", 0x0F8: "fdTdY", 0x0FC: "fdWdY", 0x100: "ftriangleCMD",
    0x104: "fbzColorPath", 0x108: "fogMode", 0x10C: "alphaMode", 0x110: "fbzMode",
    0x114: "lfbMode", 0x118: "clipLeftRight", 0x11C: "clipLowYHighY", 0x120: "nopCMD",
    0x124: "fastfillCMD", 0x128: "swapbufferCMD", 0x12C: "fogColor", 0x130: "zaColor",
    0x134: "chromaKey", 0x140: "stipple", 0x144: "color0", 0x148: "color1",
    0x14C: "fbiPixelsIn", 0x150: "fbiChromaFail", 0x154: "fbiZfuncFail",
    0x158: "fbiAfuncFail", 0x15C: "fbiPixelsOut", 0x200: "fbiInit4", 0x204: "vRetrace",
    0x208: "backPorch", 0x20C: "videoDimensions", 0x210: "fbiInit0", 0x214: "fbiInit1",
    0x218: "fbiInit2", 0x21C: "fbiInit3", 0x220: "hSync", 0x224: "vSync",
    0x228: "clutData", 0x22C: "dacData", 0x230: "maxRgbDelta", 0x300: "textureMode",
    0x304: "tLOD", 0x308: "tDetail", 0x30C: "texBaseAddr", 0x310: "texBaseAddr_1",
    0x314: "texBaseAddr_2", 0x318: "texBaseAddr_3_8", 0x31C: "trexInit0",
    0x320: "trexInit1",
}

_GT64010 = {
    0x000: "CPU_CONFIG", 0x008: "R1_0_LO", 0x010: "R1_0_HI", 0x018: "R3_2_LO",
    0x020: "R3_2_HI", 0x028: "CS_2_0_LO", 0x030: "CS_2_0_HI", 0x038: "CS_3_BOOT_LO",
    0x040: "CS_3_BOOT_HI", 0x048: "PCI_IO_LO", 0x050: "PCI_IO_HI", 0x058: "PCI_MEM0_LO",
    0x060: "PCI_MEM0_HI", 0x068: "INTERNAL_SPACE", 0x070: "BUSERR_LO", 0x078: "BUSERR_HI",
    0x400: "RAS0_LO", 0x404: "RAS0_HI", 0x408: "RAS1_LO", 0x40C: "RAS1_HI",
    0x410: "RAS2_LO", 0x414: "RAS2_HI", 0x418: "RAS3_LO", 0x41C: "RAS3_HI",
    0x420: "CS0_LO", 0x424: "CS0_HI", 0x428: "CS1_LO", 0x42C: "CS1_HI", 0x430: "CS2_LO",
    0x434: "CS2_HI", 0x438: "CS3_LO", 0x43C: "CS3_HI", 0x440: "CSBOOT_LO",
    0x444: "CSBOOT_HI", 0x448: "DRAM_CONFIG", 0x44C: "DRAM_BANK0", 0x450: "DRAM_BANK1",
    0x454: "DRAM_BANK2", 0x458: "DRAM_BANK3", 0x45C: "DEVICE_BANK0",
    0x460: "DEVICE_BANK1", 0x464: "DEVICE_BANK2", 0x468: "DEVICE_BANK3",
    0x46C: "DEVICE_BOOT", 0x470: "ADDRESS_ERROR", 0x860: "DMA_ARBITER",
    0x864: "TIMER_CONTROL", 0xC00: "PCI_COMMAND", 0xC04: "PCI_TIMEOUT", 0xC08: "PCI_R1_0",
    0xC0C: "PCI_R3_2", 0xC10: "PCI_CS_2_0", 0xC14: "PCI_CS_3_BOOT", 0xC18: "INTR_CAUSE",
    0xC1C: "CPU_MASK", 0xC24: "PCI_MASK", 0xCF8: "CONFIG_ADDRESS", 0xCFC: "CONFIG_DATA",
}
for _k in range(4):
    _GT64010[0x800 + 4 * _k] = "DMA%d_COUNT" % _k
    _GT64010[0x810 + 4 * _k] = "DMA%d_SOURCE" % _k
    _GT64010[0x820 + 4 * _k] = "DMA%d_DEST" % _k
    _GT64010[0x830 + 4 * _k] = "DMA%d_NEXT" % _k
    _GT64010[0x840 + 4 * _k] = "DMA%d_CONTROL" % _k
    _GT64010[0x850 + 4 * _k] = "TIMER%d_COUNT" % _k

# MAME midwayic.cpp register numbers, and CarnEvil's order of them on the bus once the
# IOASIC is unlocked (SHUFFLE_CARNEVIL): bus word i is register IOASIC_SHUFFLE[i].
IOASIC_REGS = ("PORT0", "PORT1", "PORT2", "PORT3", "UARTCONTROL", "UARTOUT", "UARTIN",
               "COIN", "SOUNDCTL", "SOUNDOUT", "SOUNDSTAT", "SOUNDIN", "PICOUT", "PICIN",
               "INTSTAT", "INTCTL")
IOASIC_SHUFFLE = (0x7, 0x3, 0x2, 0x0, 0x1, 0xC, 0xD, 0xE, 0xF, 0x4, 0x5, 0x6,
                  0x8, 0x9, 0xA, 0xB)

_IDE = ("data", "error/features", "sector count", "sector number", "cylinder low",
        "cylinder high", "drive/head", "status/command")


def register_name(addr):
    """A short name for the register at addr, from the MAME sources; offset if unnamed."""
    r = region_of(addr)
    if r is None:
        return "unknown"
    off = physical(addr) - r.start
    if r.name == "voodoo":
        reg = off & 0x3FC
        if 0x160 <= reg < 0x1E0:
            name = "fogTable[%d]" % ((reg - 0x160) // 4)
        elif 0x324 <= reg < 0x384:
            name = "nccTable[%d]" % ((reg - 0x324) // 4)
        else:
            name = _VOODOO.get(reg, "reg_0x%03X" % reg)
        chips = (off >> 10) & 0xF
        if chips:
            name += " chips=0x%X" % chips
        if off & 0x200000:
            name += " (alternate map)"
        if off & 0x100000:
            name += " (byte-swizzled)"
        return name
    if r.name == "gt64010":
        return _GT64010.get(off & ~3, "reg_0x%03X" % off)
    if r.name == "ioasic":
        return IOASIC_REGS[IOASIC_SHUFFLE[off // 4]]
    if r.name in ("ide", "ide2"):
        return _IDE[off]
    if r.name in ("ide_cs1", "ide2_cs1"):
        return "alt status/device control" if off == 2 else "+0x%X" % off
    if r.name == "carnevil_gun":
        return ("P1 X low", "P1 X high", "P1 Y low", "P1 Y high",
                "P2 X low", "P2 X high", "P2 Y low", "P2 Y high")[off // 4]
    if off == 0 and r.end - r.start <= 4:
        return r.name
    return "%s+0x%X" % (r.name, off)
