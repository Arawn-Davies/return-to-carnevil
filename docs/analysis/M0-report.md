# M0 report: what extraction and analysis found

Milestone M0 (19 November 2026) built the tools that extract CarnEvil's files from the disk
image and analyse `GAME.EXE`, the boot ROM's loader and the hardware they use. This page is
the summary to plan M1 and M2 from. The detail and the evidence are in the six notes it
points to: [disk-format](disk-format.md), [exe-format](exe-format.md),
[opcode-census](opcode-census.md), [functions](functions.md),
[boot-handoff](boot-handoff.md) and [hardware-usage](hardware-usage.md), plus
`config/boot_state.toml` and `config/game.functions.toml`.

Every fact below carries its basis:

- **static**: measured by the committed tools (`python3 -m carnevil_tools …`) over the files;
- **read**: read from the code by hand with the `disasm` command;
- **MAME**: measured in MAME 0.276 in the Linux container. All MAME runs cover boot and attract mode only,
  with no coin or input: nothing about gameplay has been observed;
- **inferred**: a conclusion from the above, not measured.

## The executable

- `GAME.EXE` is 918,060 bytes, SHA-256 `f9455ce8…d3a6`: an 8-byte header, then a flat
  little-endian MIPS image. Header word 0 is the POSIX `cksum` CRC of the file from byte 4
  (0x2CAE3F54); word 1 is the load address (static; the loader computes and checks the same
  CRC, MAME).
- Load address and entry: 0x800C4000; the image ends at 0x801A4224 (static; the loader
  jumps to 0x800C4000 and the loaded image equals the extracted file byte for byte, MAME).
- Text 0x800C4000 to 0x8016DC4C (695,372 bytes); data from there to 0x801A4224, with one
  16-byte code island at 0x801A355C; BSS 0x801A4228 to 0x80800000, cleared by `_start`, which
  also sets sp to 0x80800000 (static, read).
- 2,093 entries in `config/game.functions.toml`. 29 of them are one-slot `stub` entries for
  the stub block (below) and are not the game's code, so the game has 2,064 functions of its
  own, including the island and the 3 manual starts of `config/game.manual.toml`. 75 jump
  tables (1,488 entries), 165,638 words reached as code, none of them an unknown encoding,
  262 unreached ranges (4.64% of the text) (static).
- Built with GCC, with 3dfx's Glide, an IDT MIPS runtime and DJ Delorie's libc linked in. 21
  functions are named from debug strings, 6 of those names probably wrong (static).

## Boot and the hand-off

- A normal boot is: boot ROM reset stub, the ROM's POST program, the ROM's loader, then
  `GAME.EXE`. `DIAG.EXE` runs only with the Test Switch on (MAME, both paths;
  [boot-handoff](boot-handoff.md)).
- The loader (ROM 0xBFC00F20 to 0xBFC16B60, 89,152 bytes, 333 functions) is copied to
  0x80000000 and **stays resident while the game runs**. It holds the IDE driver, the disk
  file system, the device drivers (`cmos`, `ioasic`, `timer`, `pic`, `sysctrl`, `wdog`, `tty`,
  `sound` and others), the exception vectors and the interrupt dispatcher (static, read,
  MAME).
- The 576-byte block at 0x800C408C is zero in the file. `_start` fills it by copying physical
  0x000 to 0x23F: the loader's table of 72 `j` slots. The game calls 29 of those slots; each
  is a service of the resident loader (MAME, byte for byte). `config/boot_state.toml` lists
  each slot, its loader routine and its callers.
