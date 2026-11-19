"""Hardware usage census: constant hardware addresses in the reached code of an image.

Results and method are documented in docs/analysis/hardware-usage.md. In short:

- `REGIONS` is the Seattle board's physical memory map as MAME's driver lays it out
  (src/mame/williams/seattle.cpp, the GT64010 windows of src/devices/machine/gt64xxx.cpp,
  and the PCI devices behind them). Regions may nest; `classify` returns the narrowest.
- `census` runs a forward constant propagation over each function's reached words: one
  `lui` feeds every later `addiu`/`ori` and every load/store that uses the register,
  across branches, merged at joins and reset at calls for the caller-saved registers. It
  records each memory access whose base register holds a known constant (or constant +
  register), every constant a register is given, and accesses through words loaded from
  fixed RAM locations; `resolve` turns the latter into hardware accesses where every store
  seen into the location holds the same hardware base. Constant arguments and return
  values are carried between functions (`_summaries`).
- What it does not follow is listed in the document.
"""
import bisect
import hashlib
from dataclasses import dataclass, field
from typing import NamedTuple

from carnevil_tools import exe, functions, mips

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

# Loader services with a constant result. Slot 0x110 (stub 0x800C419C in GAME.EXE, loader
# routine 0x8000E80C) finds the PCI device, writes its BAR0 = 0x08000000 and returns
# 0xA8000000 (0 when no device matches): that is the Voodoo base Glide keeps.
GAME_RETURNS = {0x800C419C: 0xA8000000}

# Constants in reached code at or above physical 0x08000000 that classify as unknown and
# are never used as an address (no load/store base, no jump target). Each is explained in
# docs/analysis/hardware-usage.md, "Constants that are not addresses".
NOT_ADDRESSES = {
    "GAME.EXE": [
        0x8D0D0000, 0x8D0D2CAD, 0x8FE30000, 0x8FE3297E, 0x9AA40000, 0x9AA47BF0,
        0x9C0A0000, 0x9C0A4D5B, 0x9D530000, 0x9D535756, 0x9F4B0000, 0x9F4BA339,
        0x9FAF0000, 0x9FAF1308, 0xAC710000, 0xAC71C590, 0xAD160000, 0xAD161652,
        0xAD510000, 0xAD517F33, 0xAF270000, 0xAF2798CC, 0xAFB00000, 0xAFB0E9BD,
        0xBA930000, 0xBA93A92D, 0xBADB0000, 0xBADBEEF1, 0xBC230000, 0xBC23D70A,
        0xBD270000, 0xBD276B66, 0xBD4C0000, 0xBD4CCCCD, 0xBE190000, 0xBE19999A,
        0xBE4C0000, 0xBE4CCCCD, 0xBEB00000, 0xBEB0C6F7, 0xBF100000, 0xBF10F977,
        0xBFFF0000, 0xBFFFF001, 0xBFFFFFEF,
    ],
    "loader": [],
}


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


# --- propagation ----------------------------------------------------------------------

@dataclass
class Access:
    site: int       # address of the load/store
    function: int   # start of the function holding it
    addr: int       # effective address (virtual, as formed)
    op: str         # "read", "write" or "cache"
    size: int       # bytes (0 for cache/pref)
    value: int | None = None  # constant stored, when the stored register holds one
    indexed: bool = False  # base was constant + a register: addr is the constant part
    via: tuple | None = None  # base loaded from this memory path (see `resolve`)


class Idx(NamedTuple):
    """A constant plus an unknown register (an indexed address)."""
    base: int


class Ptr(NamedTuple):
    """A word loaded from memory location `path`, plus `delta`. path is (A,) for the word
    at constant address A, or (A, k) for the word at offset k of the pointer stored at A.
    A location indexed by a register (table + i) is taken at entry 0.
    indexed: an unknown register was added to the loaded word."""
    path: tuple
    delta: int
    indexed: bool


@dataclass
class Indirect:
    """A load/store through a base loaded from memory: address = *path + offset."""
    site: int
    function: int
    path: tuple
    offset: int
    op: str
    size: int
    indexed: bool


