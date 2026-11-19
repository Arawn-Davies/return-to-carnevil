# Hardware usage census

Which hardware the code touches, and how. There are two layers, and the runtime has to deal
with both:

1. registers `GAME.EXE` touches itself;
2. registers the boot ROM's resident loader touches on the game's behalf, when the game calls
   one of the 29 loader services of the stub block (`boot-handoff.md`), or when an interrupt
   or exception runs the loader's vectors while the game runs.

`python3 -m carnevil_tools hwcensus <GAME.EXE>` and `python3 -m carnevil_tools hwcensus
--loader <boot.u32>` print the per-region summaries this page is written from
(`carnevil_tools/hwcensus.py`). Addresses below are physical unless written as 0x8.../0xA...;
register names are MAME's. No instruction listings.

"Measured" means counted by the census over reached code. "Read" means read from the code by
hand (with the `disasm` command), and says where. "Inferred" is labelled as such.

## Sources

- MAME, read at master `d19b5702bdbd` (2026-11-19). Files and the last commit that touched
  each: `src/mame/williams/seattle.cpp` (774a180, 2026-08-02; memory map, CS3 devices,
  interrupt wiring, CarnEvil configuration), `src/mame/williams/midwayic.cpp` and `.h`
  (b33a6ac, 2026-05-23; IOASIC registers, CarnEvil's register order, the serial PIC
  protocol), `src/devices/machine/gt64xxx.cpp` (4f583c8, 2026-10-31; GT64010 registers and
  windows), `src/devices/video/voodoo_regs.h` (13727e1, 2023-12-10; register names),
  `src/devices/video/voodoo.cpp`, `voodoo_pci.cpp` and `src/devices/machine/pci-ide.cpp`
  (Voodoo address decode, its 16 MB BAR, IDE legacy placement). MAME is BSD-3-Clause/GPL by
  file; only facts (ranges, offsets, names) were taken, no code.
- `GAME.EXE` (SHA-256 `f9455ce8...d3a6`) with `config/game.functions.toml`; `boot.u32`
  (SHA-256 `05346e09...c4a6`): the loader image is ROM bytes 0xF20 to 0x16B60 at 0x80000000.
- The MAME run 3 NVRAM of Task 5 (`mame/nvram-carnevil/` on the share). No new MAME run was
  made for this task (see "Not confirmed").

## Method

**Regions** (`hwcensus.REGIONS`, physical, narrowest match wins). The GT64010 windows are
those the boot ROM programs for CarnEvil (seattle.cpp, "Carnevil config"): RAM 0x00000000
(8 MB fitted), PCI memory 0x08000000 to 0x09FFFFFF, PCI I/O 0x0A000000 to 0x0BFFFFFF, CS0
0x10000000, CS1 0x12000000, CS2 0x14000000, CS3 0x16000000 to 0x17FFFFFF, boot ROM
0x1FC00000 (512 KB), update ROM 0x1FD00000. Inside them:

- GT64010 registers at 0x0C000000 (4 KB). MAME's reset default is 0x14000000; the reset stub
  moves them (it programs 0x0C000448 to 0x0C000458, `boot-handoff.md`).
- Voodoo at 0x08000000 (16 MB BAR): registers 0x08000000, linear frame buffer (LFB)
  0x08400000, texture memory 0x08800000. The address is fixed by the loader: its PCI set-up
  service (slot 0x110, routine 0x8000E80C, read) finds the device, writes BAR0 = 0x08000000
  and returns 0xA8000000.
- IDE (PC87415, legacy placement under 0x0A000000): task file 0x0A0001F0 to 0x0A0001F7,
  alternate status/device control 0x0A0003F6, bus-master DMA 0x0A000F00.
- CS1: the IOASIC sound FIFO at 0x13000000. CS2: an analog port at 0x14000000 (Flagstaff
  only in MAME). CS3: IOASIC 0x16000000 (16 registers, one per 32-bit word), CMOS 0x16100000
  to 0x1611FFFF (one byte per word, 32 KB), CarnEvil gun 0x16800000 (8 registers), CMOS
  write enable 0x17000000, watchdog 0x17100000, interrupt enable 0x17300000, interrupt
  configuration 0x17400000, interrupt state 0x17500000 and 0x17600000, VBLANK clear
  0x17700000, a no-op register 0x17800000, status LEDs 0x17900000, ASIC reset 0x17F00000.