- The game also calls a `break 0x409` stub (installs an exception handler through the
  loader's debug monitor, 18 callers), has 4 `break 0x407` asserts, and jumps to the reset
  vector 0xBFC00000 on 4 failure paths (static, read).
- State at entry (MAME, run 1): pc 0x800C4000, Status 0x30010401 (CU1, CU0, DE, IM2, IE, BEV
  clear, FR clear), FCSR 0x01000400, gp 0, all of the loader's handler tables empty; Config
  K0 = 3 is read from the reset stub, since MAME does not expose Config. `_start` sets sp and
  FCSR (0x01000000) itself. It does **not** set Status: the game's reached code has no `mtc0`
  at all, so it runs with the Status the loader left (static, read). The text does hold four
  unreferenced Status helpers in unreached code (see the open questions). The game reads
  nothing in RAM below its image except `_start`'s copy of the slot table (static, MAME).
- Interrupt handlers the game registers through the loader (static; all logged in MAME): CPU
  line 0 = VBLANK (IP6) 0x800C6030, line 3 = IOASIC (IP3) 0x8013F678, GT64010 cause bits 1 to
  4 (bit 4 DMA0 complete), FPU causes 3 and 5 (0x80150860 emulates the trapped instruction
  using the loader's frame), the `break 7`/`break 6` hooks, and a catch-all for exception
  codes 1 to 8 and 10 to 15.

## Hardware

What `GAME.EXE`'s own code touches (static, then MAME with exact PC attribution, attract
mode):

- **Voodoo 1** at physical 0x08000000: 125 registers written, 16 read; 20,342 register
  writes, 568,150 `vdstatus` reads (the busy-wait), 436,973 texture-memory writes, 8,237 LFB
  reads and 12 LFB writes in about 85 s (MAME). Only the fixed-point triangle path was used.
  The static census finds only 6 of the registers, because Glide reaches them through two
  one-instruction helpers (static, MAME; the helper explanation is inferred from approximate
  PCs).
- **Light gun** registers 0x16800000, all 8 (76,878 accesses); **sound FIFO** 0x13000000 (512
  writes); **0x17F00000** (1 read, 1 write; probably the gun flash control, inferred) (MAME,
  static).
- **CMOS reads**: 252,532 direct byte reads at 0xB6100000 + 4 × offset, one CRC loop at
  0x801458C0, 1,160 offsets in 0x000 to 0x767 (MAME). The static census cannot see them.
- **IOASIC SOUNDSTAT** writes: 13 static sites, none executed in MAME.

Everything else goes through the loader: IDE, GT64010 (timers, interrupt cause and mask, DMA,
PCI configuration), the rest of the IOASIC (inputs, coin, UART, sound latches, interrupt
control), CMOS writes and the write enable, watchdog, VBLANK clear, LEDs. The game's code made
none of those accesses; after the hand-off the loader made 9,142,595 GT64010, 2,985,826
IOASIC, about 32,600 IDE and 4,256 CMOS-write accesses on its behalf (MAME, split run).

The game's 160 `ioctl` calls (slot 0x0C8) all have constant command numbers (static,
`hwcensus --ioctl`): `cmos:` 0x6300 to 0x6302; `ioasic:` 14 commands in 0x6900 to 0x6911;
fd 5 (`timer:` by open order) 9 commands in 0x7401 to 0x740B; `pic:` 0x7000 to 0x7002;
`pci:` (the `sysctrl` driver, inferred) 0x7300 and 0x7301.

The three questions M0 was asked:

- **Security PIC**: the game never touches the PIC. It asks the loader's `pic:` driver for
  three words (ioctls 0x7000 to 0x7002) that the loader decoded from the PIC's serial block at
  start-up, before the game ran (static, MAME). The runtime has to supply three words, not a
  PIC.
- **CMOS/NVRAM**: read directly by the game, written only through the loader's `cmos:` driver.
  Attract mode wrote 1,792 bytes in five ranges between 0x000 and 0x76B (MAME).
- **LFB**: used, but in attract mode it is a side path (a frame-buffer memory test and a few
  pixel reads); drawing goes through the triangle registers (static, MAME).

## What M0 changed in the design

Each item is an assumption of the design document that M0 showed wrong or incomplete.

- **Boot chain.** The design says boot ROM, `DIAG.EXE`, boot ROM, `GAME.EXE`. A normal boot is
  reset stub, POST, loader, `GAME.EXE`; `DIAG.EXE` runs only with the Test Switch on, and
  control never returns to the ROM itself ([boot-handoff](boot-handoff.md), "Boot order").
- **The game doing its own disk access.** It does not. It calls 29 services of the boot ROM's
  RAM-resident loader for files, devices, PCI, raw sectors and handler registration
  ([boot-handoff](boot-handoff.md), "What `_start` copies"; [hardware-usage](hardware-usage.md)).
- **"The game's own file system code".** There is none in `GAME.EXE`: the file system is in
  the loader. An IDE controller under the game would never be reached by the game's own code
  ([hardware-usage](hardware-usage.md), "By GAME.EXE's own code").