@dataclass
class Store:
    """A store of a known value (a constant, or a Ptr) into memory location `path`."""
    site: int
    function: int
    path: tuple
    value: object


@dataclass
class Formed:
    sites: set = field(default_factory=set)  # instructions giving a register this value
    roles: set = field(default_factory=set)  # "base", "target", "store_value", "operand"
    consumers: set = field(default_factory=set)  # mnemonics that read it
    functions: set = field(default_factory=set)


@dataclass
class Census:
    accesses: list  # Access through a constant (or constant + register) base
    formed: dict    # constant value -> Formed
    indirect: list = field(default_factory=list)  # Indirect
    stores: list = field(default_factory=list)    # Store
    seeds: dict = field(default_factory=dict)     # function -> {arg register: value}
    returns: dict = field(default_factory=dict)   # function or call target -> v0 value
    initial: dict = field(default_factory=dict)   # path (A,) -> the image's word at A


_LOADS = {"lb": 1, "lbu": 1, "lh": 2, "lhu": 2, "lw": 4, "lwu": 4, "lwl": 4, "lwr": 4,
          "ll": 4, "lwc1": 4, "ld": 8, "ldl": 8, "ldr": 8, "lld": 8, "ldc1": 8}
_STORES = {"sb": 1, "sh": 2, "sw": 4, "swl": 4, "swr": 4, "sc": 4, "swc1": 4, "sd": 8,
           "sdl": 8, "sdr": 8, "scd": 8, "sdc1": 8}
_CACHE = frozenset(("cache", "pref"))
_FP_DATA = frozenset(("lwc1", "ldc1", "swc1", "sdc1"))  # rt is an FPR, not a GPR
# Registers a call may change (o32): at, v0-v1, a0-a3, t0-t9, k0-k1, ra.
_CALLER_SAVED = frozenset([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25,
                           26, 27, 31])
_CTI = frozenset(("branch", "branch_likely", "call", "call_reg", "jump", "jump_reg"))


def _value(state, reg):
    return 0 if reg == 0 else state.get(reg)


def _offset(value, imm):
    """value + imm for a known value (constant, Idx or Ptr); None stays None."""
    if isinstance(value, int):
        return (value + imm) & _M32
    if isinstance(value, Idx):
        return Idx((value.base + imm) & _M32)
    if isinstance(value, Ptr):
        return Ptr(value.path, (value.delta + imm) & _M32, value.indexed)
    return None


def _add(a, b):
    """a + b where either may be unknown (None): constant + register is an Idx when the
    constant is a KSEG0/KSEG1 address, a Ptr plus anything stays that Ptr, indexed."""
    if a == 0:
        return b
    if b == 0:
        return a
    if isinstance(a, int) and isinstance(b, int):
        return (a + b) & _M32
    for x, y in ((a, b), (b, a)):
        if isinstance(x, Ptr):
            return _offset(x, y) if isinstance(y, int) else Ptr(x.path, x.delta, True)
        if isinstance(x, Idx):
            return Idx((x.base + y) & _M32) if isinstance(y, int) else x
        if isinstance(x, int) and is_kseg01(x) and y is None:
            return Idx(x)
    return None


def _location(base, imm):
    """Memory path of the word at base + imm, or None: (A,) for a constant (or indexed)
    RAM address A, (A, k) for offset k from a pointer loaded from (A,)."""
    if isinstance(base, int) or isinstance(base, Idx):
        addr = ((base if isinstance(base, int) else base.base) + imm) & _M32
        return (addr,) if is_kseg01(addr) and classify(addr) == "ram" else None
    if isinstance(base, Ptr) and len(base.path) == 1:
        return base.path + (((base.delta + imm) & _M32),)
    return None


_POINTER_LOADS = frozenset(("lw", "ld"))