- IOASIC names follow CarnEvil's register order once the IOASIC is unlocked (MAME
  `SHUFFLE_CARNEVIL`): bus word 0 COIN, 1 PORT3, 2 PORT2, 3 PORT0 (DIP switches), 4 PORT1,
  5 PICOUT, 6 PICIN, 7 INTSTAT, 8 INTCTL, 9 UARTCONTROL, 10 UARTOUT, 11 UARTIN, 12 SOUNDCTL,
  13 SOUNDOUT, 14 SOUNDSTAT, 15 SOUNDIN.

**Address formation.** Per function, over its reached words, a forward propagation of
register values:

- constants through `lui`, `addiu`/`daddiu`, `ori`/`xori`/`andi`, `addu` and moves, so one
  `lui` feeds every later access through that register, across branches and delay slots;
- a constant plus an unknown register (a table or buffer index) is "indexed": the access is
  counted at the constant part;
- a word loaded from a fixed RAM address A (or from offset k of a pointer loaded from A) is
  tracked as "the word at A" (or "at A, k"). When every store the census sees into that
  location, and the image's initial word there, is the same hardware base, accesses through
  the loaded word are counted against that base ("through a base held in memory");
- interprocedural: a function whose every direct caller passes the same hardware constant
  or loaded pointer in a0 to a3, and whose address is never taken, starts with it; a
  function whose every `jr ra` returns the same such value (or null) returns it. Slot
  0x110's result is given (0xA8000000);
- joins keep a register only when all paths agree, except: null on one path and a base on
  the other keeps the base (a null dereference would fault); a hardware address that steps
  through a loop widens once to "indexed"; calls clear the caller-saved registers.

What it does not follow: bases computed from arguments that callers do not agree on, bases
behind more than two loads, pointers into tables whose index is not constant (taken at entry
0), locations written through bases the census cannot see, and indirect calls. A register
reached only that way is missing below; "Not confirmed" lists where that is known to matter.

**Loader reachability.** The loader was analysed with function discovery from its entry, the
72 slot targets, the four exception vectors and the 36 routines of its device-driver table at
0x80013530 (13 drivers, each 7 routine slots followed by a name: open, close, read, write,
init, unused, ioctl; unused slots point at a shared return-0 routine). The names are wdog,
led, ioasic, sysctrl, pic, timer, tty, sound, cmos, mdiag, disk, file and scsi. Each register's functions are labelled with what reaches them by direct calls and
tail jumps: `game` (the 29 slots GAME.EXE calls), `vector` (the exception vectors, which run
while the game runs), `slot` (the other 39 slots), `boot` (the loader's own start-up, which
has run before GAME.EXE), and `driver:<name>`. The file slots call drivers through the table,
so `driver:` labels stand for "reached when the game uses that device". Limits: labels are the
union over a register's functions; jumps through the handler tables are not followed.

## GAME.EXE

Measured, 2,091 functions, `GAME_RETURNS` applied. Regions with no access at all: GT64010,
PCI I/O and IDE, CMOS and its write enable, the security PIC registers, the interrupt
controller registers, watchdog, LEDs. The game reaches all of those through the loader only.

**Voodoo registers** (0x08000000): 96 sites, 82 of them through the base held in memory, 26
registers, in functions 0x8014FB78 to 0x80168C48 (Glide and the code around it). The base is stored by the board-mapping
code at 0x801E5DE0 (0x80157018, after slot 0x110) and at 0x801E756C, with the LFB and
texture bases at 0x801E7574 (base + 0x400000) and 0x801E7570 (base + 0x800000), by
0x80161258.

- vdstatus 0x000: read, 13 sites, 6 functions (0x8015B1B0, 0x8015B498, 0x8015EEAC,
  0x8015F0C8, 0x801624D4, 0x80162570)
- fbzColorPath 0x104, fogMode 0x108, stipple 0x140, color0 0x144, color1 0x148: write and
  read, 0x80160340 and 0x801615E8
- alphaMode 0x10C: write 2, read 1; 0x80160340, 0x801615E8
- fbzMode 0x110: write 6, read 1; 6 functions
- lfbMode 0x114: write 7, read 2; 8 functions (0x80160340, 0x80160D40, 0x80160DBC,
  0x80160E54, 0x80160EEC, 0x80160F64 and two more)