- **Extraction.** Files are stored as chains of 4 KB blocks, not contiguously. The research
  scripts, and our first extractor, read three files wrongly, `GAME.EXE` among them; the first
  pinned `GAME.EXE` hash (`47aea64a…`) was of the mis-extracted file. Fixed in an extra task
  (5b) and re-pinned ([disk-format](disk-format.md), "Correction").
- **Function count.** The design's estimate was "on the order of 2,000" from `jr ra` and
  prologue counts. Discovery gives 2,093 entries, of which 29 are loader slots, so 2,064 game
  functions (3 of them manual starts MAME showed), plus 75 jump tables, 16 unresolved indirect
  jumps and 7 probable callbacks that still need manual entries ([functions](functions.md)).
- **MIPS IV needs.** The design lists indexed FP loads and stores, `pref` and FP condition
  codes 0 to 7. Reached code uses none of the indexed forms or `pref`, and condition codes 0 to
  2 only ([functions](functions.md), "Census of reached code").
- **CMOS.** The design treats it as a settings file behind the hardware. The game reads the
  CMOS directly at 0xB6100000 (one byte per 32-bit word) and writes it only through the loader
  ([hardware-usage](hardware-usage.md), question 2).
- **Security PIC.** Not checked by the game; three loader-held words suffice
  ([hardware-usage](hardware-usage.md), question 1).
- **Exception vectors.** The design runs "the recompiled exception handler (vector at
  0x80000180)". The vectors are the loader's slots in low RAM, jumping to the loader's handler
  at 0x80004368 and dispatcher at 0x80004B18. `GAME.EXE` has no `eret` anywhere, and its
  reached code has no exception entry code and no COP0 moves, only handlers registered
  through the loader ([boot-handoff](boot-handoff.md), "Exceptions and interrupts"). The only
  COP0 moves in the text are four unreferenced Status helpers in unreached code
  ([functions](functions.md), "Unreached ranges").
- **Code outside `GAME.EXE`.** The design expects "boot ROM routines the game calls". The game
  calls the RAM copy of the loader, never the ROM, except the reset vector to reboot
  ([boot-handoff](boot-handoff.md)).
- **What the boot state is for.** The design expects the runtime to need the boot ROM's state.
  The game needs little of it: Status (its reached code never writes it), the slot table copy,
  and
  whatever the chosen loader replacement needs (decision 4).

## Decisions for M1 and M2

These are options with a recommendation each, not decisions already taken.

### 1. How to supply the loader's services

- **(a) Native stand-ins for the 29 services**: file I/O from the extracted files or the disk
  image, the raw-sector and partition calls from the image, PCI configuration answered for the
  Voodoo, `cmos:` as a 32 KB byte store, the ioctl sets, the handler registrations into the
  runtime's own interrupt and exception dispatch, `break 0x409`/`0x407` and the `break 7`/`6`
  hooks.
  - **(a2) Stand-ins plus individual slots interpreted**: as (a), but a slot whose meaning
    is not yet read (0x038, 0x190, 0x1B8, 0x1C0, 0x1D8) runs the loader's own routine under
    the interpreter. The interpreter then needs the loader image (from the user's
    `boot.u32`) and the loader state that routine reads, and the hardware it reaches. By
    direct calls (static, printed per slot by `python3 -m carnevil_tools hwcensus --loader
    <boot.u32> --reach`): 0x038, 0x190, 0x1B8 and 0x1C0 each reach 46 to 57 loader
    functions, the IDE task file, alternate status and bus-master DMA, the IOASIC, the ASIC
    reset register and the LEDs, with 3 of those functions making indirect calls (the driver
    table); 0x1D8 reaches 5 functions and no hardware. So for the four file-system slots the
    interpreter would need IDE emulation and the loader's file-system and descriptor state
    kept consistent with whatever the native file stand-ins do, which they would not
    maintain on their own. For 0x1D8 the cost is only the loader state it reads, which was
    not traced.
- **(b) Recompile the loader too** and emulate the hardware beneath it at register level:
  IDE, GT64010 (timers, cause/mask, DMA, PCI), IOASIC (sound latches, inputs, UART, interrupt
  control), CMOS with its write enable, the CS3 control registers.
- **(c) Run the loader under the interpreter**, with the same hardware as (b).

Weighed against the measurements:

- The game's own code touches only the Voodoo, the gun, the sound FIFO, CMOS reads,
  0x17F00000 and (statically) SOUNDSTAT (MAME, static). Under (a) the runtime models those and
  nothing else at register level. Under (b) and (c) it must also model IDE, GT64010, the rest
  of the IOASIC and the CS3 registers, behind the 9.1 million GT64010 and 3.0 million IOASIC
  accesses the loader made in the 85 s after the hand-off (over the whole 90 s run, 3.7
  million `TIMER_CONTROL` reads and as many writes, 1.7 million SOUNDSTAT reads). Each is a
  slow-path MMIO access; under (c) each is also interpreted. (a) needs none of them.
- The interface (a) must implement is closed and known: 29 slots with every caller listed,
  160 ioctl sites with constant commands (31 command numbers over five drivers). What is not
  known is the meaning of slots 0x038, 0x110 (partly), 0x190, 0x1B8, 0x1C0, 0x1D8 and 0x220's
  callbacks, and of the 0x69xx, 0x73xx and 0x74xx commands. These must be read from the loader
  before the stand-ins are written. (b) and (c) need none of that understanding.
- Interrupts are dispatched by the loader's vectors. Under (a) the runtime dispatches
  natively, reproducing the loader's ABI: the line map (IP6 → line 0, IP3 → line 3, …), a0 =
  the Cause IP bit for CPU-line handlers, (Cause, frame) for GT64010 and exception handlers,
  and the frame layout the FPU-emulation handler 0x80150860 reads (Cause +0x180, EPC +0x110,
  FPRs +0x218, FCSR +0x31C). No COP0 emulation is needed for the game's reached code, which
  has no COP0 moves; the four unreferenced Status helpers would need Status emulated if they
  turn out to be reached (open questions). Under (b) the recompiled vector code runs, so the
  runtime must emulate COP0 (Status, Cause, EPC, `eret`), including the `break` exception path
  into the loader's debug monitor (the monitor itself is recompiled with the rest of the
  loader), the GT64010 cause register and the IOASIC interrupt registers. Under (c) every
  VBLANK and IOASIC interrupt runs interpreted loader code.
- (b) and (c) also need the loader's state at entry: its image is in the user's boot ROM,
  but its BSS (the `loader_data` ranges in `boot_state.toml`) comes from a MAME dump, or from
  running the loader's start-up, which drives the PIC and the disk.
- Two costs land on (a) whichever way: the sound path moves from the IOASIC registers to the
  driver interface (the stand-in for the sound ioctls talks to the DCS emulation), and the
  GT64010 DMA0 work the loader does after the hand-off (6,075 control writes; the game has a
  DMA0-complete handler that ran) must be understood and reproduced (MAME; what DMA0 moves is
  not known).

**Recommendation: (a), with (a2) as the fallback for individual slots.** (a) keeps the
runtime to the hardware the game itself uses, removes the hottest MMIO traffic measured, and
needs no COP0 or debug-monitor emulation for reached code. Its risk is semantic: the
unconfirmed slots and ioctl sets must be read first, and each stand-in checked against MAME
by logging the service's arguments and results at the slot in a MAME run. Reading a slot and
writing its stand-in is the plan; interpreting it (a2) is the fallback where reading stalls,
and is cheap only for a slot like 0x1D8 that reaches no hardware. For the four file-system
slots it would bring back IDE emulation and the loader's file-system state, so they should
be read rather than interpreted. This agrees with `boot-handoff.md` (recommendations 2 and 4:
interpreting loader code is the fallback, and needs the loader image and its live state) and
`hardware-usage.md` (recommendation 5). Interpreting the whole loader (c) is not worth
keeping, since it brings back all of (b)'s hardware.

### 2. Where to intercept Voodoo traffic

- **(a) At the two Glide helpers** 0x8015DB04 (store a1 at a0) and 0x8015DB0C (load from a0):
  bind them to native functions that decode the address.
- **(b) At the memory-access level**: every access the recompiler cannot prove to be RAM goes
  through a range check, and the Voodoo range is decoded there.

The helpers are not the only route. The static census found 14 direct register sites (6
registers), 39 LFB sites and texture-memory writes from copy routines (static); in MAME the
436,973 texture writes came from copy routines, not the helpers, and the LFB accesses from
the memory test and pixel reads (MAME). The helper role itself is inferred from approximate
PCs. So (a) alone would miss traffic, and (b) is needed anyway for the gun, the sound FIFO
and the CMOS reads. The rates are modest (about 240 register writes and 5,100 texture writes
per second in attract mode; gameplay not measured), so (b)'s cost is a range check per
non-constant access.