def _transfer(state, insn):
    """State after insn. Constants go through lui, addiu/daddiu, ori, xori, andi, addu and
    moves; constant + register gives an Idx; a word load from a RAM location (or from a
    pointer loaded from one) gives a Ptr; any other write forgets the register."""
    name = insn.mnemonic
    dest = functions._dest(insn)
    if dest is None or dest == 0 or name in _FP_DATA:
        return state
    out = dict(state)
    rs, rt = _value(state, insn.rs), _value(state, insn.rt)
    value = None
    if name == "lui":
        value = (insn.imm << 16) & _M32
    elif name in ("addiu", "daddiu", "addi", "daddi"):
        value = _offset(rs, insn.imm)
    elif name in ("ori", "xori", "andi") and isinstance(rs, int):
        value = {"ori": rs | insn.imm, "xori": rs ^ insn.imm, "andi": rs & insn.imm}[name]
    elif name in ("addu", "daddu"):
        value = _add(rs, rt)
    elif name == "or" and 0 in (insn.rs, insn.rt):
        value = rt if insn.rs == 0 else rs
    elif name == "or" and isinstance(rs, int) and isinstance(rt, int):
        value = rs | rt
    elif name in _POINTER_LOADS:
        path = _location(rs, insn.imm)
        if path is not None:
            value = Ptr(path, 0, False)
    if value is None:
        out.pop(dest, None)
    else:
        out[dest] = value
    return out


def _base(value):
    return value.base if isinstance(value, Idx) else value


def _stepping(x, y):
    """Two addresses (constant or table + register) in the same hardware region."""
    if not (isinstance(x, (int, Idx)) and isinstance(y, (int, Idx))):
        return False
    bx, by = _base(x), _base(y)
    return _hardware(bx) and region_of(bx) == region_of(by)


def _meet(a, b):
    """Registers both states agree on. A hardware base (or memory pointer) on one path and
    null on the other keeps the base: dereferencing the null would fault, so every access
    that executes sees the base. A constant C on one path and C + register on the other
    (a loop over a table, first with index 0) is C + register. Two addresses in the same
    hardware region, or two offsets from the same loaded pointer, widen to the lower one
    plus a register (a loop stepping through registers or a buffer)."""
    out = {}
    for r in a.keys() | b.keys():
        x, y = a.get(r), b.get(r)
        if x == y:
            out[r] = x
        elif isinstance(x, Idx) and y == x.base or isinstance(y, Idx) and x == y.base:
            out[r] = x if isinstance(x, Idx) else y  # table + i, met first with i = 0
        elif isinstance(x, Ptr) and isinstance(y, Ptr) and x.path == y.path:
            low = min(x.delta, y.delta)  # a pointer stepping on; widen once, not again
            if not (x.indexed and low != x.delta):
                out[r] = Ptr(x.path, low, True)
        elif _stepping(x, y):
            low = min(_base(x), _base(y))  # a hardware address stepping on; widen once
            if not (isinstance(x, Idx) and low != x.base):
                out[r] = Idx(low)
        elif x == 0 and _summarisable(y):
            out[r] = y
        elif y == 0 and _summarisable(x):
            out[r] = x
    return out


class _Function:
    def __init__(self, image, start, words, tables, returns):
        self.image, self.start, self.words, self.tables = image, start, words, tables
        self.returns = returns
        self._decoded = {}

    def insn(self, addr):
        insn = self._decoded.get(addr)
        if insn is None:
            insn = self._decoded[addr] = mips.decode(self.image.word(addr), addr)
        return insn

    def successors(self, addr, state):
        """(successor address, state on entry) pairs of the instruction at addr. A control
        transfer's delay slot is applied here, so its successors are the targets."""
        insn = self.insn(addr)
        kind = insn.kind
        if kind == "unknown" or insn.mnemonic == "eret":
            return []
        if kind not in _CTI:
            return [(addr + 4, _transfer(state, insn))]
        after_branch = _transfer(state, insn)
        if addr + 4 not in self.words:
            return []
        taken = _transfer(after_branch, self.insn(addr + 4))
        if kind in ("branch", "branch_likely"):
            out = [(insn.target, taken)]
            if not functions._always_taken(insn):
                out.append((addr + 8, taken if kind == "branch" else after_branch))
            return out
        if kind in ("call", "call_reg"):
            after = {r: v for r, v in taken.items() if r not in _CALLER_SAVED}
            if kind == "call" and insn.target in self.returns:
                after[2] = self.returns[insn.target]  # v0
            return [(addr + 8, after)]
        if kind == "jump":
            return [(insn.target, taken)]
        table = self.tables.get(addr)
        return [(t, taken) for t in table.targets] if table and insn.rs != 31 else []

    def solve(self, entry):
        states = {self.start: dict(entry)}
        pending = [self.start]
        while pending:
            addr = pending.pop()
            for succ, state in self.successors(addr, states[addr]):
                if succ not in self.words:
                    continue
                old = states.get(succ)
                new = state if old is None else _meet(old, state)
                if old is None or new != old:
                    states[succ] = new
                    pending.append(succ)
        return states


