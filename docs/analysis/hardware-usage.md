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

"Measured" means counted, either statically by the census over reached code or dynamically
in the MAME runs below; each section says which. "Read" means read from the code by hand
(with the `disasm` command), and says where. "Inferred" is labelled as such.

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
- The MAME run 3 NVRAM (`mame/nvram-carnevil/` on the share), and three new MAME
  runs with memory taps for this task: the Voodoo run ("Dynamic check: Voodoo accesses"), and
  the hardware run and the split run ("Dynamic check: the other hardware").

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
  tracked as "the word at A" (or "at A, k"). Accesses through the loaded word are counted
  against a hardware base ("through a base held in memory") only when every store the census
  sees into that location, and the image's initial word there, is that base, or null. A
  store into the location of a value the census cannot know disqualifies it: an unknown
  register, a partial-word store, an FPU store, or a copy from a location that is not itself
  a holder. Stores through bases the census does not know are invisible to it, so the rule
  is sound only for what it sees;
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
72 slot targets, the four exception vectors and the 36 routines of its device-driver table
at 0x80013530 (13 drivers, each 7 routine slots followed by a name: open, close, read,
write, init, unused, ioctl; unused slots point at a shared return-0 routine). The names are
wdog, led, ioasic, sysctrl, pic, timer, tty, sound, cmos, mdiag, disk, file and scsi. Each
register's functions are labelled with what reaches them by direct calls and tail jumps:
`game` (the 29 slots GAME.EXE calls), `vector` (the exception vectors, which run while the
game runs), `slot` (the other 39 slots), `boot` (the loader's own start-up, which has run
before GAME.EXE), and `driver:<name>`. The file slots call drivers through the table, so
`driver:` labels stand for "reached when the game uses that device". Limits: labels are the
union over a register's functions; jumps through the handler tables are not followed.
`hwcensus --loader <boot.u32> --reach` prints, per slot, how many loader functions it
reaches, how many of those make an indirect call, and the hardware regions among them.

## GAME.EXE

Measured statically over 2,093 functions with `GAME_RETURNS` applied, with
`hwcensus.hardware_accesses`; then checked dynamically by the three MAME runs (sections
"Dynamic check" below). Which regions GAME.EXE's own code touches is settled by the hardware
run (attract mode only), not by the census: the census cannot see the CMOS reads below.

**Voodoo registers** (0x08000000), static: 14 sites, 6 registers, all through constant bases.
The holder rule now disqualifies the Glide context words that hold the Voodoo base, so no
access through them is counted (see "Holders under the sound rule"):
- vdstatus 0x000: read 2 (0x8015EEAC)
- swapbufferCMD 0x128: write 1 (0x8015EEAC)
- clutData 0x228: write 2 (0x8014FB78, 0x8014FD7C)
- textureMode 0x300, tLOD 0x304, texBaseAddr 0x30C: write 3 each (0x80168558)

Where the base lives: the board-mapping code stores it at 0x801E5DE0 (0x80157018, after slot
0x110), and 0x80161258 stores it at 0x801E756C, with the LFB and texture bases at 0x801E7574
(base + 0x400000) and 0x801E7570 (base + 0x800000). The MAME run gives the full register
list: 125 written, 16 read ("Dynamic check: Voodoo accesses"). That list is the one to use.

**Voodoo LFB** (0x08400000), static: 39 sites, 16-bit, in 0x8015BF58 and 0x8015BF08. These are
fixed-pixel writes and reads with test patterns (0xDEAD, 0x5A5A, 0x55AA, 0xAA55, 0xFFFF, 0)
at 14 constant addresses across the buffer: a frame buffer memory test (inferred from the
patterns).

**Voodoo texture memory** (0x08800000), static: 2 indexed writes in 0x80168558 and 0x80168C48
(texture download). The run measured 436,973.

**IOASIC sound FIFO** (0x13000000): 3 writes in 0x8013FF78, through the base the image's data
word 0x801A25CC holds (0xB3000000): the game feeds the DCS2 FIFO itself.

**IOASIC** (0x16000000), static: one register only, SOUNDSTAT (bus word 14, 0x16000038): 13
writes in 0x801470A4, 0x80147130, 0x801477B4, with small constants 0 to 5 and more. MAME
treats SOUNDSTAT as a status read, and a write only latches. None of these writes ran in
the MAME run. Everything else on the IOASIC (inputs, coin, UART, sound data port, PIC,
interrupt control) the run saw only from loader code. Statically the game uses the `ioasic:`
descriptor (fd 4) and `pic:`; `sound:` and `tty:` were not seen in the game's descriptor or
ioctl use, and the console descriptors' driver is not identified.

**CMOS** (0x16100000): no static access. The MAME run measured 252,532 direct byte reads by
GAME.EXE, all at one site in 0x801458C0, a CRC loop over CMOS bytes with its table at
0x801A2A38. The address it reads comes in as an argument, built from the base the loader
returns for ioctl 0x6301; the census cannot see it. The game writes no CMOS byte and does
not write the CMOS write enable; writes go through the loader (dynamic, attract mode).

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

**GT64010** (0x0C000000): 126 sites, 15 registers (the CLI shows a sixteenth address,
0xC17, as `reg_0xC17`: an artefact; see below).
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
(0x8000A0E0) and write (0x8000A13C) routines, at the position word 0x80014D78. That word is
initially 0xB6100000, and ioctl 0x6300 stores the caller's address there. That store has a
value the census cannot know, so under the sound holder rule the census no longer resolves
these data accesses; this paragraph is from reading the code and from the MAME run, which
measured 9,980 reads and 4,256 writes by loader code. Each byte write is preceded by a write
of 1 to the CMOS write enable 0x17000000 (read; the run counted 4,256 enable writes, one per
byte written).

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
  0x17800000: one write, of the value 0 (`sysctrl:` init 0x8000F998).
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