- clipLeftRight 0x118, clipLowYHighY 0x11C: write 2, read 1; 0x8015F23C, 0x80160340,
  0x801615E8
- fastfillCMD 0x124: write, 0x8015EC5C
- swapbufferCMD 0x128: write, 0x8015EEAC (constant base)
- fogColor 0x12C, chromaKey 0x134: write, 0x80160340; zaColor 0x130: write, 3 functions
- fbiInit3 0x21C: read 4, 0x8015C45C
- clutData 0x228: write 2, 0x8014FB78 and 0x8014FD7C (constant base)
- textureMode 0x300, tLOD 0x304: write 10 each; texBaseAddr 0x30C: write 9; tDetail 0x308:
  write 2; texBaseAddr_1/_2/_3_8 0x310 to 0x318: write 1 each; nccTable[4] 0x334: write 1.
  Texture-unit code (0x80160340, 0x80163864, 0x80163F74, 0x80165124, 0x80165B7C, 0x801661C4,
  0x80166814, 0x80168558), indexed by TMU

Not seen: the triangle set-up registers 0x008 to 0x100 (vertex, start, gradient and the
triangle commands), nopCMD, fogTable, the fbiInit0/1/2/4 and video timing registers, the
counters 0x14C to 0x15C. See "Not confirmed".

**Voodoo LFB** (0x08400000): 45 sites, 16-bit accesses.
- 0x8015BF58 and 0x8015BF08: fixed-pixel writes and reads with test patterns (0xDEAD,
  0x5A5A, 0x55AA, 0xAA55, 0xFFFF, 0) at 14 constant addresses across the buffer: a frame
  buffer memory test (inferred from the patterns).
- 0x80167390, 0x8016754C: indexed reads and writes through the LFB base held at 0x801E7574.

**Voodoo texture memory** (0x08800000): 11 writes, indexed, 9 through the texture base held
at 0x801E7570; 0x80165B7C, 0x801661C4, 0x80168558, 0x80168C48 (texture download).

**IOASIC sound FIFO** (0x13000000): 3 writes in 0x8013FF78, through the base the image's data
word 0x801A25CC holds (0xB3000000): the game feeds the DCS2 FIFO itself.

**IOASIC** (0x16000000): one register only, SOUNDSTAT (bus word 14, 0x16000038): 13 writes in
0x801470A4, 0x80147130, 0x801477B4, small constants 0 to 5 and more. MAME treats SOUNDSTAT as
a status read; a write only latches. Everything else on the IOASIC (inputs, coin, UART,
sound data port, PIC, interrupt control) the game reaches through the loader's `ioasic:`,
`sound:`, `tty:` and `pic:` drivers.

**CarnEvil gun** (0x16800000): 29 sites, byte reads of all 8 registers (P1/P2 X low, X high,
Y low, Y high); X high read 11 and 10 times. 0x800D4F6C reads all eight and writes offsets 0
(the value 0) and 4; 0x800D4990, 0x800D49C4, 0x800D49F8, 0x800D4A34, 0x800D5478 and one more
read the X-high bytes.