def _reads(insn):
    reads = functions._reads(insn)
    return set() if reads is None else reads - {0}


def _record(fn, addr, insn, state, out):
    """Note what insn does with the known values in state (the state before it)."""
    name = insn.mnemonic
    base = _value(state, insn.rs)
    memory = name in _LOADS or name in _STORES or name in _CACHE
    if base is not None and memory:
        if name in _LOADS:
            op, size = "read", _LOADS[name]
        elif name in _STORES:
            op, size = "write", _STORES[name]
        else:
            op, size = "cache", 0
        stored = None if op != "write" or name in _FP_DATA else _value(state, insn.rt)
        if isinstance(base, Ptr):
            out.indirect[(addr, base)] = Indirect(
                addr, fn.start, base.path, (base.delta + insn.imm) & _M32, op, size,
                base.indexed)
        else:
            indexed = isinstance(base, Idx)
            ea = ((base.base if indexed else base) + insn.imm) & _M32
            value = stored if isinstance(stored, int) else None
            out.accesses[(addr, ea)] = Access(addr, fn.start, ea, op, size, value, indexed)
        path = _location(base, insn.imm)
        if path is not None and isinstance(stored, (int, Ptr, Idx)):
            out.stores[(addr, path, stored)] = Store(addr, fn.start, path, stored)
    formed = out.formed
    for reg in _reads(insn):
        v = _value(state, reg)
        if not isinstance(v, int):
            continue
        f = formed.setdefault(v, Formed())
        f.consumers.add(name)
        if reg == insn.rs and (name in _LOADS or name in _STORES or name in _CACHE):
            f.roles.add("base")
        elif name in _STORES and reg == insn.rt:
            f.roles.add("store_value")
        elif insn.kind in ("jump_reg", "call_reg"):
            f.roles.add("target")
        else:
            f.roles.add("operand")
    dest = functions._dest(insn)
    move = name in ("addu", "daddu", "or") and 0 in (insn.rs, insn.rt)
    if dest and not move and name not in _FP_DATA:
        after = _transfer(state, insn)
        if isinstance(after.get(dest), int):
            f = formed.setdefault(after[dest], Formed())
            f.sites.add(addr)
            f.functions.add(fn.start)


def function_words(analysis):
    """(function start, set of its reached words) for each function of an Analysis."""
    unreached = sorted(analysis.unreached)
    lows = [lo for lo, _hi in unreached]
    for f in sorted(analysis.functions, key=lambda f: f.addr):
        words = set()
        for a in range(f.addr, f.addr + f.size, 4):
            i = bisect.bisect_right(lows, a) - 1
            if i >= 0 and a < unreached[i][1]:
                continue
            words.add(a)
        yield f.addr, words


class _Collect:
    def __init__(self):
        self.accesses, self.formed, self.indirect, self.stores = {}, {}, {}, {}


_ARGS = (4, 5, 6, 7)  # a0-a3
SUMMARY_ROUNDS = 8


def _summarisable(value):
    """Values carried across calls: hardware constants and pointers loaded from memory."""
    if isinstance(value, int):
        return _hardware(value)
    return isinstance(value, Ptr) and not value.indexed


def _agree(values):
    """The one value all of `values` share (nulls aside for returns), or None."""
    distinct = set(values)
    if len(distinct) == 1:
        value = next(iter(distinct))
        return value if _summarisable(value) else None
    return None