Constant arguments at the ioctl slot (0x0C8) calls, 160 sites. Produced by
`python3 -m carnevil_tools hwcensus GAME.EXE --ioctl` (`hwcensus.ioctl_calls`, with
`call_arguments` and the census's interprocedural summaries). The standard descriptors are
opened by the loader in this order (0x800065E0, read): `con:` three times (fd 0, 1, 2, inferred
from the order), `cmos:` (fd 3), `ioasic:` (fd 4), `timer:` (fd 5).

- fd 3 (`cmos:`, driver ioctl 0x8000A1A4): 0x6300 set position, 23 sites; 0x6301 get base
  (0x8022B174 receives 0xB6100000), 1; 0x6302 get size (0x802372E8 receives 0x8000), 1.
- fd 4 (`ioasic:`): 0x6900 5, 0x6901 4, 0x6902 4, 0x6903 17, 0x6905 4, 0x6906 6, 0x6907 6,
  0x6908 3, 0x6909 16, 0x690A 7, 0x690E 2, 0x690F 6, 0x6910 6, 0x6911 6.
- fd 5 (`timer:` by the open order; the `tty:` ioctl 0x8000F1B0 also tests 0x74xx numbers,
  so the driver is to be confirmed): 0x7401 6, 0x7402 1, 0x7403 2, 0x7405 6, 0x7406 1, 0x7407 2, 0x7409 11,
  0x740A 1, 0x740B 1.
12 sites have no constant descriptor. Every one of them has a constant command:
- 0x800C64D4, 0x800C64E4, 0x800C64F4 in 0x800C6480: commands 0x7000, 0x7001, 0x7002. The
  descriptor is the result of `open("pic:", 0)` in the same function, through the open
  wrapper 0x80145698 (read).
- 0x80168014 in 0x80167FF8: 0x7300. 0x80168300, 0x80168328, 0x80168348, 0x80168368 in
  0x80168224 and 0x80168480, 0x801684A8, 0x801684C8, 0x801684E8 in 0x801683EC: 0x7301. The
  descriptor is the word at 0x801DE89C, which `main` sets from `open("pci:", 6)` (0x800C4540,
  stored at 0x800C4550). The 0x73xx numbers match the constants in the ioctl routine
  0x8000FB44 of the table entry named `sysctrl`. That `pci:` opens that driver is inferred.

So the command numbers at all 160 sites are known. The pic commands issued are 0x7000 to
0x7002 only.

## The four questions

**1. Does GAME.EXE talk to the security PIC through the IOASIC, and with which commands?**
No. GAME.EXE has no access to PICOUT or PICIN: none statically (the test asserts it), and
none in the MAME run (attract mode). In that run every PIC access came from loader code
before the hand-off, and none after it ("Split run"). It opens `pic:`
and calls ioctl 0x7000, 0x7001 and 0x7002 from 0x800C6480. The loader's `pic:` ioctl
(0x8000B0D8, read; jump table at 0x80013818) answers those three from words it stored at
start-up (0x800B8650, 0x800B8398, 0x800B867C), with no PIC traffic. The PIC itself is driven
only by the `pic:` driver's init 0x8000B0B0, which runs at the loader's device start-up
before GAME.EXE: it sends PIC command 1 (read the 16-byte serial block, 0x8000A92C) and
decodes it (0x8000AE2C). The driver also has clock commands 2 then 3 (read, 0x8000A994) and 4
(write, 0x8000AA20), behind ioctls 0x7003 to 0x7006, which the game never issues. Nibbles
are sent as `0x90 | nibble` on PICOUT, the echo `0x80 | nibble` is awaited on PICIN bit 8,
then 0 is written, as in MAME's PIC2 model. PIC NVRAM commands 5 and 6 are never sent; the
MAME run 3 PIC NVRAM (`ioasic`, 256 bytes) stayed all 0xFF. Sure: high for attract mode
(static, and dynamic with PC attribution). Medium for gameplay, which was not run, but the
game issues no pic command besides 0x7000 to 0x7002 at any of its 160 ioctl sites.

**2. Does it read CMOS/NVRAM, and which offsets?** Yes, both.
- It writes only through the loader's `cmos:` driver (fd 3). The MAME run saw no CMOS or
  write-enable write by GAME.EXE code, and 4,256 by loader code after the hand-off.
- It reads both ways. The run measured 252,532 direct byte reads by GAME.EXE code, at the
  CRC loop in 0x801458C0, over 1,160 byte offsets in 0x000 to 0x767 (every byte of each word
  group the CRC covers, with gaps between groups). It also measured 9,980 reads through the
  driver. The static census cannot see the direct reads: their address is an argument built
  from the base ioctl 0x6301 returns. The earlier "no CMOS access of its own" was wrong.

It asks for the base (0x6301: 0xB6100000) and the size (0x6302: 0x8000 bytes),
computes the addresses of its areas from bases kept at 0x801DE3B0, 0x801E3BF8, 0x8022B080
and 0x80230F74 (the debug strings name audits, adjustments and GCR tables; which base is
which was not read), sets the
position with ioctl 0x6300 (23 sites, functions 0x80145C0C to 0x80146A30) and reads or writes
through the file slots. No offset is a constant in the code.

Offsets:
- In MAME run 3 (149 s from a blank NVRAM, attract mode) the CMOS bytes that differ from a
  blank NVRAM are 0x000 to 0x76B, in five ranges: 0x000-0x3BF, 0x3C8-0x59F, 0x5A8-0x60B,
  0x65C-0x75B, 0x768-0x76B (1,792 bytes, from the NVRAM file).
- The hardware run measured the written offsets directly: the loader wrote exactly those
  1,792 byte offsets, in the same five ranges.
- The loader read 1,648 offsets in 13 ranges between 0x000 and 0x76B. The game's CRC loop
  read the 1,160 above.

Sure: high for the mechanism and for attract mode (static and dynamic); medium for the
offset list, since no game was played and gameplay may touch more (audits).

**3. Linear frame buffer or command registers only?** Both, plus texture memory. Statically,
a memory test writes and reads fixed LFB pixels (0x8015BF58, 0x8015BF08), and texture
download writes texture memory (0x80168558, 0x80168C48). Dynamically, lfbMode was written
7,551 times. In MAME the game's own code read the LFB
8,237 times and wrote it 12 times in 85 seconds of attract mode, against 436,973 texture
memory writes and 20,342 register writes (dynamic check below): in attract mode the LFB is a
side path and drawing goes through the triangle registers (triangleCMD, nopCMD, fastfillCMD,
swapbufferCMD). Sure: high that the LFB is used (static and dynamic); medium for how much
during gameplay, which was not run.

**4. Interrupt sources the game registers handlers for.** Static reading of the calls from
`main` and 0x800C4D58 gives exactly the registrations run 3 logged (`boot-handoff.md`):
- CPU line 0 (slot 0x200), 0x800C6030. The loader's line map (0x800146C0: IP2→1, IP3→3,
  IP4→2, IP5→4, IP6→0, IP0/IP1/IP7 none) puts line 0 on IP6, MIPS IRQ4. Installing line 0
  also writes VBLANK clear and sets interrupt enable bit 7 (0x80006220), so line 0 is the
  VBLANK interrupt. Run 3 hit it thousands of times. Sure: high.
- CPU line 3, 0x8013F678: IP3, IRQ1, the IOASIC summary (seattle.cpp). Hit in run 3.
  Sure: high for the registration and the IP3 mapping (static, run 3); medium that the
  IOASIC is its only source, since MAME wires only the IOASIC to IRQ1 and the board was not
  checked.
- GT64010 cause bits (slot 0x1E0, served by the loader's IP2 default handler): bit 4 (DMA0
  complete) 0x800C611C, then 0x8014FDF0 (0x8014FDF0 hit in run 3); bits 1, 2, 3 (MemOut,
  DMAOut, CPUOut errors) 0x800C4CEC, 0x800C4D10, 0x800C4D34. Sure: high for the
  registrations (static, all logged in run 3); medium for the bit names, which come from
  MAME's GT64010 constants, not from the game.
- FPU causes 5 (unimplemented operation) 0x80150860 and 3 (divide by zero) 0x8014FEF0; break
  hooks 0x800C4DA0 (divide by zero) and 0x800C4E68 (overflow); exception codes 1 to 8 and 10
  to 15, 0x800C47E0, and code 13 later 0x80151A70 (break 0x409). Sure: high (static, logged
  in run 3).