**Recommendation: (b) as the mechanism**, decoding the chip-select bits (0x800, 0x1000,
0x2000) seen on the texture-unit registers; no alternate register map or byte swizzle was
seen. Optionally bind the two helpers to the same Voodoo model as well, as a fast path and
as the hook for the later Glide-level work, and answer the `vdstatus` busy-wait (568,150
reads) as an idle point.

### 3. What the recompiler must add to N64Recomp

From the census of reached code ([functions](functions.md)):

- Little-endian memory and a flat-image loader fed by `config/game.functions.toml`.
- MIPS IV: `movz` 82, `movn` 33, `movf` 5, `movt` 2, `madd.s` 64, `madd.d` 6, `msub.s` 14,
  `nmsub.s` 6, `nmadd.s` 1, `recip.s` 1. FP condition codes 1 and 2 (4 uses, one function at
  0x80155B18); everything else uses cc0. No indexed FP loads or stores, no `pref`, no
  `movX.fmt` (static).
- Single and double FP with FR = 0 (Status at entry, MAME; never changed by the game's
  reached code, static): doubles live in even/odd register pairs. The four unreferenced
  Status helpers set or flip FR (open questions); if they are ever reached, the FPU model
  must follow FR at run time. Whether N64Recomp's FPU model handles
  FR = 0 has to be checked (inferred risk).
- Likely branches: 858 in reached code (`beql` 325, `bnel` 323, `bc1tl` 102, `bc1fl` 71, the
  rest 37). No control transfer sits in a delay slot (static).
- No 64-bit integer instructions and no `ll`/`sc` in reached code (static).
- `break`: the 34 GCC checks (`break 7` divide by zero, `break 6` overflow) call the hooks the
  game registers; `break 0x409` installs a handler (a0 code, a1 handler); `break 0x407` is an
  assert. These become runtime calls, not traps through a vector.
- The stub block 0x800C408C to 0x800C42CC (end exclusive) must not be translated from the
  file: calls to its 29 slots become calls to the runtime's loader stand-ins (decision 1).
  `config/game.functions.toml` marks it as `[[stub_block]]` and its 29 entries as `stub`.
- 75 jump tables as `switch`. 16 unresolved `jr`: 2 switches whose tables are known
  (0x80174328, 0x801757A8) and 14 label-table dispatches in the block-copy routines; they need
  `manual` entries or the interpreter fallback. 6 of the 14 ran in MAME.
- Of the 10 probable callbacks, the 3 MAME showed are manual starts in
  `config/game.manual.toml`: 0x800C6030 is the VBLANK handler and ran thousands of times,
  0x8014A930 ran, 0x80151A70 is registered as the trap handler (MAME). The other 7 still need
  `manual` starts or the interpreter fallback (static).
- 2 `j` instructions into 0x80151E90, a shared restore path in hand-written code, must be
  treated as jumps between functions (static).
- 63 `jalr` sites, through the lookup table of recompiled functions.

### 4. What the runtime provides at game entry

From `config/boot_state.toml`:

- Registers: pc 0x800C4000 and Status 0x30010401 matter for the whole run (FR = 0; the
  game's reached code never writes Status). sp and FCSR matter only until `_start` sets them;
  gp is unused; ra 0x8000EBE8 matters only if `main` returns (the loader would then report it
  and stop).
- The stub block: the runtime either places the loader's 576-byte slot table (from the user's
  `boot.u32`, ROM 0xBFC00F20) at physical 0 so that `_start`'s copy is faithful, or binds the
  slots at recompile time and lets `_start` copy whatever is there. With (a) of decision 1 the
  bytes are never executed.
- Slot 0x1A8 returns the address of 4 zero words; `main` ignores it.
- The CMOS bytes at 0xB6100000, seeded from a NVRAM file or blank, and the three `pic:` words
  (stored by the loader at 0x800B8650, 0x800B8398, 0x800B867C, read; their values should be
  in the run 1 RAM dump, inferred, and were not recorded).