def _summaries(image, analysis, tables, returns):
    """Entry states (constant arguments) and return values, to a fixpoint.

    A function gets an argument register at entry when every direct call and tail jump to
    it passes the same hardware constant or memory pointer in that register, and its
    address is never taken (no "pointer" source), so no unseen caller exists. A function
    gets a return value when every `jr ra` returns the same such value in v0, or null."""
    funcs = list(function_words(analysis))
    starts = {f.addr for f in analysis.functions}
    taken = {f.addr for f in analysis.functions if "pointer" in f.sources}
    seeds, rets = {}, dict(returns)
    for _round in range(SUMMARY_ROUNDS):
        calls, results = {}, {}
        for start, words in funcs:
            if start not in words:
                continue
            fn = _Function(image, start, words, tables, rets)
            states = fn.solve(seeds.get(start, {}))
            for addr, state in states.items():
                insn = fn.insn(addr)
                if insn.kind not in ("call", "jump", "jump_reg") or addr + 4 not in words:
                    continue
                after = _transfer(_transfer(state, insn), fn.insn(addr + 4))
                if insn.kind == "jump_reg":
                    if insn.rs == 31:
                        results.setdefault(start, []).append(after.get(2))
                    continue
                if insn.target in starts and not (insn.kind == "jump" and insn.target in words):
                    calls.setdefault(insn.target, []).append(after)
        new_seeds = {}
        for target, sites in calls.items():
            if target in taken:
                continue
            entry = {}
            for reg in _ARGS:
                value = _agree(site.get(reg) for site in sites)
                if value is not None:
                    entry[reg] = value
            if entry:
                new_seeds[target] = entry
        new_rets = dict(returns)
        for start, values in results.items():
            value = _agree(v for v in values if v != 0)
            if value is not None:
                new_rets[start] = value
        if new_seeds == seeds and new_rets == rets:
            break
        seeds, rets = new_seeds, new_rets
    return seeds, rets


def census(image, analysis, returns=None, interprocedural=True):
    """Accesses through known bases, and constants formed, over the reached code.
    returns: call target -> the value it is known to return in v0 (for targets outside
    the image). With interprocedural, constant arguments and return values found by
    `_summaries` are used too."""
    tables = {t.jr_addr: t for t in analysis.jump_tables}
    returns = dict(returns or {})
    seeds, rets = (_summaries(image, analysis, tables, returns) if interprocedural
                   else ({}, returns))
    out = _Collect()
    for start, words in function_words(analysis):
        if start not in words:
            continue
        fn = _Function(image, start, words, tables, rets)
        states = fn.solve(seeds.get(start, {}))
        for addr in sorted(states):
            state = states[addr]
            insn = fn.insn(addr)
            _record(fn, addr, insn, state, out)
            if insn.kind in _CTI and addr + 4 in words:
                _record(fn, addr + 4, fn.insn(addr + 4), _transfer(state, insn), out)
    indirect = [out.indirect[k] for k in sorted(out.indirect, key=lambda k: k[0])]
    stores = [out.stores[k] for k in sorted(out.stores, key=lambda k: (k[0], k[1]))]
    words = {ia.path[0] for ia in indirect} | {s.path[0] for s in stores}
    words |= {s.value.path[0] for s in stores if isinstance(s.value, Ptr)}
    initial = {(a,): image.word(a) for a in words if a % 4 == 0 and image.contains(a)}
    return Census([out.accesses[k] for k in sorted(out.accesses)], out.formed, indirect,
                  stores, seeds, rets, initial)


def _single(values, accept):
    """The one non-null value of a set, if there is exactly one and accept() takes it."""
    rest = values - {0}
    if len(rest) == 1:
        value = next(iter(rest))
        if accept(value):
            return value
    return None


def ram_pointers(c):
    """Memory path -> the RAM address it holds, when every store the census sees into the
    path stores that address (a table + register counts as the table, entry 0) or null."""
    seen = {}
    for s in c.stores:
        value = s.value.base if isinstance(s.value, Idx) else s.value
        if isinstance(value, int):
            seen.setdefault(s.path, set()).add(value)
    out = {}
    for path, values in seen.items():
        value = _single(values, lambda v: is_kseg01(v) and classify(v) == "ram")
        if value is not None:
            out[path] = value
    return out