- Not registered: IP4 (IDE, IRQ2: the loader's disk driver polls), the GT64010 timers (the
  loader's `timer:` driver owns them), IP5, IP7. Sure: medium. No registration call for them
  exists in the code, and run 3 logged none. But "the disk driver polls" and "the timer
  driver owns the timers" are read from the loader, not traced: the timer-driver writes to
  TIMER_CONTROL and TIMER1 in the second MAME run were made by loader code.
- Correction to `boot-handoff.md`: the loader calls a CPU-line handler with a0 = the Cause IP
  bit (0x100 << n), not (Cause, frame); a1 is not set for it (0x80005100, read). GT64010 and
  exception handlers are as described there. (Corrected in that page since.)

## Dynamic check: Voodoo accesses (measured in MAME)

**Run.** MAME 0.276 in the Linux container, `carnevil` (boot ROM 1.9, the `carnevil` CHD), blank NVRAM,
default DIP switches, no input. It was started with `-video none -sound none -nothrottle
-seconds_to_run 90` under `timeout 2400`, with the Lua `-autoboot_script`
`tools/mame/voodootap.lua`. The script installs a write tap and a read tap on the main
CPU's program space over physical 0x08000000 to 0x08FFFFFF. For each access it records the
raw offset, read or write, and whether the PC is inside GAME.EXE (0x800C4000 to 0x801A4224),
with up to 12 PCs per offset. It writes `mame/voodoo-tap/voodoo-tap.txt` when the machine
stops. The script is in `tools/mame/` (command lines in its README); its output is on the
share, not in git.
- It ran 90.0 emulated seconds (4,891 frames). The first Voodoo access from GAME.EXE came at
  about 5 emulated seconds, so the game ran for about 85 seconds: its own start-up, then
  attract mode. No game was played.
- The taps were removed and reinstalled every 30 frames (163 times), because the GT64010
  remaps the CPU space when its windows are written. An access made between a remap and the
  next reinstall would be lost. That is at most half a second per remap; how many remaps
  happened after the hand-off was not measured.
- The PCs are what MAME's recompiler reports at the access. They often point at the call
  site of Glide's register-write helper rather than at the store, so they locate the code,
  not the instruction.

**Folding.** A register offset is folded to `offset & 0x3FC`, the register number. Bits 10 to
13 select chips: 0 means all chips, 0x800 TMU0, 0x1000 TMU1, 0x2000 TMU2 (`voodoo.cpp`).
Chip bits were seen only on textureMode, tLOD and tDetail (0 and 0x800) and on
trexInit0/trexInit1 (0x800, 0x1000, 0x2000). Neither the alternate register map (bit 21)
nor the byte-swizzle bit (bit 20) was seen in CPU accesses. GAME.EXE made 132 raw write
offsets, which fold to 125 registers. Correction, 2026-11-20: the taps saw CPU accesses only.
The game's GT64010 DMA does write through the alternate map; see "The Voodoo model",
"Alternate register map".

**Writes by GAME.EXE** (20,342 in all). The tag is "static" where the static census also had
the register and "missed" where it did not. Counts are writes:
- vertexAx/Ay/Bx/By/Cx/Cy 0x008 to 0x01C: 260 each (missed)
- startR/G/B/Z/A 0x020 to 0x030: 2 each; startS/T/W 0x034 to 0x03C: 260 each (missed)
- dRdX/dGdX/dBdX/dZdX/dAdX 0x040 to 0x050: 2 each; dSdX/dTdX/dWdX 0x054 to 0x05C: 260 each
  (missed)
- dRdY ... dAdY 0x060 to 0x070: 2 each; dSdY/dTdY/dWdY 0x074 to 0x07C: 260 each (missed)
- triangleCMD 0x080: 258 (missed)
- fbzColorPath 6, fogMode 3, alphaMode 3, fbzMode 3,653, lfbMode 7,551, clipLeftRight 5,
  clipLowYHighY 5 (static)
- nopCMD 0x120: 269 (missed)
- fastfillCMD 3, swapbufferCMD 108, fogColor 2, zaColor 3,646, chromaKey 2, stipple 2,
  color0 4, color1 260 (static)
- fogTable[0] to [31], 0x160 to 0x1DC: 2 each (missed)
- fbiInit4 6, vRetrace 1, backPorch 2, videoDimensions 2, fbiInit0 17, fbiInit1 25, fbiInit2
  22, fbiInit3 6, hSync 2, vSync 2 (missed). vRetrace is a read-only register in MAME's
  table; the write is recorded as made.
- clutData 33 (static), dacData 31, maxRgbDelta 2 (missed)
- textureMode 114, tLOD 114, tDetail 3, texBaseAddr 116, texBaseAddr_1/_2/_3_8 2 each
  (static)
- trexInit0 5, trexInit1 11 (missed)
- nccTable[0] to [23], 0x324 to 0x380: 2 each. The static census had nccTable[4] only.

Not written by the CPU: the float triangle path 0x088 to 0x100 (fvertex*, fstart*, fd*,
ftriangleCMD) and the counters 0x14C to 0x15C. Correction, 2026-11-20: attract mode does use
the float path, through DMA records that write the alternate register map. dma0.txt holds
137,936 ftriangleCMD writes in 150 s, each with fvertexAx to fvertexCy and fstartS/T/W, fdS/T/WdX
and fdS/T/WdY, first at 11.126 s. That is measured, decoded with the alternate map from MAME's
model. The fixed-point path's 258 triangleCMD writes are the CPU's start-up checks.

**Reads by GAME.EXE** (568,262 in all; 16 registers):
- vdstatus: 568,150 reads, the busy-wait (static)
- fbzColorPath, fogMode, alphaMode, fbzMode, lfbMode, clipLeftRight, clipLowYHighY, stipple,
  color0, color1: 1 read each, near 0x80161AE4 (static)
- fbiInit4 5, fbiInit0 22, fbiInit1 30, fbiInit2 36 (missed); fbiInit3 9 (static)

**Frame buffer and texture memory**, GAME.EXE only: LFB 8,237 reads and 12 writes (PCs near
0x8015B250 to 0x8015DB10 and 0x8015C074 to 0x8015C220); texture memory 436,973 writes, no
reads (0x8015B82C to 0x8015B85C, 0x80163864, 0x80163F74, the computed-jump copy routines
around 0x801644F4, and 0x80169134 to 0x8016932C).

**Code outside GAME.EXE** touched the Voodoo: 1,916 swapbufferCMD, 178 each of textureMode,
tLOD and texBaseAddr, 1 trexInit1 write, 45,330 vdstatus reads and 399,314 texture writes.
This run did not record when these accesses happened or which program made them. Inferred:
they are the boot ROM's POST program before the hand-off, which does the Glide/Voodoo set-up
(`boot-handoff.md`); the loader has no Voodoo access in the static census, and slot 0x110
only maps the board through PCI configuration. Note added 2026-11-20: this inference is
probably wrong. In M2 the port, which runs GAME.EXE alone, makes exactly the texture-memory
and trexInit1 write totals that this tap counts only once these "outside GAME.EXE" writes are
added to the game's own, so they are the game's, made through code this tap did not
attribute to GAME.EXE (inferred; `M2-report.md`, "The Voodoo's write counts").

**Reconciliation.** These are the static census figures before the holder rule was made
sound (fix round 1). The static census as it now stands lists 6 registers, all of them also
seen in the run. The earlier 26-register list, which used the now-disqualified holders, had
no false positives either: every register on it was seen in the run. That list missed 101 of
the 125 written registers (fbiInit3, which it had only as a read, is among them) and 4 of
the 16 read ones. Inferred from the PCs, which are those of MAME's recompiler and only
approximate: the missed accesses go through Glide's one-instruction access helpers. Their
call sites are the PCs recorded for the triangle, init and reset registers. 0x8015DB04
stores a1 at a0, and 0x8015DB0C loads from a0. Their callers pass "base + register offset" in
a0, for example 0x8015B1B0 (triangles), 0x801579F8 (register reset: every register once) and
0x80157454 (board initialisation), with the base in a saved register that came in as an
argument. What stopped the propagation:
- the helper's a0 differs on every call, so the "all callers agree" rule gives it no entry
  value, and the store inside it has no known base;
- at the call sites, the base was itself a caller's argument that callers do not agree on, or
  agree on only through a pointer whose holder is a table entry.

A context-sensitive step would recover most of them: count a call to a function that only
stores or loads through its incoming a0 as an access at the call site, at the caller's a0.
The census does not do that yet, and the static lists above are left as measured. The
recommendation is to take the Voodoo register list from this run.

**Holders under the sound rule** (fix round 1). The holder rule now rejects a location that
receives any store the census cannot value. Holders before and after:
- GAME.EXE before: 0x801A25CC (0xB3000000), 0x801E5DE0, 0x801E756C, 0x801E7578, 0x80212D74
  (0xA8000000), 0x801E7574 (0xA8400000), 0x801E7570 (0xA8800000). GAME.EXE after: 0x801A25CC
  and 0x801E5DE0. 0x801E5DE0 resolves no access.
- What disqualified them: 0x801E756C gets an unknown `sw` at 0x80164AF4 (in 0x80164A10). It
  is a store to a field of a table entry indexed by a register, which the entry-0 rule
  folds onto this address, so not the base arriving another way. 0x801E7574 and 0x801E7570
  get unknown stores at 0x80164C78 and 0x80164C80 (in 0x80164B2C), and copies of 0x801E756C
  + 0x400000 / + 0x800000. 0x801E7578 and 0x80212D74 get only copies from 0x801E756C, which
  is no longer a holder.
- Resolved GAME.EXE accesses: 100 before, 3 after. The 97 lost are 82 Voodoo register, 9
  texture memory and 6 LFB accesses (registers: vdstatus, fbzColorPath, fogMode, alphaMode,
  fbzMode, lfbMode, clipLeftRight, clipLowYHighY, fastfillCMD, fogColor, zaColor, chromaKey,
  stipple, color0, color1, fbiInit3, textureMode, tLOD, tDetail, texBaseAddr, _1, _2, _3_8,
  nccTable[4]). The first MAME run saw all of them from GAME.EXE code, so the losses were
  true accesses. The 3 that survive are the sound FIFO writes in 0x8013FF78; the second MAME
  run saw 512 sound FIFO writes from GAME.EXE code (0x8013FF88, 0x8013FF9C).
- Loader before: 0x80015344 (0xA9000000, SCSI) and 0x80014D78 (0xB6100000, the CMOS
  position). Loader after: 0x80015344. 0x80014D78 gets the caller's address from ioctl 0x6300
  at 0x8000A1F8. Its 2 resolved CMOS accesses are lost; the second MAME run saw loader CMOS
  reads and writes, so they were true. The SCSI accesses were not exercised (no SCSI
  device).

## Dynamic check: the other hardware (measured in MAME, fix round 1)

**Run.** The machine, ROMs, NVRAM (blank) and inputs (none) were the same as the Voodoo run.
The options were `-nodrc -video none -sound none -nothrottle -seconds_to_run 90` under
`timeout 2400`, with `-autoboot_script tools/mame/hwtap.lua`. Read and write taps covered
physical 0x0A000000 to 0x0C000FFF (PCI I/O, so IDE, plus the GT64010) and 0x10000000 to
0x17FFFFFF (all four chip selects: sound FIFO, CS2, IOASIC, CMOS, gun, CS3 control
registers).
- Each access is attributed by the CPU's CURPC, which is precise under the interpreter
  (`-nodrc`). In this build it is the next PC to run: for most accesses the instruction after
  the access, but for an access in a branch delay slot the branch target. So an access in the
  delay slot of a transfer between groups (a `jal` into the stub block or the loader, a
  `jr ra` back to the game) is counted in the group of the target, and per-PC lists name the
  target, not the access. The CMOS reads counted for GAME.EXE are not such a case: their PC,
  0x801458DC, follows the `lbu` at 0x801458D8 in the CRC loop (read). The groups are
  GAME.EXE (0x800C4000 to 0x801A4224, the stub block excluded), the stub block
  (0x800C408C to 0x800C42CC, end exclusive), the loader (0x80000000 to 0x80015C40) and other.
- 90.0 emulated seconds, 4,892 frames, average speed 28%. The first access by GAME.EXE code
  came at 9 emulated seconds. Taps were reinstalled every 30 frames, as before.
- The taps report the 32-bit-aligned address, so byte registers that share a word (the IDE
  task file) are counted per word.
- Output: `mame/hw-tap/hw-tap.txt` on the share.
- A second run, with an extra tap marking the hand-off and splitting the loader's counts
  into before and after it, is reported below as "split run".

**By GAME.EXE's own code** (attract mode):
- carnevil_gun: 76,878 accesses. Reads of all 8 registers: X high 21,355 and 17,084, the
  others 4,271 each. Writes: offset 0 8,542, offset 4 4,271. PCs 0x800D4FE4 to 0x800D5814.
- CMOS: 252,532 byte reads (1,160 distinct byte offsets in 0x000 to 0x767), all at the CRC
  loop in 0x801458C0. No writes.
- sound FIFO 0x13000000: 512 writes (0x8013FF88, 0x8013FF9C).
- 0x17F00000: 1 read, 1 write (0x800D4BA4, 0x800D4BC8).
- Nothing else: no IDE, GT64010, IOASIC, CMOS write enable, watchdog, interrupt control, VBLANK
  clear or LED access by GAME.EXE code, and none by the stub block. The static SOUNDSTAT
  writes (0x801470A4 ...) did not run.

**By loader code** (the loader's own start-up plus everything after the hand-off):
- IDE: 256,552 task-file accesses (data word 239,360 reads, 4,911 writes; the 0x1F4 word
  12,281); alternate status 348,813; bus-master DMA 11,650.
- GT64010: 9,604,980. TIMER_CONTROL 3,705,134 reads and as many writes; TIMER1/2/3 counts;
  INTR_CAUSE 6,116 reads, 2,884 writes; CPU_MASK; DMA0 control 6,076 writes and DMA0 next
  2,025 writes; DMA1 to DMA3 control; PCI configuration 359 address writes, 295 data reads,
  64 data writes; PCI command and timeout.
- IOASIC: 3,052,229. SOUNDSTAT 1,679,829 reads, SOUNDOUT 1,230,929 writes, COIN 32,790 reads,
  PORT2 32,768 writes, INTCTL 19,166 reads and writes, INTSTAT 14,658, PORT0 to PORT3 reads,
  UARTOUT 1,033, SOUNDIN, SOUNDCTL, PICOUT 34 writes, PICIN 102 reads.
- CMOS: 9,980 reads, 4,256 writes; CMOS write enable 4,256 writes.
- watchdog 173, VBLANK clear 4,374, LEDs 5,377 reads and 5,393 writes, interrupt enable 3
  reads and 3 writes, interrupt configuration 1 and 1, 0x17F00000 6 and 6, no-op register 1.
- Other code (the POST program): CS2 0x14000068 1 write, LEDs 2 writes.

**Split run** (`tools/mame/hwtap-split.lua`, output `mame/hw-tap/hw-tap-split.txt`). It is
the same run with one more tap. The first non-zero write to physical 0x000C408C, `_start`
copying the slot table, marks the hand-off, which came at 5.0 emulated seconds. Loader
accesses after it are counted apart. The totals came out identical to the first run, so the
emulation is deterministic here. After the hand-off, loader code made:
- IDE: 16,303 task-file accesses, 4,658 alternate status reads, 11,645 bus-master DMA
  accesses (file reads for the game);
- GT64010: 9,142,595 (timers, cause and mask, DMA0 6,075 control and 2,025 next writes, PCI
  configuration 94 address writes);
- IOASIC: 2,985,826 (sound, inputs PORT0 to PORT3, INTCTL/INTSTAT, UARTOUT 902), but no
  PICOUT, PICIN or COIN access: all 34 PIC writes and 102 PIC reads, and all 32,790 COIN
  reads, came before the hand-off;
- CMOS: all 9,980 reads and 4,256 writes, and all 4,256 enable writes;
- watchdog 170, VBLANK clear 4,371, LEDs 10,748, interrupt enable 1 and 1. Interrupt
  configuration only before the hand-off.

What changed against the static census: GAME.EXE does touch the CMOS itself, for reads.
This is a result: the runtime must give it readable CMOS at 0xB6100000 with one byte per
word (Recommendation 1). Everything else matches the static census's negatives, for attract
mode.

## Measured values the runtime answers with

The values the runtime's bus and loader stand-ins give the game (`runtime/hw`,
`runtime/core/bus.c`, `runtime/loader/slots.c` and `dev_sysctrl.c`), each from a file of
`mame/m2-facts/` on the share (run A of `tools/mame/README.md`, "M2 facts": facts-A2, blank
NVRAM, no input; the Voodoo file covers the first 15 s after the entry, the others 150 s).
The code cites the paragraph of each constant.

**Gun** (measured, `inputs.txt`). With no input every byte read of a gun register returned
one value: 0x00 at 0x16800000 (7,788 reads), 0x48 at 0x16800004 (38,940), 0x00 at
0x16800008 (7,788), 0x02 at 0x1680000C (7,788), 0x00 at 0x16800010 (7,788), 0x48 at
0x16800014 (31,152), 0x00 at 0x16800018 (7,788) and 0x02 at 0x1680001C (7,788). The reads are
`lbu` (0x800D4FE0 and on). The writes of offset 4 are the `sb` at 0x800D50C0, and those of
offset 0 the `sw` of 0 at 0x800D5430 (read; measured: every offset-0 write in the M0 hardware
tap, `mame/hw-tap/hw-tap.txt`, has the next pc 0x800D5434, and the offset-4 writes 0x800D50C4
and 0x800D528C, the `sb` in the delay slot of a branch-likely to 0x800D528C). Correction,
2026-11-20: this paragraph first called both writes `sb`, and the gun model refused the `sw`
when the boot run reached it.

**0x17F00000** (measured, `inputs.txt` and the raw `debug.log` of facts-A2). The game read it
once with `lb` at 0x800D4BA0 and got 0x0E, then wrote it once with `sb` at 0x800D4BC4 with
0x1E, the value read with bit 4 set (`ori` 0x10 at 0x800D4BBC, read); both at frame 620. The
watchpoint's pcs, 0x800D4BA4 and 0x800D4BC8, are the next pcs, which is how the read and the
write are told apart.

**Sound FIFO** (read). The game's 512 writes (measured above) are the `sw` at 0x8013FF98.

**PCI configuration** (measured, `voodoo-reads.txt`, the 44 PCI slot calls). Device 8 read
0x0001121A for configuration word 0 (byte offsets 0 and 2, twice), 0x00000002 for word 2
(twice) and 0x08000008 for word 4, BAR0, once, after slot 0x110 had written 0x08000000 there.
No other word of any device was read through slot 0x0F0. The game wrote, through slot 0x0F8
(19 calls): byte offset 0x04, word 1 (2, twice); 0xE0, 0x44 and 0x48 (0, once each); 0xC0 (0,
twice; corrected on 2026-11-20 from "1, later 0": the 1 is the call's fifth argument, not the
value); and 0x40, initEnable (1 once, then 3 six times and 5 five times, alternating). It
wrote no other word; BAR0 is written only by slot 0x110. The runtime accepts only these
words through slot 0x0F8 and stops on any other, which also stops a BAR-sizing write. The
vendor probes of devices 6, 7 and 9 returned 0. Inferred: word 4 reads back the base with
the memory BAR's type bits (bit 3, prefetchable); this is the one point measured. The game
sets initEnable bit 0 (1 at 5.1928 s) before its first fbiInit write (5.1928 s, 21 µs
later; `voodoo-ramp.txt`), so the runtime stops on an fbiInit write while that bit is clear.

**Voodoo registers the game read** (measured, `voodoo-reads.txt`), and the kind of each in the
runtime's sink:
- status (0x000), 604,082 reads, kind 2 (computed). The 18 distinct values, by field: PCI
  FIFO free (bits 0 to 5), vertical retrace (6), FBI, TMU and SST busy (7 to 9), the
  displayed buffer (10 and 11), memory FIFO free (12 to 27), swaps pending (28 to 30). The
  field meanings are those of the Voodoo Graphics status register; they are inferred, and
  agree with every value seen.
  - 596,461 reads of 0x0FFFF03F: idle, FIFOs free (0x3F, 0xFFFF), buffer 0, no retrace, no
    swap pending.
  - 5,815 of 0x0FFFF07F: the same with retrace.
  - 253 of 0x0FFFF47F: idle with retrace and displayed buffer 1, between 11.18 s and 19.99 s,
    after swaps. They are neither busy nor swap-pending.
  - 771 of 0x0FFFF3BD, 167 of 0x0FFFF3BE and 152 of 0x0FFFF3BC: busy, PCI FIFO 0x3D, 0x3E
    and 0x3C, buffer 0.
  - 152 of 0x0F1217B8 (buffer 1) and 150 of 0x0F1213B8 (buffer 0): busy, PCI FIFO 0x38,
    memory FIFO 0xF121.
  - 76 of 0x1F1217B7 (buffer 1) and 75 of 0x1F1213B7 (buffer 0): the same with PCI FIFO 0x37
    and one swap pending.
  - 2 of 0x0FFFF3F9: busy with retrace, PCI FIFO 0x39, buffer 0.
  - 2 of 0x0F1737AB: busy, PCI FIFO 0x2B, memory FIFO 0xF173, buffer 1.
  - 1 of 0x1FFFF3F8: busy, retrace, one swap pending, PCI FIFO 0x38, buffer 0.
  - 1 of 0x1F2763BE: busy, one swap pending, PCI FIFO 0x3E, memory FIFO 0xF276, buffer 0.
  - 1 of 0x1F1737AA: busy, one swap pending, PCI FIFO 0x2A, memory FIFO 0xF173, buffer 1.
  - 1 of 0x0FF327F3: busy, retrace, PCI FIFO 0x33, memory FIFO 0xFF32, buffer 1.
  - 1 each of 0x0F2563BF and 0x0F2463BF: busy, PCI FIFO 0x3F, memory FIFO 0xF256 and 0xF246,
    buffer 0.
  The model computes every field ("The Voodoo model": busy and the FIFOs, swaps, video timing
  and retrace). In the captures the first alternate-map write is a DMA record at 11.126 s
  after power-on (measured, `dma0.txt`); CPU code makes no alternate-map write in
  `voodoo-ramp.txt`. The retrace length is measured: in the 148 frames from 6.58 s to
  9.30 s, which the game polled densely, the bit stayed set at least 251,727 ns (the longest
  span from a frame's first to its last set read; the shortest such span is 251,354 ns, which
  bounds nothing) and at most 254,120 ns (the shortest span between the clear reads either
  side of a frame's set reads; corrected 2026-11-20, the whole-branch review's open item).
  Those frames were
  18,476,186 to 18,476,194 ns apart. Both agree with MAME's model of the card's timing:
  4 rows of 63,492 ns (253,968 ns) in a frame of 291 rows at 15,750 Hz (18,476,190.5 ns).
  No status read before the game programs the video timing at 6.5675 s has bit 6 set
  (measured: 266,000 reads over 1.37 s from 5.19 s).
- fbzColorPath 0x104, fogMode 0x108, alphaMode 0x10C, fbzMode 0x110, lfbMode 0x114,
  clipLeftRight 0x118, clipLowYHighY 0x11C, stipple 0x140, color0 0x144, color1 0x148: one
  read each at 9.29 s (0x80161AF0 to 0x80161BC4), returning 0, 0, 0, 0x4200, 0, 0x190, 0x100,
  0, 0, 0. Kind 1 (storage; inferred from their being plain mode registers the game wrote
  earlier). Their values before any write are not measured.
- fbiInit4 0x200 (5 reads), fbiInit0 0x210 (22), fbiInit1 0x214 (30), fbiInit3 0x21C (9):
  kind 1. The first reads returned 0x00000001, 0x00000410, 0x00201102 and 0x001E4000; the
  sink starts them there (inferred: no game write came before them; fbiInit1's and
  fbiInit3's came before the game set initEnable). Later reads returned values consistent
  with the game's read-modify-write sequences (for example fbiInit0 0x410, 0x416, 0x412).
- fbiInit2 0x218 (36 reads): kind 1 while initEnable bit 2 is clear (27 reads, the first
  0x80000040, which the sink starts it at), kind 3 while it is set: exactly the 9 reads made
  between an initEnable write of 5 and the next of 3 returned single bytes, 0x79, 0x79, 0x55,
  0x55, 0x71, 0x71 and three of 0x00, and no other register was read in those windows.
  Inferred: bit 2 remaps fbiInit2 onto the DAC's read data, which depends on the game's
  dacData writes (next paragraph). For want of evidence, a read of fbiInit3 while bit 2 is
  set stops.

**The DAC** (the writes read from the code, the values measured, the model inferred). Every
fbiInit2 read under initEnable bit 2 follows dacData (0x22C) writes made through the helper
0x8015DB04, each followed by the idle wait 0x8015D56C, and is made through 0x8015DB0C:
- the ICS detect 0x8015906C (read): 0x070B, then 0x0D00 and a read twice, 0x0701, 0x0D00 and a
  read twice, 0x0707, 0x0D00 and a read twice; it succeeds if the first read of each pair was
  0x79, 0x55 and 0x71 (0x801592AC to 0x801592C8). Measured, `voodoo-reads.txt`: 0x79, 0x79,
  0x55, 0x55, 0x71, 0x71 at 5.19 s after power-on, and the routine was entered once.
- the PLL routines 0x8015A1D0 and 0x80159A28 (read): 0x070E, 0x0D00 and a read, then 0x040A
  (0x0400 in 0x80159A28), 0x05mm and 0x05nn (two values from a table), 0x040E and 0x0500 with
  the byte read and 0xEF. Measured: the read gave 0x00 each of the three times.
- 0x8015A934 (read): one write, 0x0650 or 0x0670, and no read.
The model: a dacData write puts its bits 0 to 7 into DAC register (bits 8 to 10) unless bit 11
is set; with bit 11 it latches that register's value, except that register 5 answers 0x55,
0x71 or 0x79 while register 7 holds 0x01, 0x07 or 0x0B; fbiInit2 read under initEnable bit 2
returns the latch. Of the DAC's power-on values only register 5's is known: it is 0. The
first plain read of register 5, made after register 7 was written (0x0E) and before any
write to register 5, returned 0. Register 7's power-on value is not known, and register 5's
answer depends on it, so the runtime stops on a register-5 read request made before
register 7 has been written; the game always writes register 7 first. The basis for the model is the coordinator's recollection of MAME's
Voodoo DAC, which was not re-read. It reproduces all 9 measured reads in order: the six of
the ICS detect, and three of 0x00 (register 5 as last written, its byte read & 0xEF). The
capture `voodoo-ramp.txt` (below) holds the first 23 dacData writes, which are exactly these
sequences, and replaying them reproduces the reads of its first 2 s.

**Voodoo frame buffer reads** (measured, `voodoo-reads.txt` and `voodoo-ramp.txt`). Of the
8,237 LFB reads, 9 are the start-up memory test's (0x8015C0A8 to 0x8015C224, 6.553 s after
power-on). It writes halfwords under lfbMode 0x8F, 0x10 and 0x40 in turn and reads them back.
In one case it wrote 0xDEAD at 0x08432100, then 0xFFFF at 0x0852BE3C and other addresses, and
read 0xFFFF at 0x08432100. So LFB addresses alias in the card's frame-buffer memory, by its
layout (row pitch, buffer offsets and memory size, set by the fbiInit registers).

The dither ramp makes 8,192 reads, at 0x8015B384 to 0x8015B390, from 6.5538 s to 6.5670 s
(routine 0x8015B250). It draws 256 flat-coloured triangles, one at a time, each with
vertices (0, 0), (36, 0) and (0, 36) in 12.4 fixed point, fbzColorPath 0x2 (color1), fbzMode
0x300 (dither and RGB write) and lfbMode 0. color1 steps from 0x000000 to 0xFFFFFF by
0x010101. The triangle is drawn with triangleCMD, never fastfillCMD (the one fastfillCMD
before it, at 6.5529 s, cleared a 100 by 100 rectangle with color1 0 under fbzMode 0x600).
After each triangle the routine reads the 4 by 4 pixels at the origin through the LFB (eight
words, each read four times). From 0x8015B39C it sums the 16 pixels' red, green and blue into
tables at 0x801AB190 and 0x801AF190 indexed by the sums, and returns 0 when they disagree
(read, in outline). Inferred: a calibration of the card's dither.

The last 36 reads are a TMU check, made right after the ramp with 2 textured triangles.
- At 6.5670 s it writes trexInit1 0x0004F420 (bit 18 set) to each TMU, then draws the 36 by 36
  triangle with fbzColorPath 0x08000001 (texture RGB), textureMode 0x00000D00 and tLOD 0. It
  reads 32 times at 0x8015B664 to 0x8015B670: 0x00020002 at every word but 0x08401800, which
  gives 0x00020003.
- At 6.5671 s it writes three texture words at texBaseAddr 0x40000, 0x20000 and 0, then draws
  a 4 by 4 triangle (textureMode 0x08241A00, RGB 565, fbzMode 0x200, no dither). It reads
  0x08400000 4 times at 0x8015B948 to 0x8015B954 and gets 0x92F56EB0, the first texture word.
- Inferred: the first triangle reads the TMU's revision or configuration word, and the
  second sizes texture memory by aliasing.

Correction, 2026-11-20: this paragraph said 258 flat triangles and 8,228 ramp reads. The
measured split is 256 flat triangles with 8,192 reads, and 2 textured triangles with 36
reads. The three checks are modelled ("The Voodoo model").

**`voodoo-ramp.txt`** (measured, `tools/mame/voodooramp.lua`): every access of physical
0x08000000 to 0x08FFFFFF by GAME.EXE code from its entry to 2 emulated seconds after it, in
order. That is 5,276 writes and 274,967 reads. Its reads are `voodoo-reads.txt`'s for the
same span, the same addresses and values in the same order, with times that differ by at
most 1 ns of rounding; it has one more read, at the span's end. So the two runs agree. A write by the
GT64010's DMA is not a CPU access and is not in it; the first DMA chain starts at 9.30 s,
after the span.

**GT64010 DMA channel 0** (measured, `dma0.txt`; the fit is inferred). Every one of the
1,376,343 records of the 3,580 chains has a word count and word-aligned addresses; 575,067
write Voodoo registers (275,872 of them through address bit 21, the alternate register map),
797,696 the frame buffer (0x640 bytes each) and 3,580 RAM. The time from the enable write to
the cause-bit-4 handler is, to within 5 ns (0.002%) for every start, 2,663.890 ns plus
1,939.399 ns per record that does not write the frame buffer plus 16.946 ns per frame-buffer
byte: a least-squares fit to the eight chain shapes (28, 126, 195, 279, 564, 571 and 576
small records, and 130 small with 256 large). The data cannot tell a cost per large record
from one per large byte, since every large record has 0x640 bytes.

## The Voodoo model

The runtime models the card in `runtime/hw/voodoo_null.c` (registers, status, FIFOs, video
timing), `voodoo_fb.c` (frame buffer, LFB, fastfill, triangles, swap) and `voodoo_tmu.c`
(texture unit).

**Source.** The capture cannot fix the card's rules, so they come from MAME's model of the
Voodoo Graphics. That is MAME 0.276 (tag `mame0276`, BSD-3-Clause), read, not built: the
files `src/devices/video/voodoo.cpp`, `voodoo.h`, `voodoo_regs.h`, `voodoo_render.cpp`,
`voodoo_render.h`, `voodoo_pci.cpp`, `voodoo_pci.h` and `poly.h`, plus `src/emu/screen.cpp`,
`src/mame/midway/seattle.cpp` and `src/lib/util/palette.h`, and for the hold of the
writer `src/devices/machine/gt64xxx.cpp` (the GT64010's DMA and its PCI stall). The
runtime's code is written fresh. The dither matrices and the alternate register order are reproduced as data, as facts
about the card. Each rule below is marked "from MAME's model", "measured", "read" or
"inferred". Where the two disagree, the capture wins; no disagreement was found.

**Checked against the capture** (measured). `voodoo-ramp.txt` is replayed in time order with
`voodoo-reads.txt`'s PCI configuration writes, at its recorded times. Virtual time is the
capture time less the entry's, 5,003,569,667 ns, the CPU's cycle 750,535,450 at 150 MHz.
This is a convention for the whole runtime: virtual time 0 is GAME.EXE's entry, which is
5,003,569,666,667 ps after power-on. That figure is measured: the entry's cycle count in
tools/mame/README.md, "M2 facts", at 150 MHz (hw.h, `VOODOO_ENTRY_PS`).
Every one of its 274,967 reads returns the value MAME returned (`tests/test_voodoo_fb.py`):
- 266,645 status reads, including 771 of 0x0FFFF3BD and 16 of 0x0FFFF3BE, which are busy
  with writes queued;
- 85 register reads;
- the memory test's 9 LFB reads;
- the dither ramp's 8,192;
- the TMU check's 36.

**Frame buffer** (from MAME's model; the memory size from seattle.cpp's `set_fbmem(2)`).
- Memory and layout:
  - 2 MB of 16-bit pixels.
  - fbiInit1 bits 4 to 7 give the row in 64-pixel tiles.
  - fbiInit2 bits 11 to 19 give a colour buffer's size in 4 KB pages.
  - With fbiInit2 bit 4 clear there are two colour buffers at 0 and one size, and an aux
    buffer at twice the size. With it set there are three colour buffers and no aux.
  - An offset past the memory is clamped to its last byte.
  - The layout is recomputed only on an fbiInit1 or fbiInit2 write. Before one, every colour
    buffer is at 0, there is no aux buffer, a row is 0 pixels, and three buffers rotate.
- Memory test: its answer follows from this. With 13 tiles and 247 pages, its 0xDEAD under
  lfbMode 0x8F goes to aux row 100, past the 2 MB, and is lost. The read there gives 0xFFFF
  (measured agreement).
- Power-on content: MAME's memory starts at zero. The model treats it as unknown, because it
  is not measured. An LFB read of a pixel no modelled operation wrote since reset stops. The
  capture never makes such a read.
- Dropped drawing (not from MAME's model: the never-guess rule). Each of the three buffers,
  front, back and aux, carries one flag.
  - Setting the flag:
    - A dropped triangle sets its draw buffer's flag, and the aux buffer's flag too when its
      mode writes depth or alpha (fbzMode bit 10).
    - A dropped LFB write sets the flags of its write-select buffer and of the aux buffer,
      always both.
    - The flag belongs to the buffer's place in memory, so a swap does not move it.
    - A layout change while any flag is set sets them all.
  - Clearing the flag: it clears only when a fastfill overwrites the whole buffer. The
    fastfill's clip rectangle must cover the buffer's visible area as the model lays it out:
    columns 0 to the row width, and every row the buffer's pages hold, after the y flip.
  - An LFB read through a flagged buffer, or of a pixel in a flagged buffer's memory, stops
    with the message that the buffer holds drawing the model does not render.
  - No measured read meets a flag. Every triangle and LFB write of the capture is one the
    model draws.

**LFB** (from MAME's model).
- Addressing:
  - A word's offset, doubled, is the pixel index: x in its low 10 bits, y above, y & 0x3FF.
  - lfbMode bit 13 flips y about fbiInit3 bits 22 to 31.
  - For reads, bits 6 and 7 select front, back or aux, and 3 is none, reading 0xFFFFFFFF.
  - For writes, bits 4 and 5 select front or back; 2 and 3 write nothing.
- Reads:
  - A read returns the pair (x, x + 1), or 0xFFFFFFFF if the pair's second pixel is at or past
    the end of the memory.
  - Bits 15 and 16 swap words and bytes.
  - A halfword is the half that address bit 1 selects (little-endian).
- Writes:
  - The game writes raw format 0 (RGB 565, expanded to 8 bits by bit replication and
    dithered as fbzMode says) and raw format 15 (16-bit depth into the aux buffer, nothing
    under fbzMode bit 18).
  - A pixel past the end of the memory is not written.
  - Any other mode (the pixel pipeline, the 32-bit formats, the lanes and swaps) is counted
    and dropped, and flags its buffers (above).

**Dither** (from MAME's model, "hardware-verified" there).
- Red and blue go from 8 to 5 bits as (2v - v/16 + v/128 + d) / 16, green to 6 bits as
  (4v - v/16 + v/64 + d) / 16, in integers.
- d comes from the 4 by 4 matrix 0, 8, 2, 10 / 12, 4, 14, 6 / 3, 11, 1, 9 / 15, 7, 13, 5, or
  MAME's 2 by 2 one under fbzMode bit 11, indexed by (y & 3, x & 3).
- Without fbzMode bit 8 the top bits are kept: 0x808080 is 0x8410.
- Triangles and LFB writes use the flipped row, fastfill the unflipped one.

**Triangles** (from MAME's model: voodoo.cpp `triangle`, poly.h `render_triangle`).
- The fill:
  - Vertices are the s16 registers / 16, in single precision, sorted by y.
  - Scanlines run from round(y1) to round(y3), exclusive, sampled at y + 0.5. round rounds
    down, and up only above one half.
  - Each scanline is the long edge v1 to v3 against v1 to v2 or v2 to v3. The extent is
    [round(start), round(stop)), swapped if reversed.
  - Then fbzMode bit 0 clips to [clipLeft, clipRight) and [clipLowY, clipHighY) on the
    flipped row.
  - The ramp's triangle covers 630 pixels, 35 - y in row y.
- The card is busy for 100 clocks plus one per pixel of the unclipped extents, at 50 MHz.
- triangleCMD and ftriangleCMD are the same command. The float registers write their
  fixed-point twins: vertices in .4, colours in .12 (MAME's float_to_int32), and the TMU's S,
  T and W in .32.
- The capability rule decides what is drawn. At each triangle the model reads the mode
  registers and draws only if every feature they enable is one it implements:
  - the colour path selecting color1 or the texel, with no zero, subtraction, factor,
    reverse, add or invert;
  - clipping, the y origin, dither and the RGB write.
- The rule is not from MAME's model. Otherwise the first feature missing, in pipeline order,
  names the reason: subpixel adjust, iterated (Gouraud) colour, the colour combine, chroma
  key, stipple, the depth test, the alpha mask, alpha test, fog, alpha blending, a depth or
  alpha buffer write, the TMUs disabled, TMU0 off, or a texture-path reason below.
- A triangle that fails is counted under that reason, dropped, and its buffers are flagged
  (Frame buffer, above). A triangle for draw buffer 2 or 3, which MAME writes nowhere, is
  counted under a reason of its own and flags nothing.
- A drawn pixel outside the memory, which MAME leaves undefined, stops.

**Fastfill** (from MAME's model).
- It fills the rectangle [clipLeft, clipRight) by [clipLowY, clipHighY) of the draw buffer
  (fbzMode bits 14 and 15). RGB is filled with color1's dither pattern if bit 9 is set, aux
  with zaColor's low half if bit 10 is set and there is an aux buffer.
- It does nothing, in no time, without either bit. The card is busy for pixels / 2 clocks.
- The capture's one fastfill, 100 by 100, keeps the card busy for 100 µs, with the
  following nopCMD queued. That gives the 16 reads of 0x0FFFF3BE (measured agreement).

**Texture unit** (from MAME's model, implemented as far as the TMU check needs).
- Texture memory:
  - One TMU with 4 MB (seattle.cpp `set_tmumem(4, 0)`).
  - Registers reach it with chip bits 0 or 0x800. Writes for TMU1 and TMU2, absent, are
    dropped.
  - A texture word is placed by texBaseAddr × 8 and the LOD layout of tLOD and the format, as
    MAME's `internal_texture_w`.
  - A texel never written since reset leaves its pixel's content unknown, so a read of that
    pixel stops.
- Drawn: a non-perspective, point-sampled RGB 565 texture of one LOD (lod_min = lod_max).
  - S and T are iterated in 32.32 from vertex A, converted to 24.8 by truncation (out of
    range 0x80000000, as MAME's x86-64 build), shifted by the LOD, then clamped or wrapped.
  - The texture combine must pass the TMU's own texel through: zero other, add c_local.
  - Under trexInit1 bit 18 the texel is MAME's TMU configuration word, 0x11 in blue. The
    check's 0x0002 and 0x0003 follow from it under the 4 by 4 dither, and from no other blue
    of 0x10 to 0x12 (measured).
- Otherwise the reason is perspective-corrected texturing, trilinear filtering, more than one
  LOD, bilinear filtering, a texture format other than RGB 565, or another texture combine.

**Busy and the FIFOs** (from MAME's model; measured agreement over the ramp).
- Queueing:
  - A command's clocks keep the card busy.
  - While it is busy and PCI initEnable bit 1 is set, writes wait in the PCI FIFO and run in
    order as the card frees. Those are writes to every register but 0x14C to 0x15C and 0x200
    to 0x230, plus LFB and texture writes.
  - The PCI FIFO holds 128 entries, two per write. The memory FIFO behind it (fbiInit0 bit
    13; rows fbiInit4 bits 8 to 17 to 18 to 27, at most 131,072 entries) takes the oldest
    writes once the PCI FIFO's free entries fall to twice fbiInit4 bits 2 to 7.
  - Register reads return executed values.
- Status:
  - bits 0 to 5: PCI FIFO free writes, at most 0x3F;
  - bit 6: the retrace;
  - bits 7 to 9: busy;
  - bits 10 and 11: the displayed buffer;
  - bits 12 to 27: memory FIFO free writes, 0xFFFF when it is off;
  - bits 28 to 30: swaps pending.
- Where MAME would lose a write (a full FIFO), the model stops.

**Holding the writer** (from MAME's model: voodoo.cpp's `stall_cpu` and
`check_stalled_cpu`, gt64xxx.cpp's `pci_stall`, `master_mem0_w` and `perform_dma`).
- With fbiInit0 bit 4 set, a queued write that leaves the memory FIFO at its high-water mark
  (2 × 32 × fbiInit0 bits 14 to 24 entries, two per write) or the PCI FIFO's free entries at
  twice its low-water mark (fbiInit0 bits 6 to 10) makes the card hold its writer. The write
  itself is queued.
- The hold ends when, with the memory FIFO on, it is below its high-water mark, or, with it
  off, the PCI FIFO has more than twice its low-water mark (fbiInit0 bits 6 to 10) in free
  entries, or when the card is idle. MAME checks at the running command's end and at a
  swap; the model checks at the earlier of the two. (Correction, 2026-11-20: this item said
  "more than its low-water mark free"; the code, `check_release` in `voodoo_null.c`, holds
  while the free entries are at most twice the mark, the same quantity that starts the hold.)
- A CPU store returns only when the hold ends: virtual time passes in the store
  (`vclock_advance`, machine.h) while the card runs its queue. The clock's events run on the
  way at their own times, so the retrace raises VBLANK during a long hold. The interrupt is
  delivered at the next check point, after the store.
- The GT64010's DMA channel is held instead of the CPU: it moves no word while the card holds,
  polls the card at the next time the hold may end, and goes on from the word it stopped at.
  The CPU runs meanwhile. Cause bit 4 is raised at the measured completion time (`dma0.txt`,
  the fit above), or when the last word goes in, if that is later.
- Measured agreement (the 150 s attract run, `tests/test_boot.py`; the counts are the trace's
  `hold_cpu`, `hold_cpu_ns` and `hold_dma` totals). The two CPU figures count different sets:
  - 187 is the number of holds a CPU store started, that is, the stalls that began with a
    CPU write while the card was not already holding;
  - 3.38 s (3,378,204,411 ns) is the virtual time of every wait of a CPU store, including the
    stores that arrived while a hold the DMA had started was in force. A 20 s run already
    counts 187 holds and 0.35 s (measured, its trace, 2026-11-20); since the run is
    deterministic, most of the 3.38 s is CPU waits behind DMA-started holds after 20 s
    (inferred).
  The card held the DMA channel 3,273 times, once in each chain of the movie shape (130 small
  records and 256 of 0x640 bytes). No chain finished later than its measured completion:
  every chain of every shape completed within 5 ns of MAME's times. (Correction, 2026-11-20:
  this item first paired the 187 and the 3.38 s as one set, "held the CPU 187 times, for
  3.38 s in all, most of them behind a swap", which the counters do not show.)
- A DMA enable while a chain is still being delivered or its cause bit 4 is pending stops.
  MAME's model ignores the enable of an active channel and does not queue it (read,
  gt64xxx.cpp); the game never makes one.

**The DMA's completion time stays measured** (read and measured). MAME's GT64010 moves up to
32 words of the current record every 64 PCI clocks (1,939.39 ns at 33 MHz). It fetches the
next record when one ends and raises the completion at the end of the chain, and it moves
nothing while the card holds the bus. Without holds, those rules give a chain's time as its
bursts × 1,939.39 ns. Against `dma0.txt` that is short by 2,662 to 2,669 ns for every
small-record shape. That is 4.7% of the 28-record chain's 56,966 ns, and 0.24% of the
576-record chain's. For the movie shape it is short by 489,449 to 489,456 ns, 6.8%: about one
extra burst per large record, which MAME's holds during the frame-buffer writes would have to
supply. Neither difference comes within 1%, so the fitted delay above remains the completion
time. The hold acts only on the card's side: when the words arrive, and a completion that
would come before the last word. The constant part is inferred to be one burst period plus
the loader's dispatch from the interrupt to the game's handler, which MAME's times include
and the runtime does not run.

**Swaps** (from MAME's model).
- A swapbufferCMD write counts as a pending swap at once.
- Without bit 0 it swaps when it runs.
- With bit 0 the card is busy for (interval + 1) × 5,000,000 clocks, and at each retrace start
  that finds the retrace count (reset by each swap, at most 250) at the interval or more, it
  swaps. The queue then goes on from that time.
- A swap rotates front and back over two or three buffers. Before any fbiInit2 write MAME's
  layout has three, all at 0.

**Alternate register map** (from MAME's model). With fbiInit3 bit 0 set, a write with address
bit 21 renumbers registers 0 to 0x3F. Each colour, Z, A, S, T and W start value is followed by
its d/dX and d/dY, for the fixed-point registers and the float ones; registers from 0x40 up,
ftriangleCMD among them, are unchanged. The game sets bit 0 from 6.5527 s (measured). Decoded
this way, the DMA's alternate-map words (measured, `dma0.txt`, 150 s) are 137,936 each of
fvertexAx to fvertexCy, fstartS/T/W, fdS/T/WdX, fdS/T/WdY and ftriangleCMD. The earlier
count of alternate-map writes therefore hid all of attract mode's textured drawing. No swap
or fastfill goes through the alternate map; the DMA's standard-map words include 4,042
fastfillCMD and 7,159 nopCMD.

**Video timing and retrace.** The card's vertical retrace is the board's VBLANK interrupt
(from MAME's model: seattle.cpp connects the card's vblank callback to the board's VBLANK).
- Before the timing is programmed:
  - The screen runs MAME's default frames: 57 Hz, 480 rows, all visible, VBLANK at every
    frame from power-on. Measured agreement: the capture's frame counter steps at multiples
    of 1/57 s since power-on, for example frame 296 at 5.19298 s.
  - The card has no retrace. Measured: no status read before 6.5675 s has bit 6.
- When hSync, vSync, backPorch and videoDimensions are all non-zero, the screen is
  reconfigured (from MAME's model):
  - vtotal rows (vSync's two fields added) at the standard (15,750 Hz), medium or VGA line
    rate, whichever frame is closest to the current one;
  - the visible rows from backPorch's vertical field for videoDimensions' height;
  - the time of its last VBLANK start kept, unless that is a new frame ago, when VBLANK
    starts now.
- The card's retrace runs from row vSync-off to row 0, rows counting from the screen's VBLANK
  start (the end of the visible area). Each start raises IP6 while the board's VBLANK enable
  is set (slot 0x200 for line 0; loader-services.md section 1).
- With the game's values, written at 6.567466 s, that gives:
  - a frame of 291 rows at 15,750 Hz, 18,476,190.476 ns. Measured: 18,476,186 to 18,476,194
    ns between retraces.
  - a retrace of 4 rows, 253,968 ns. Measured: 251,727 to 254,120 ns.
  - a first retrace at 6,580,768,588 ns. Measured: between the reads at 6,580,767,773 and
    6,580,768,713 ns.
- The residual phase error against the capture is within the reads' spacing. Every status
  read of the capture agrees, the nearest falling 120 ns from a model edge. The capture's
  frame counter steps 21 rows after each model retrace start, between the accesses either
  side, as MAME's screen counts a frame at its VBLANK end (35 rows after its start).
- If the game registers line 0 before the timing is programmed, no VBLANK arrives until it is.
- Reprogramming the timing during a retrace stops, because when MAME's model ends that
  retrace is not modelled. The game programs it once.
- Timing registers that are all non-zero but give zero total lines stop, naming the four
  values; MAME's model would divide by them. An example is vSync 0x0000F000, with bits only
  outside its fields. A row's total pixels cannot be zero, because MAME adds 2 to hSync's
  fields.
- The card's state holds card times it has passed. If the virtual clock goes back under it,
  as a machine_reset without hw_reset makes it do, the next access stops.

**Known gaps in the card model.**
- A held DMA chain's words go in at once up to the hold and then as the card releases it,
  not 32 words every 1,939.39 ns as in MAME (see "Holding the writer"). Writes arrive at the
  card earlier than MAME delivers them, and the completion time is the measured one, not
  derived. Correction, 2026-11-20: until the hold was modelled, a chain reaching a water mark
  stopped the run.
  - The 253 reads of 0x0FFFF47F after 11.18 s (swaps by DMA) cannot be replayed, because
    the DMA's data was not captured. For those the model is from MAME's model.
- MAME's memory FIFO lives in frame-buffer memory. From 6.5678 s that is rows 192 to 511, over
  the aux buffer, and MAME's entries would overwrite those pixels. The model keeps the FIFO's
  writes apart.
  - What would show it: an LFB read of aux pixels there after the memory FIFO held entries.
    MAME would answer with its FIFO's encoding, the model with the pixels. The capture has no
    such read.
- A CPU store that arrives while the card is holding the DMA channel (a hold the DMA
  started) is queued at once and then held until the release (read, `enqueue` and `hold` in
  `voodoo_null.c`). MAME's model instead saves the CPU's write without passing it to the
  card, stalls the CPU, and re-issues the saved write when the card releases (from MAME's
  model: gt64xxx.cpp, `master_mem0_w` and `pci_stall`, read 2026-11-20). The
  order in which the card sees the CPU's word and the remaining DMA words can therefore
  differ from MAME's. What would show it: a read-back or a status value that depends on that
  order; none of the comparisons covers it (added 2026-11-20).

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

1. **Implement the loader's drivers, not its registers, for the game's view of the PIC,
   timers, IOASIC, disk and CMOS writes. Back the CMOS with memory the game can also read.**
   - GAME.EXE's own code touched none of the IDE, GT64010, IOASIC (bar the never-executed
     SOUNDSTAT writes), PIC, interrupt-controller, watchdog or LED registers. That holds
     statically, and dynamically with PC attribution in attract mode.
   - It calls open/read/write/ioctl on `cmos:`, `ioasic:`, `timer:`, `pic:`, `pci:`, `wdog:`,
     the console and files, with the ioctl commands listed above.
   - The stand-ins need:
     - `cmos:` as a 32 KB byte store with ioctls 0x6300 to 0x6302. Positions are addresses
       0xB6100000 + 4 × offset. The same bytes must also be readable by the game at those
       addresses, one byte per 32-bit word, because its CRC loop reads them directly.
     - `pic:` 0x7000 to 0x7002, returning the three serial-derived words.
     - the 0x69xx, 0x74xx and 0x73xx sets, whose meanings still need reading from the
       drivers named above.
   - Sure: high for attract mode; medium for gameplay; medium until the 0x69xx, 0x73xx and
     0x74xx semantics are read.
2. **Emulate directly what GAME.EXE touches itself:** the Voodoo (registers, LFB, texture
   memory at 0x08000000), the gun registers, the sound FIFO at 0x13000000, CMOS reads, the
   IOASIC SOUNDSTAT write (static only), the ASIC reset/flash register, and the reset
   vector. For the Voodoo, take
   the register list from the dynamic check (125 registers written, 16 read), not from the
   static census alone. Sure: high for these being touched; medium for completeness of the
   Voodoo list, since gameplay was not run.
3. **Interrupts to deliver:** VBLANK as CPU line 0, the IOASIC summary as line 3, GT64010
   DMA0 completion (and the three error causes) through the GT64010 handler table, the FPU
   and exception handlers through the loader's frame layout. The loader's default IP2 path
   (cause register, watchdog, LEDs) is the runtime's own business. Sure: high.
4. **Watchdog and LEDs** are written only by the loader, from paths the game reaches
   (interrupt paths, `wdog:`); the runtime can treat them as no-ops. Basis: measured (no
   watchdog or LED access by GAME.EXE code in the hardware run, static and dynamic) and read
   (the loader paths named above); that no-ops are enough is inferred. Sure: high.
5. **IDE, GT64010 DMA/PCI, SCSI and update ROM** matter only if loader code is run under an
   interpreter; with native stand-ins for the slots they need not be emulated. Sure: high.
   Correction, 2026-11-19, for the GT64010 DMA (`loader-services.md`, section 3 and open
   question 8): the game itself programs DMA channel 0 through the loader's `pci:` ioctl
   0x7301 (the "DMA0 6,075 control and 2,025 next writes" of the split run are these), once
   per scheduler pass, with a chain of descriptors that copies its Voodoo register writes and
   frame-buffer data, and its handler for GT64010 cause bit 4 takes the completion (read;
   measured in MAME: 3,580 starts and 3,580 completions in 150 s of attract mode). So the
   runtime must emulate chained DMA channel 0 behind ioctl 0x7301 and raise cause bit 4 when
   a chain is done; IDE, the GT64010's PCI side, SCSI and the update ROM still need no
   emulation.

## Not confirmed

- **Everything the three MAME runs measured covers boot and attract mode only.** A region or
  register GAME.EXE touches only during gameplay would be missing, both from "by GAME.EXE's
  own code" and from the static negatives, which the CMOS reads show can be incomplete.
- **Voodoo registers used only during gameplay.** The dynamic check covered boot and attract
  mode only, with no coin or input. Registers only gameplay uses (for example the float
  triangle path 0x088 to 0x100, fogColor changes, other chromaKey or alpha settings) would
  be missing from its list. Gameplay can't be reached without input in a headless run; a
  run with scripted coin and start inputs would close this.
- The IOASIC unlock (MAME: write 0xE2 to bus word 0) is not in the loader or GAME.EXE. MAME
  run 2 read the DIP switches at bus word 3, which is the unlocked order, so it happened
  earlier, presumably in the POST program (not analysed).
- What SOUNDSTAT writes from GAME.EXE do on the board, and what 0x17F00000 controls.
- The meanings of the `ioasic:`, `timer:` and `sysctrl:` ioctl numbers.
- The fd numbers of the standard descriptors (from the open order, not traced).
- Which CMOS bytes a played game reads or writes. In attract mode the reads (1,160 offsets by
  the game, 1,648 by the loader) and the writes (1,792 offsets) are measured, all within 0x000
  to 0x76B; gameplay (audits) may go beyond.