**ASIC reset** (0x17F00000): byte reads 3, byte writes 3, in 0x800D4908, 0x800D4AC8,
0x800D4B98, next to the gun code. MAME's handler resets the IOASIC when bit 1 is clear.
Inferred: this is the gun flash control seattle.cpp lists as not emulated ("lets you set the
flash brightness"), not an IOASIC reset; to be checked before the runtime gives it MAME's
meaning.

**Boot ROM**: the reset vector 0xBFC00000 as a jump target (4 reboot paths, `boot-handoff.md`).

## The loader, on the game's behalf

Measured, 333 functions. Labels as in "Method"; `game` means a direct-call path from one of
the 29 game slots.

**IDE** (task file 0x0A0001F0 to 0x0A0001F7): 116 sites. Data port: read 48, write 16;
error/features, sector count, sector number, cylinder low/high, drive/head, status/command:
read and write; commands seen written: 0x20 (read sectors), 0xC4 and 0xC6 (read multiple, set
multiple), 0xEC (identify), 0xEF (set features); drive/head 0xA0. Functions 0x80010338 to
0x8001203C and 0x800124F4. Labels: game, slot, boot, vector, driver:disk, driver:file.
Alternate status/device control 0x0A0003F6: read 22, write 2 (0 and 4: soft reset), 9
functions. Bus-master DMA 0x0A000F00: read 3, write 7, 0x0A000F04 write 1 (0x80012158,
0x800124F4, 0x800128F8).

**GT64010** (0x0C000000): 126 sites, 16 registers.
- DMA0 to DMA3 control 0x840 to 0x84C: read 3, write 3 each (0x8000D970, 0x8000E930 load and
  run, 0x8000EC04): stop the DMA channels before a new program runs.
- TIMER0 to TIMER3 count 0x850 to 0x85C (written 0x4C4B40, 0x17D7840, 0xFFFFFF) and
  TIMER_CONTROL 0x864 (read 22, write 19): the `timer:` and `wdog:` drivers (0x8000A228,
  0x8000A52C, 0x8000A584) and the sound driver (0x8000FC60, 0x8000FCBC).
- INTR_CAUSE 0xC18 (read 11, write 10) and CPU_MASK 0xC1C (read 6, write 6; 0x10001E seen):
  the default interrupt handler 0x80005368, slot 0x1E0 (0x800060BC), timers. The one access
  at 0xC17 is the indexed approximation of the cause register, not PCI_CS_3_BOOT.
- PCI_COMMAND 0xC00, PCI_TIMEOUT 0xC04 (0x70F): `sysctrl:` init 0x8000F998.
- CONFIG_ADDRESS 0xCF8 (write 3, 0x80000008 seen) and CONFIG_DATA 0xCFC (read 2, write 1):
  PCI configuration, 0x8000F718, 0x8000F774, 0x8000F84C; what slots 0x0F0, 0x0F8, 0x108 and
  0x110 do for Glide.

**IOASIC** (0x16000000), 56 sites:
- COIN (bus 0): read 2, 0x80006990, 0x800069CC; PORT0 (bus 3, DIP switches): read 1, boot
  only (the Test Switch check).
- PICOUT (bus 5): write 4; PICIN (bus 6): read 2; only the `pic:` driver (0x8000A718 to
  0x8000A84C) and its slot. See the PIC answer.
- INTSTAT (bus 7): read 5 (UART paths); INTCTL (bus 8): read 9, write 9 (0x80005244,
  0x80005368 the interrupt paths, 0x800069DC, 0x8000F044, 0x8000FD70).
- UARTCONTROL (written 0x35), UARTOUT, UARTIN: the `tty:` driver (0x8000F2AC to 0x8000F5A4).
- SOUNDCTL (0 and 1: DCS reset), SOUNDOUT (write 3), SOUNDSTAT (read 8), SOUNDIN (read 2,
  write 2: acknowledge): the `sound:` driver (0x8000FD70 to 0x8000FF3C) and 0x8000D370.

**CMOS** (0x16100000): read and write, one byte per 32-bit word, from the `cmos:` driver's read
(0x8000A0E0) and write (0x8000A13C) routines, at the position word 0x80014D78 (initially
0xB6100000; ioctl 0x6300 moves it). Each byte write is preceded by a write of 1 to the CMOS
write enable 0x17000000.

**CS3 control registers**:
- watchdog 0x17100000: write 15 sites, 11 functions (interrupt paths 0x80005244, 0x80005368,
  the TLB/cache-error stops 0x80005F0C, 0x80005F9C, 0x8000602C, `wdog:` write 0x80009FF8,
  timer). Labels include game and vector.
- interrupt enable 0x17300000: read 6, write 6; slot 0x200 (0x80006220) sets bit 7 (VBLANK
  enable) when line 0 is installed; 0x8000C2A0, 0x8000EED0, 0x8000F044.
- interrupt configuration 0x17400000: read 3, write 3 (0x8000C2A0, 0x8000EED0, driver init).
- interrupt state 0x17500000: read in the general exception entry 0x80004368 (bits 0x40 and
  0x08) when IP7 is pending.
- VBLANK clear 0x17700000: write 8 (default handler 0x80005368, slot 0x200, DMA stop paths).
- status LEDs 0x17900000: read 7, write 19 (0, 2, 3, 7), 12 functions; ASIC reset 0x17F00000:
  read 4, write 4 (`ioasic:` init 0x800069DC, `sysctrl:` init 0x8000F998); no-op register
  0x17800000: write 0.
- update ROM 0x1FD00000 and 0x1FD00008: read, `mdiag:` driver.

**PCI memory above the Voodoo** (0x09000000 to 0x0900004F): 24 sites, all but one through the
pointer at 0x80015344 (initially 0xA9000000): the `scsi:` driver and the boot step that opens
`scsi:` (not taken on this board), plus one read of 0x09000014 in the general exception entry
on an IP7 interrupt. MAME maps no device there. Labels: boot, driver:scsi, vector; never game
slots.

**Not reached from the game slots by direct calls**: PICOUT/PICIN (driver:pic, slot), UARTIN,
SOUNDCTL, SOUNDIN, TIMER1/2, PCI_COMMAND/TIMEOUT, interrupt configuration, update ROM, SCSI.
Through the driver table the game reaches the `cmos`, `ioasic`, `timer`, `pic`, `sysctrl`,
`wdog` drivers and the console (see the ioctl list).

### What the game asks the loader's drivers for (measured)

Constant arguments at the ioctl slot (0x0C8) calls, 160 sites. The standard descriptors are
opened by the loader in this order (0x800065E0, read): `con:` three times (fd 0, 1, 2, inferred
from the order), `cmos:` (fd 3), `ioasic:` (fd 4), `timer:` (fd 5).

- fd 3 (`cmos:`, driver ioctl 0x8000A1A4): 0x6300 set position, 23 sites; 0x6301 get base
  (0x8022B174 receives 0xB6100000), 1; 0x6302 get size (0x802372E8 receives 0x8000), 1.
- fd 4 (`ioasic:`): 0x6900 5, 0x6901 4, 0x6902 4, 0x6903 17, 0x6905 4, 0x6906 6, 0x6907 6,
  0x6908 3, 0x6909 16, 0x690A 7, 0x690E 2, 0x690F 6, 0x6910 6, 0x6911 6.
- fd 5 (`timer:` by the open order; the `tty:` ioctl 0x8000F1B0 also tests 0x74xx numbers,
  so the driver is to be confirmed): 0x7401 6, 0x7402 1, 0x7403 2, 0x7405 6, 0x7406 1, 0x7407 2, 0x7409 11,
  0x740A 1, 0x740B 1.
- `pic:` (opened by 0x800C6480): 0x7000, 0x7001, 0x7002, one each.
- a descriptor held in memory: 0x7300 1, 0x7301 8 (0x80167FF8, 0x80168224, 0x801683EC); the
  0x73xx commands are handled by the `sysctrl:` driver's ioctl 0x8000FB44 (read).
- 3 sites with no constant descriptor.

## The four questions

**1. Does GAME.EXE talk to the security PIC through the IOASIC, and with which commands?**
No. GAME.EXE has no access to PICOUT or PICIN (measured; the test asserts it). It opens `pic:`
and calls ioctl 0x7000, 0x7001 and 0x7002 from 0x800C6480. The loader's `pic:` ioctl
(0x8000B0D8, read; jump table at 0x80013818) answers those three from words it stored at
start-up (0x800B8650, 0x800B8398, 0x800B867C), with no PIC traffic. The PIC itself is driven
only by the `pic:` driver's init 0x8000B0B0, which runs at the loader's device start-up
before GAME.EXE: it sends PIC command 1 (read the 16-byte serial block, 0x8000A92C) and
decodes it (0x8000AE2C). The driver also has clock commands 2 then 3 (read, 0x8000A994) and 4
(write, 0x8000AA20), behind ioctls 0x7003 to 0x7006, which the game never issues. Nibbles
are sent as `0x90 | nibble` on PICOUT, the echo `0x80 | nibble` is awaited on PICIN bit 8,
then 0 is written, as in MAME's PIC2 model. PIC NVRAM commands 5 and 6 are never sent; the
MAME run 3 PIC NVRAM (`ioasic`, 256 bytes) stayed all 0xFF. Sure: high (static, consistent
with the MAME NVRAM).

**2. Does it read CMOS/NVRAM, and which offsets?** Yes, read and write, but only through the
loader's `cmos:` driver (fd 3): GAME.EXE has no CMOS or write-enable access of its own
(measured). It asks for the base (0x6301: 0xB6100000) and the size (0x6302: 0x8000 bytes),
computes the addresses of its areas from bases kept at 0x801DE3B0, 0x801E3BF8, 0x8022B080
and 0x80230F74 (the debug strings name audits, adjustments and GCR tables; which base is
which was not read), sets the
position with ioctl 0x6300 (23 sites, functions 0x80145C0C to 0x80146A30) and reads or writes
through the file slots. No offset is a constant in the code. In MAME run 3 (149 s from a blank
NVRAM, attract mode) CMOS bytes 0x000 to 0x76B were written, in five runs: 0x000-0x3BF,
0x3C8-0x59F, 0x5A8-0x60B, 0x65C-0x75B, 0x768-0x76B (1,792 bytes; measured from the NVRAM
file). Which of those are read back was not measured. Sure: high for the mechanism; medium
for the offset list (one run, no game played).

**3. Linear frame buffer or command registers only?** Both, plus texture memory. The LFB is
used: lfbMode is written by 8 functions, the LFB is read and written through its base
(0x80167390, 0x8016754C) and a memory test writes and reads fixed pixels (0x8015BF58). Texture
memory is written by 4 texture download functions. Command registers seen: fastfillCMD and
swapbufferCMD; the triangle commands were not seen (below). Sure: high that the LFB is used
(constant addresses, MAME's map); medium for how much.

**4. Interrupt sources the game registers handlers for.** Static reading of the calls from
`main` and 0x800C4D58 gives exactly the registrations run 3 logged (`boot-handoff.md`):
- CPU line 0 (slot 0x200), 0x800C6030. The loader's line map (0x800146C0: IP2→1, IP3→3,
  IP4→2, IP5→4, IP6→0, IP0/IP1/IP7 none) puts line 0 on IP6, MIPS IRQ4. Installing line 0
  also writes VBLANK clear and sets interrupt enable bit 7 (0x80006220), so line 0 is the
  VBLANK interrupt. Run 3 hit it thousands of times. Sure: high.
- CPU line 3, 0x8013F678: IP3, IRQ1, the IOASIC summary (seattle.cpp). Hit in run 3.
- GT64010 cause bits (slot 0x1E0, served by the loader's IP2 default handler): bit 4 (DMA0
  complete) 0x800C611C, then 0x8014FDF0; bits 1, 2, 3 (MemOut, DMAOut, CPUOut errors)
  0x800C4CEC, 0x800C4D10, 0x800C4D34.
- FPU causes 5 (unimplemented operation) 0x80150860 and 3 (divide by zero) 0x8014FEF0; break
  hooks 0x800C4DA0 (divide by zero) and 0x800C4E68 (overflow); exception codes 1 to 8 and 10
  to 15, 0x800C47E0, and code 13 later 0x80151A70 (break 0x409).
- Not registered: IP4 (IDE, IRQ2: the loader's disk driver polls), the GT64010 timers (the
  loader's `timer:` driver owns them), IP5, IP7.
- Correction to `boot-handoff.md`: the loader calls a CPU-line handler with a0 = the Cause IP
  bit (0x100 << n), not (Cause, frame); a1 is not set for it (0x80005100, read). GT64010 and
  exception handlers are as described there. (Not edited in that page by this task.)

## Constants that are not addresses

Every KSEG0/KSEG1 constant at or above physical 0x08000000 in reached code either classifies
into a named region or is listed here; none of these is used as a load/store base or jump
target (measured; `hwcensus.NOT_ADDRESSES`, checked by the tests). The loader has none.

GAME.EXE, unclassified, by what consumes them:
- float constants loaded into a register (and passed on or stored): 0xBC23D70A (-0.01),
  0xBD4CCCCD (-0.05), 0xBE19999A (-0.15), 0xBE4CCCCD (-0.2), 0xBD276B66, 0xBEB0C6F7,
  0xBF10F977, 0xBA93A92D (about -0.0409, -0.345, -0.566, -0.00113);
- 32-bit values stored to memory and never dereferenced (tags, hashes or float data):
  0x8D0D2CAD, 0x8FE3297E, 0x9AA47BF0, 0x9C0A4D5B, 0x9D535756, 0x9F4BA339, 0x9FAF1308,
  0xAC71C590, 0xAD161652, 0xAF2798CC, 0xAFB0E9BD, 0xBADBEEF1 (a debug marker);
- an integer multiplier: 0xAD517F33 (consumed by `mult`);
- masks consumed by `and`: 0xBFFFF001, 0xBFFFFFEF;
- the high halves of all of the above (0x8D0D0000 ... 0xBFFF0000), consumed only by the
  `ori` that completes them.

Classified into a region, but not addresses either (never dereferenced): 0x88888889 and
0x92492493 (division by 15 and by 7 multipliers, consumed by `mult`), stored 32-bit values
0x8BA70A85, 0x92F56EB0, 0x92FD703C, 0x9500A178, 0xA9F51B4A, 0xB2F709C7, 0xB3FF103A,
0xB727C5AC, and their high halves. 0xA8000214 (fbiInit1) is formed in 0x80157018 but not
dereferenced there.

## Decisions for the runtime (recommendations)

1. **Implement the loader's drivers, not its registers, for the game's view of the CMOS, PIC,
   timers, IOASIC and disk.** The game never touches them; it calls open/read/write/ioctl on
   `cmos:`, `ioasic:`, `timer:`, `pic:`, `sysctrl:`, `wdog:`, the console and files, with
   the ioctl commands listed above. The stand-ins need: `cmos:` as a 32 KB byte store with
   ioctls 0x6300 to 0x6302 (positions are addresses 0xB6100000 + 4 × offset); `pic:` 0x7000
   to 0x7002 returning the three serial-derived words; the 0x69xx, 0x74xx and 0x73xx sets,
   whose meanings still need reading from the drivers named above. Sure: high for the list,
   medium until the 0x69xx/0x73xx/0x74xx semantics are read.
2. **Emulate directly what GAME.EXE touches itself:** the Voodoo (registers, LFB, texture
   memory at 0x08000000), the gun registers, the sound FIFO at 0x13000000, the IOASIC
   SOUNDSTAT write, the ASIC reset/flash register, and the reset vector. Sure: high for these
   being touched; the Voodoo register set is incomplete (below).
3. **Interrupts to deliver:** VBLANK as CPU line 0, the IOASIC summary as line 3, GT64010
   DMA0 completion (and the three error causes) through the GT64010 handler table, the FPU
   and exception handlers through the loader's frame layout. The loader's default IP2 path
   (cause register, watchdog, LEDs) is the runtime's own business. Sure: high.
4. **Watchdog and LEDs** are written only by the loader, from paths the game reaches
   (interrupt paths, `wdog:`); the runtime can treat them as no-ops. Sure: high.
5. **IDE, GT64010 DMA/PCI, SCSI and update ROM** matter only if loader code is run under an
   interpreter; with native stand-ins for the slots they need not be emulated. Sure: high.

## Not confirmed

- **The Voodoo triangle path.** No write to the triangle set-up registers (0x008 to 0x100,
  including triangleCMD/ftriangleCMD) was found, statically or by a search of all stores
  with those offsets on non-stack bases. Glide must reach them through bases the census does
  not follow (a pointer carried in a register from a table entry, or the alternate register
  map at base + 0x200000: fbiInit3 is read in 0x8015C45C and the GC word at 0x801E75B4 is
  set to 0x00200000 in 0x80162B50; inferred). A dynamic check is needed before the
  runtime's Voodoo register list is final: a MAME write watchpoint over 0x08000000 to
  0x083FFFFF for a few seconds of attract mode, logging PC and offset. Not done here: this
  session had no shell in the Linux container.
- The IOASIC unlock (MAME: write 0xE2 to bus word 0) is not in the loader or GAME.EXE. MAME
  run 2 read the DIP switches at bus word 3, which is the unlocked order, so it happened
  earlier, presumably in the POST program (not analysed).
- What SOUNDSTAT writes from GAME.EXE do on the board, and what 0x17F00000 controls.
- The meanings of the `ioasic:`, `timer:` and `sysctrl:` ioctl numbers.
- The fd numbers of the standard descriptors (from the open order, not traced).
- Which CMOS bytes are read, and whether a played game writes beyond 0x76B.