def _canonical(path, rams):
    """(A, k) -> (P + k,) when (A,) holds the RAM address P."""
    if len(path) == 2 and (path[0],) in rams:
        return (((rams[(path[0],)] + path[1]) & _M32),)
    return path


def holders(c):
    """Memory path -> the hardware base it holds: every store this census sees into the
    path, and the image's initial word there, is the same non-RAM KSEG0/KSEG1 constant
    (or null), directly or by copying a word loaded from another holder. A path through a pointer to a RAM structure is taken
    as the structure's own address (`ram_pointers`). Stores the census cannot see (through
    bases it does not know) are not accounted for."""
    rams = ram_pointers(c)
    seen = {path: {value} for path, value in c.initial.items()}  # initialised data
    copies = []
    for s in c.stores:
        dst = _canonical(s.path, rams)
        if isinstance(s.value, (int, Idx)):
            seen.setdefault(dst, set()).add(s.value)
        elif isinstance(s.value, Ptr) and not s.value.indexed:
            copies.append((_canonical(s.value.path, rams), s.value.delta, dst))

    def accept(value):
        return _hardware(value.base if isinstance(value, Idx) else value)

    while True:
        known = {}
        for path, values in seen.items():
            value = _single(values, accept)
            if value is not None:
                known[path] = value
        changed = False
        for src, delta, dst in copies:
            if src in known:
                value = _offset(known[src], delta)
                if value not in seen.setdefault(dst, set()):
                    seen[dst].add(value)
                    changed = True
        if not changed:
            return known, rams


def _hardware(value):
    return is_kseg01(value) and classify(value) not in ("ram", "unknown")


def resolve(c):
    """Indirect accesses whose base path holds a known hardware base, as Accesses with
    `via` set to the path."""
    known, rams = holders(c)
    out = []
    for ia in c.indirect:
        base = known.get(_canonical(ia.path, rams))
        if base is not None:
            indexed = ia.indexed or isinstance(base, Idx)
            addr = (base.base if isinstance(base, Idx) else base) + ia.offset
            out.append(Access(ia.site, ia.function, addr & _M32, ia.op, ia.size, None,
                              indexed, ia.path))
    return out


def all_accesses(c):
    """Direct accesses and resolved indirect ones, by site."""
    return sorted(c.accesses + resolve(c), key=lambda a: (a.site, a.addr))


def constant_addresses(image, analysis):
    """Address -> code addresses forming it: each lui-based value an addiu/ori (or other
    constant arithmetic) builds, each effective address of a load/store/cache through a
    constant base, and each constant used as a jump target."""
    c = census(image, analysis)
    out = {}
    for a in c.accesses:
        out.setdefault(a.addr, set()).add(a.site)
    for value, f in c.formed.items():
        sites = {s for s in f.sites if mips.decode(image.word(s), s).mnemonic != "lui"}
        if "target" in f.roles:
            sites |= f.sites
        if sites:
            out.setdefault(value, set()).update(sites)
    return {v: sorted(s) for v, s in sorted(out.items())}


# --- the boot ROM's resident loader -----------------------------------------------------

BOOT_ROM_SHA256 = "05346e09d90a8e5aadc5e0aae8bd53b80816e96ea1e649ba548e4c6b07dac4a6"
LOADER_ROM = (0xF20, 0x16B60)  # file offsets of the loader in boot.u32
LOADER_BASE = 0x80000000
LOADER_ENTRY = 0x80004000
SLOTS = 72
VECTORS = (0x000, 0x080, 0x100, 0x180)


def loader_image(path):
    """The loader as it runs: ROM bytes 0xF20-0x16B60 at 0x80000000 (boot-handoff.md)."""
    with open(path, "rb") as f:
        raw = f.read()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != BOOT_ROM_SHA256:
        raise exe.UnknownRevisionError(
            "unknown boot ROM: expected sha256 %s, got %s" % (BOOT_ROM_SHA256, digest))
    lo, hi = LOADER_ROM
    return exe.ExeImage(0, LOADER_BASE, raw[lo:hi], digest)