- Nothing else in low RAM for option (a) (static). For (a2): the loader image and the state of
  each interpreted slot's routines (inferred). For (b) or (c): the loader's image and every
  `loader_data` range in `boot_state.toml`, and the exception vectors (inferred).

## Open questions and risks carried into M1

- **All dynamic evidence covers boot plus attract mode only.** No game was played: registers,
  devices, CMOS offsets, ioctls or callbacks used only in gameplay are missing. A MAME run with
  scripted coin and start inputs would close this.
- The meaning of loader slots 0x038, 0x110 (partly), 0x190, 0x1B8, 0x1C0, 0x1D8 and of the
  callbacks 0x220 installs; the names of the file and device slots come from structure, not
  symbols.
- The meaning of the `ioasic:` (0x69xx), `timer:` (0x74xx) and `sysctrl` (0x73xx) ioctls; the
  fd numbers come from the open order, not a trace; fd 5 may be `tty:` rather than `timer:`;
  that `pci:` opens `sysctrl` is inferred.
- Which loader service drives GT64010 DMA0 after the hand-off, and what it moves.
- Which code made the Voodoo accesses outside `GAME.EXE` (1,916 swapbufferCMD, 399,314
  texture writes): the POST program before the hand-off is inferred, not measured.
- Which Status interrupt-mask bits the loader's services set when the game registers its
  handlers (IP3 and IP6 must be unmasked by loader code; inferred).
- **Four unreferenced Status helpers** at 0x80167FB8 to 0x80167FF4, in one unreached
  leaf-shaped range just before the function at 0x80167FF8 ([functions](functions.md)). Two
  read Status and form it with bit 0x04000000 (FR) set or flipped; two write v1 to Status
  (read, `disasm`). Not known whether they are reached: no direct call or jump reaches them,
  no word in the image points into the range and no lui-built pointer lands in it (static:
  the range is not among the refused pointers of `functions.md`, and a scan of every image
  word found none), but a pointer built some other way would not be seen. No MAME run can
  say: run 3 set no breakpoint there, and the memory taps record PCs only for hardware
  accesses, which these helpers do not make. If they are reached, the runtime must emulate
  Status reads and writes for them (a `manual` start and a stand-in for each, or the
  interpreter with Status emulated), and FR could change at run time (decision 3).
- The exception-frame offsets other than Cause, EPC, the FPRs and FCSR; Config's read-only
  fields.
- What the game's SOUNDSTAT writes do, and what 0x17F00000 controls.
- Whether IP3 has sources other than the IOASIC; the GT64010 cause-bit names come from MAME.
- The 7 probable callbacks left and 10 of the 16 unresolved jumps were not reached in MAME;
  141 leaf-shaped unreached ranges are dead code or reached in ways the analysis cannot see.
  Every "the game does not" statement about reached code (no COP0 moves, Status never
  written) holds only if those ranges stay unreached; the Status helpers above are one of
  them.
- `DIAG.EXE` starting `GAME.EXE` is static only (out of scope, since the port skips `DIAG.EXE`).
- The IOASIC unlock is presumably done by the POST program (not analysed).
- MAME is the oracle, not the board: where MAME's emulation is incomplete (for example the gun
  flash control), so is the evidence.

## How the findings were checked

Static numbers come from the Python tools in `carnevil_tools/`, which have unit tests on
synthetic fixtures and checks against the real files (201 tests). MAME numbers come
from the Lua scripts in `tools/mame/` (command lines in its README), run under MAME 0.276 on
the Linux container, the oracle: they set breakpoints and logged registers (boot hand-off, registrations,
callbacks), dumped RAM at the hand-off, and tapped the Voodoo and every other hardware range
with PC attribution (`-nodrc` for exact PCs). Each task's result was reviewed independently,
the reviewer re-running the numbers. Twice the dynamic check overturned a static conclusion:
the RAM image at the hand-off differed from the extracted `GAME.EXE` in its last 556 bytes,
which exposed the contiguous-block extraction bug; and the hardware tap showed the game
reading the CMOS directly 252,532 times, where the static census had found no CMOS access by
the game.

## Still to record

- **MAME baseline frame rate on the PS4**, for CarnEvil's attract mode and first level, with
  the RetroArch statistics overlay, the MAME core and the current settings. To be recorded the
  next time the PS4 is up for another reason; it is the floor "not slower than MAME" is
  measured against in M7.