def slot_targets(image):
    """Slot offset -> the routine its `j` jumps to, for the 72 slots at 0x80000000."""
    out = {}
    for k in range(SLOTS):
        addr = image.load_addr + 8 * k
        insn = mips.decode(image.word(addr), addr)
        if insn.mnemonic != "j":
            raise ValueError("slot 0x%03X is not a j" % (8 * k))
        out[8 * k] = insn.target
    return out


def analyse_loader(image):
    """Function discovery over the loader from its entry, the 72 slot targets, the four
    exception vectors and the routines of its device-driver table (reached only through
    that table, so discovery would otherwise guess their starts from prologues)."""
    manual = set(slot_targets(image).values()) | {LOADER_BASE + v for v in VECTORS}
    for _name, routines in loader_devices(image):
        manual.update(routines.values())
    return functions.discover(image, LOADER_ENTRY, sorted(manual))


def call_graph(image, analysis):
    """Function start -> starts it reaches by a direct call (jal/bal) or a `j` out of
    itself, and the set of functions holding an indirect call (jalr) or unresolved jr."""
    starts = sorted(f.addr for f in analysis.functions)
    edges, indirect = {}, set()
    tables = {t.jr_addr for t in analysis.jump_tables}
    for start, words in function_words(analysis):
        out = edges.setdefault(start, set())
        for a in words:
            insn = mips.decode(image.word(a), a)
            if insn.kind == "call_reg" or (insn.kind == "jump_reg" and insn.rs != 31
                                           and a not in tables):
                indirect.add(start)
            target = insn.target
            if target is None or insn.kind not in ("call", "jump"):
                continue
            if insn.kind == "jump" and target in words:
                continue
            i = bisect.bisect_right(starts, target) - 1
            if i >= 0:
                out.add(starts[i])
    return edges, indirect


DEVICE_TABLE = 0x80013530  # the loader's device drivers: 7 routines, then a name pointer
DEVICE_SLOTS = ("open", "close", "read", "write", "init", "unused", "ioctl")
NO_OP = 0x80006984  # the loader's shared "return 0" routine in unused driver slots


def loader_devices(image):
    """[(name, {slot: routine})] of the loader's driver table, empty slots left out."""
    out = []
    addr = DEVICE_TABLE
    while image.contains(addr + 28):
        routines = [image.word(addr + 4 * k) for k in range(7)]
        name_ptr = image.word(addr + 28)
        if not any(routines) or not image.contains(name_ptr):
            break
        name = bytearray()
        a = name_ptr
        while image.contains(a) and image.data[a - image.load_addr]:
            name.append(image.data[a - image.load_addr])
            a += 1
        out.append((name.decode("ascii"), {s: r for s, r in zip(DEVICE_SLOTS, routines)
                                           if r != NO_OP}))
        addr += 32
    return out


def loader_reach(image, analysis, game_slots):
    """Function start -> labels of what reaches it through direct calls and tail jumps:
    "game" (one of the slots in game_slots), "vector" (an exception vector: runs while the
    game runs), "slot" (one of the other slots), "boot" (the loader's entry), and
    "driver:<name>" (a routine of that device driver, which the file slots call through
    the driver table)."""
    edges, _indirect = call_graph(image, analysis)
    slots = slot_targets(image)
    roots = {"game": [slots[s] for s in game_slots],
             "vector": [LOADER_BASE + v for v in VECTORS] + [slots[v] for v in VECTORS],
             "slot": [t for s, t in slots.items() if s not in game_slots and s not in VECTORS],
             "boot": [LOADER_ENTRY]}
    for name, routines in loader_devices(image):
        roots["driver:" + name] = list(routines.values())
    out = {}
    for label, starts in roots.items():
        for f in closure(edges, starts):
            out.setdefault(f, set()).add(label)
    return out


def closure(edges, roots):
    seen, stack = set(), list(roots)
    while stack:
        f = stack.pop()
        if f in seen:
            continue
        seen.add(f)
        stack.extend(edges.get(f, ()))
    return seen
