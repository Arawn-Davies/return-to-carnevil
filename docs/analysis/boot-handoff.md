# Boot hand-off: what runs before GAME.EXE

The native port skips the boot ROM and starts `GAME.EXE` at 0x800C4000. This page says
what the board has set up by then, which of it the game uses, and how the runtime should
provide it. `config/boot_state.toml` holds the values in machine-readable form.

Facts, addresses and sizes only: no instruction listings. `python3 -m carnevil_tools disasm
<file> --base <addr> --start <addr> --count <n>` prints any of the code named here to the
terminal. `--base` is the address of the file's first byte: 0xBFC00000 for `boot.u32`,
0x800C3FF8 for `GAME.EXE` and 0x800FFFF8 for `DIAG.EXE` (their 8-byte header comes first).
The loader and the POST program run at 0x80000000 from ROM offsets 0xF20 and 0x16B60, so a
loader address A is ROM address A - 0x80000000 + 0xBFC00F20.

## Evidence

- Static reading of `boot.u32` (SHA-256 `05346e09...c4a6`, mapped at 0xBFC00000; MAME's
  `carnevil1_9.u32` is the same file), `GAME.EXE` and `DIAG.EXE`.
- MAME 0.276 (Debian package) in the Linux container, `carnevil` set with the `carnevil` CHD (MAME:
  "romset carnevil is good", title CarnEvil v1.0.3), headless, debugger on. `-debugscript`
  with `-debugger none` does not run the script in this build; a Lua `-autoboot_script` that
  sets breakpoints through `manager.machine.debugger:command()` does, and was used for all
  runs. Breakpoint actions log registers to `debug.log` and save RAM with `save`.
  - Run 1, default DIP switches: breakpoints at the loader's checksum compare, at its jump to
    the EXE and at 0x800C4000; 8 MB RAM dump at GAME.EXE's first instruction. Reached
    GAME.EXE after about 18 s wall time.
  - Run 2, Test Switch on: the DIAG.EXE path (see "Boot order").
  - Run 3: GAME.EXE running for 149 emulated seconds from a blank NVRAM, with logging
    breakpoints on the handler registrations and on the callbacks and unresolved jumps of
    `functions.md`. MAME's NVRAM after it (131,072 bytes, 7,168 of them not 0xFF, so it was
    written to) is kept as `mame/nvram-carnevil/` on the share.
- Scripts, logs and dumps stay on the share under `reincarnevilation/work/mame/`.

"Confirmed" below means seen in MAME as well as read statically. Anything else is marked.

## Boot ROM layout

The ROM holds two programs that run from RAM, plus a small reset stub:

- 0xBFC00000 to about 0xBFC00F20: reset stub. 0xBFC00000 is the reset vector. 0xBFC00008
  is a second entry point (start the loader). The other 8-byte entries up to 0xBFC00200 jump
  to a routine that returns 0. The BEV vectors 0xBFC00200, 0xBFC00280 and 0xBFC00300 (TLB
  refill, XTLB refill, cache error while BEV is set) show an error code on the board LEDs
  (0xB7900000) and blink it forever; 0xBFC00380 (general exception) jumps to the return-0
  routine.
- 0xBFC00F20 to 0xBFC16B60 (89,152 bytes): the **loader**, linked at 0x80000000, entered at
  0x80004000. It holds the strings `Booting diag.exe` and `Booting game.exe`, an IDE driver,
  the disk file system, a console, PCI helpers and the exception handling.
- 0xBFC16B60 to 0xBFC55B74 (258,068 bytes): the **POST program**, also linked at 0x80000000
  and entered at 0x80004000: power-on tests (CPU, disk, ROM checksum, memory), PCI scan,
  Glide/Voodoo set-up.
- Above 0xBFC55B74 the ROM is blank but for 4 bytes.

Both programs start with the same kind of 576-byte table at 0x80000000: 72 eight-byte slots,
each a `j` to a routine plus a `nop`. Slots 0x000, 0x080, 0x100 and 0x180 are the MIPS
exception vectors (TLB refill, XTLB refill, cache error, general exception); the others are
entry points to services. The two programs' tables point at different addresses.

## Reset path

1. 0xBFC00000 jumps to 0xBFC00388. It sets Status to 0x20410000 (CU1, BEV, DE), clears
   Cause, writes Config = 3 (K0 cacheable write-back) and clears FCSR. It probes the four RAM
   banks with test patterns and programs the GT64010 DRAM registers (0x0C000448 to
   0x0C000458).
2. It tests the first words of RAM, initialises the caches, clears 0xA0000000 to 0xA0800000
   (8 MB), keeps the top of RAM in f0, and copies the POST program from ROM to 0x80000000
   (byte sum of the ROM range, copy, cache flush, byte sum of the RAM copy; a mismatch halts
   with an LED code).
3. It sets sp = 0x80800000 and jumps to 0x80004000 (the POST program). Confirmed: the first
   hit of a breakpoint at 0x80004000.
4. The POST program runs (which tests run depends on the DIP switches; not traced) and then,
   from 0x80013D8C, calls its own slot 0x1C8, which jumps to ROM 0xBFC00008. Confirmed:
   breakpoint at 0xBFC00008 hit with ra = 0x80013D94.
5. 0xBFC00008 clears Status.IE, copies the loader from ROM to 0x80000000 over the POST
   program, verifies the copy the same way, flushes the caches, sets sp = 0x80800000 and
   jumps to 0x80004000. Confirmed: the second hit of the 0x80004000 breakpoint.

## The loader

Its start code sets Status = 0x30010001 (CU1, CU0, DE, IE) and FCSR = 0x01000400, clears
its BSS (0x80015C40 to 0x800B8680 cached and 0xA00B8680 to 0xA00C1410 uncached), and
initialises its drivers. Its boot selection (0x8000F0F8 onwards) then:

1. opens `scsi:`; if that succeeds it closes it and goes straight to step 5 (not taken on
   this board, which has no SCSI device);
2. reads IOASIC 0xB600000C (MAME's `DIPS` port) bit 15, the **Test Switch** (1 = off);
3. if the Test Switch is on: prints `Booting diag.exe` and loads and runs `diag.exe` at
   0x80100000;
4. prints `Booting game.exe` and loads and runs `game.exe`;
5. if that returns, prints `Debug Mode` and executes `break 0x402` (20-bit code, see
   "Exceptions and interrupts" for the notation), which enters the debug monitor.

### Load and run an EXE (0x8000E930, slot 0x198)

Arguments: a0 = file name, a1 = a load address (0x80100000 or 0x800C0000; not used by the
routine), a2 = pointer to 4 argument words. Steps:

1. copy the 4 argument words to 0x800B6E30 (the block slot 0x1A8 returns);
2. upper-case the name in place, reset the handler tables (slot 0x0D8), mask the interrupts,
   reset devices, clear the break hooks;
3. look the file up and open it; read header word 0 into 0x800B6E40 and header word 1 into
   a local (the load address);
4. seek back to byte 4 and read the rest of the file, from the load-address word on, to the
   load address;
5. flush the data cache; compute the checksum of the (file size - 4) bytes at the load address
   and store it at 0x800B6E44; if it differs from header word 0 return 6 (not booted);
6. move the image down 4 bytes (byte copy), so that file byte 8 lands at the load address;
7. clear a flag and `jalr` to the load address. If the EXE returns, the routine returns 3.
   Other failures return 0, 1, 2 and 5.

This confirms `exe-format.md`'s layout (word 1 is the load address and the entry point, the
image is file bytes 8 onwards) and identifies **header word 0**: the POSIX `cksum` CRC of file
bytes 4 to the end, that is the load-address word followed by the image. The algorithm is
CRC-32 with polynomial 0x04C11DB7, MSB first, initial value 0, the byte length appended
least-significant byte first, and the result complemented; the table is at 0x80015450 in the
loader. Checked:

- DIAG.EXE: the CRC of the extracted file from byte 4 is 0x5799F785, its header word 0.
- GAME.EXE: in MAME the loader's computed value was 0x2CAE3F54, equal to header word 0
  (confirmed). The CRC of the load-address word plus the 918,052 image bytes in the RAM dump
  is also 0x2CAE3F54. The CRC of the extracted file is not (see the next section).

`exe-format.md` records this (updated in Task 5b).

## Finding: the last block of the extracted GAME.EXE is wrong

**Fixed by Task 5b.** `carnevil_tools/disk.py` now follows each file's block chain
(`disk-format.md`). The corrected GAME.EXE has SHA-256
`f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6`; its header checksum
matches (0x2CAE3F54) and its bytes from offset 8 equal the RAM image at 0x800C4000 in full.
Its last block is block 18828, not 16351. DIAG.EXE is unchanged. Of the other files only
`HSTD02.WMS` and `HSTD05.WMS` changed. The text below is the finding as it was made.

The RAM image at GAME.EXE's entry equals `part0_files/GAME.EXE` except in its last 556 bytes,
0x801A3FF8 to 0x801A4224 (file offsets 917,504 to 918,060), where 315 bytes (in 113 words)
differ, the first at 0x801A3FF8 and the last at 0x801A4217. 917,504
is exactly 224 × 4096, so the extractor read the file's last 4 KB block from the wrong place.
The loader's file system follows a chain of 16-bit block numbers (a FAT: two entries per
word, read by 0x80007CCC), while `carnevil_tools/disk.py` assumes a file's blocks are
contiguous. DIAG.EXE's checksum matches, so its blocks are contiguous; GAME.EXE's last block
is not. The bytes are in the data section (text ends at 0x8016DC4C), so the function
analysis is not affected. The extractor needs fixing (outside this task); until then the
runtime must not trust those 556 bytes of the extracted file. Other extracted files that span
more than one block may be affected in the same way.

## Boot order

The plan assumed boot ROM, then DIAG.EXE, then boot ROM, then GAME.EXE. The order is:

- **Normal boot (Test Switch off, the default): boot ROM reset stub, POST program, loader,
  GAME.EXE.** DIAG.EXE does not run. Confirmed: in run 1 the only EXE the loader started was
  at 0x800C4000; no breakpoint at 0x80100000 was hit.
- **Test Switch on:** the loader starts DIAG.EXE (confirmed in run 2: load-and-run called with
  `diag.exe` and load address 0x80100000, entry 0x80100000, argument words all zero, Status
  0x30010401, FCSR 0x01000400, the same as GAME.EXE gets). DIAG.EXE itself starts GAME.EXE:
  it calls slot 0x198 with `GAME.EXE` from 0x80100E80, and with a name from its menu from
  0x80118040 (static). If DIAG.EXE returned, the loader would go on to `Booting game.exe`.
  Not confirmed: in run 2, with the switch turned off again after 61 s, DIAG.EXE was still in
  its menu at 200 s (it waits for operator input that the run did not give), so the hand-off
  from DIAG.EXE to GAME.EXE was not observed.
- GAME.EXE can start DIAG.EXE the same way (0x800C6950 calls slot 0x198 with `diag.exe`),
  after removing the disk callbacks (slot 0x220) and calling 0x80151AC0.

Control never returns to the boot ROM itself: everything after step 5 of the reset path runs
in the RAM-resident loader, which stays resident while GAME.EXE runs. GAME.EXE's only way
back into the ROM is a jump to the reset vector 0xBFC00000 (4 places, all after a failed
call; a reboot).

## State at GAME.EXE's entry (confirmed, run 1)

- pc 0x800C4000; sp 0x807FFFA8 (the loader's stack); gp 0; ra 0x8000EBE8 (back into the
  load-and-run routine);
- a0 0x801A4224, a1 0x801A4228, a2 0, a3 0x000E0228, v0 6, v1 0x2CAE3F54, s0 0x800B6DD0,
  s1 0x800C4000, s2 0xBFC00008: leftovers of the loader, none read by `_start`;
- Status 0x30010401: CU1, CU0, DE, interrupt mask IM2 only, IE set, BEV clear (exceptions go
  to the RAM vectors). Interrupts are enabled on entry, but no handler is installed yet and
  only IP2 is unmasked;
- FCSR 0x01000400 (FS, divide-by-zero enable). `_start` writes 0x01000000;
- Config: K0 = 3 from the reset stub; nothing else writes Config (no `mtc0` to it in the
  loader, the POST program or GAME.EXE). MAME does not expose Config, so the read-only
  fields are unconfirmed;
- Cause 0, EPC 0x80005028 (stale), Count 0x00AF5028, Compare 0xFFFFFFFF.

All the loader's handler tables are empty at entry: the exception table (0x80014660), the
CPU interrupt-line table (0x800146A0), the GT64010 cause table (0x800146E0), the FPU table
(0x80014648) and both break hooks. The 4 argument words at 0x800B6E30 are zero.

## What `_start` copies: the stub block (confirmed)

`_start` (0x800C4000) sets sp = 0x80800000, clears 0x801A4228 to 0x80800000 byte by byte, and
copies 0x240 bytes (144 words) from 0xA0000000 to 0xA00C408C, that is physical 0x000 to 0x23F
to the zeroed block 0x800C408C to 0x800C42CC (end exclusive), through KSEG1. The source is
the loader's slot table: at entry the first 0x240 bytes of RAM equal the loader's ROM image byte for byte, and
not the POST program's. The copy therefore holds 72 `j` instructions into the resident loader
plus their `nop`s. Because `j` keeps the upper 4 address bits, each copied slot still jumps to
the loader's routine at 0x8000xxxx.

So the 29 call targets in the block are not code of their own: each is a slot, and calling
it calls a routine of the RAM-resident loader. The block also holds copies of the 4
exception-vector slots, which GAME.EXE never calls. `_start` then calls 0x80143EB8, 0x801412D0,
slot 0x1A8 (the argument block's address) and jumps to `main` (0x800C4524) with that address
in a0. `main` does not use a0.

The slots GAME.EXE calls, with the loader routine each reaches (`[[external_call]]` lists the
callers):

- 0x018 0x8000B1D0 close; 0x028 0x8000B240 open for writing; 0x038 0x8000B70C file-system
  call (unconfirmed); 0x088 0x8000BB68 open; 0x090 0x8000BCB0 read; 0x0C0 0x8000BE10 write;
  0x0C8 0x8000BEB0 ioctl (160 callers, most in the sound driver and the other device code);
  0x0E0 0x80006754 isatty;
  0x190 0x80009A80, 0x1B8 0x80009C9C file-system calls (unconfirmed); 0x1C0 0x80009CBC
  lseek-like (unconfirmed).
- 0x0E8 0x80004E3C write back and invalidate the 32 KB data cache.
- 0x0F0 0x8000F95C PCI configuration read, 0x0F8 0x8000F930 PCI configuration write (Glide's
  `sst1Init` code), 0x108 0x8000E7D0 PCI vendor probe, 0x110 0x8000E80C PCI device set-up.
- 0x118 0x80010368 select disk partition, 0x128 0x800103BC partition table, 0x158 0x800112F4
  raw IDE sector transfer, 0x1D8 0x80012718 disk call with a timeout (unconfirmed), 0x220
  0x800127AC `install_disk_info` (callers include the movie player around 0x800E7188).
- 0x198 0x8000E930 load and run an EXE; 0x1A8 0x8000EE30 argument block address.
- 0x0D8 0x80005048 reset all handler tables; 0x1E0 0x800060BC GT64010 interrupt handler;
  0x1E8 0x8000619C FPU exception handler; 0x1F0 0x80006200 break 7 hook; 0x1F8 0x80006210
  break 6 hook; 0x200 0x80006220 CPU interrupt-line handler.

The names of the file and device calls come from their structure (descriptor lookup, then a
call through the device's driver table) and their callers, for example `write(1, msg, 17)` at
0x801447B4; "unconfirmed" means the routine was not read far enough to be sure.

## Exceptions and interrupts

The vectors at entry are the loader's slots: 0x80000000 jumps to 0x80005F0C (TLB refill),
0x80000080 to 0x80005F9C (XTLB refill), 0x80000100 to 0x8000602C (cache error; the R5000 takes
it at 0xA0000100, the same word) and 0x80000180 to 0x80004368 (general exception). TLB refill,
XTLB refill and cache error print a message, call an optional hook (0x800147B8, 0x800147BC,
0x800147C0, none set) and stop, waiting for the watchdog (0xB7100000).

The general exception handler saves state in a debug-monitor area at 0xA00B86A8 and reads
Cause. A mask at 0x800B89E0 (0xFFFF0200 at entry) sends exception code 9 (breakpoint) to the
debug monitor; every other code goes to the dispatcher. The dispatcher (0x80004B18) saves all
registers in a frame at 0x80014328 (EPC at offset 0x110, FPRs from 0x218), switches to the
exception stack at 0x800C1400 and calls 0x80005244 (Cause, frame):

- **Interrupts** (code 0): for each pending bit IP0 to IP7 it calls the handler installed for
  that CPU interrupt line (table 0x800146A0, line map at 0x800146C0, slot 0x200), and when
  that is missing or returns non-zero, the loader's default (0x80005368), which serves the
  GT64010 cause register (0x0C000C18 masked by 0x0C000C1C) through the table at 0x800146E0
  (slot 0x1E0), the IOASIC and the watchdog/LED tick.
- **Other codes**: the handler installed for the code (table 0x80014660), then, when it
  returns non-zero or is missing, the loader's default for the code (table 0x80014760). The
  default for floating-point exceptions (code 15) walks the saved FCSR cause bits and calls the
  handler for each from the table at 0x80014648 (slot 0x1E8), with the frame in a0.
- **break** instructions, in the debug monitor (0x80004410), by their 20-bit code (instruction
  bits 6 to 25). On this page `break 7` and `break 6` are GCC's 10-bit form (the code in bits
  16 to 25, words 0x0007000D and 0x0006000D); every hexadecimal code from 0x400 up is the
  20-bit form. 20-bit 0x1C00 is `break 7` (divide by zero) and calls the hook set by slot
  0x1F0; 20-bit 0x1800 is `break 6` (overflow), hook of slot 0x1F8; 20-bit codes 0x400 to
  0x40A are monitor services. GAME.EXE uses two: 0x409 (word 0x0001024D, the stub at
  0x801A355C, called 18 times) installs a handler in the exception table and clears that
  code's bit in the monitor mask; 0x407 (word 0x000101CD, at 0x801447BC, 0x80144814,
  0x801448B4, 0x80151AD0) steps over itself and stops in the monitor (an assert).

GAME.EXE registers its routines in `main` and 0x800C4D58. Confirmed: run 3 logged exactly
these registrations after the entry (the order within each item is the static call order):

- CPU interrupt lines (slot 0x200): line 0, 0x800C6030; line 3, 0x8013F678;
- GT64010 cause bits (slot 0x1E0): bit 4, 0x800C611C and later 0x8014FDF0; bits 1, 2, 3,
  0x800C4CEC, 0x800C4D10, 0x800C4D34;
- FPU exceptions (slot 0x1E8): cause 5 (unimplemented operation), 0x80150860; cause 3
  (divide by zero), 0x8014FEF0;
- break hooks: divide by zero 0x800C4DA0 (slot 0x1F0), overflow 0x800C4E68 (slot 0x1F8);
- exception codes 1 to 8 and 10 to 15 (break 0x409, from 0x800C4A08): 0x800C47E0, a
  catch-all; code 9 (breakpoint) stays with the debug monitor;
- later, at run time, code 13 (trap) twice from 0x801519E0: 0x80151A70, another of the
  "probable callbacks" of `functions.md`.

CPU interrupt-line handlers are called with a0 = the Cause IP bit of their line (0x100 << n);
a1 is not set for them (0x80005100, read; `hardware-usage.md`). GT64010 cause handlers and
exception handlers are called with (Cause, frame), FPU handlers with the frame in a0, and they
use the loader's frame layout: the FPU handler 0x80150860 reads Cause at
frame + 0x180 and EPC at frame + 0x110, and writes saved FPRs from frame + 0x218 (it emulates
the instruction that trapped and advances EPC).

### Callbacks and unresolved jumps seen executing (run 3)

Run 3 let GAME.EXE run for up to 150 emulated seconds from power-on (attract mode, no
input), with breakpoints on the 10 probable callbacks and 16 unresolved indirect jumps of
`functions.md` and on the registered handlers:

- 0x800C6030 executed thousands of times, called from the loader's interrupt dispatcher
  (ra 0x80005170): it is the CPU line-0 interrupt handler.
- 0x8014A930 executed (7 hits).
- 0x80151A70 was registered as the trap handler (above) but was not seen executing.
- The other 7 probable callbacks (0x800C6F28, 0x800FE924, 0x800FEDB0, 0x80101CD8, 0x80102454,
  0x801150C8, 0x801478C0) were not hit.
- Unresolved jumps executed: 0x80164484 and 0x801644EC (from 0x80162F04 and 0x80162F54),
  0x801647D0, 0x80164830, 0x80164938 and 0x80164998 (from 0x80162BF8), all in the block-copy
  family. The other 10 were not hit.
- Handlers executed: 0x8014FDF0 (GT64010 bit 4) and 0x8013F678 (CPU line 3). The others were
  not hit in the run.

Not hit means not reached in that run, not unreachable.

## Code written to RAM

Stores from GAME.EXE's reached code to 0x80000000 to 0x800001FF (or the KSEG1 alias), with
the base built from a constant: none. The only code GAME.EXE writes is `_start`'s copy into the
stub block. Its 20 `cache` instructions are data-cache operations only (Create Dirty Exclusive,
Hit Writeback Invalidate, Hit Writeback) in its memory-fill and flush routines at 0x80155A98,
0x80167D58, 0x80169134, 0x80169280 and 0x80169384; there is no instruction-cache operation.

## Decisions for the runtime (recommendations)

1. **Native stand-ins for the whole slot table, not an interpreted loader.** Every call from
   GAME.EXE into the loader goes through the 29 slots of the stub block (plus break 0x409 and
   0x407, plus the reset jump). The runtime should fill 0x800C408C to 0x800C42CC (end
   exclusive) itself and bind each slot to a native routine: the file calls (open, close, read, write, ioctl, lseek,
   isatty) to the host's game-data files and device emulation; the PCI calls to the Voodoo
   emulation or its replacement; the cache call to a no-op; the disk partition and raw IDE
   calls to a disk image reader; the handler-registration calls to the runtime's own interrupt
   and exception dispatch; slot 0x1A8 to a pointer to 4 zero words. Evidence: the slot list
   above, MAME-confirmed. Sure: high for the mechanism and the call list; medium for the
   meaning of the slots marked unconfirmed, which need reading before their stand-ins are
   written.
2. **Loader file-system semantics need a second look before the stand-ins are written:**
   0x038, 0x190, 0x1B8, 0x1C0 and 0x1D8. Running the loader's code under the interpreter instead
   would need its 89 KB image, its BSS state and the IDE and GT64010 hardware behind it, so it
   is the fallback, not the plan. Sure: medium.
3. **Exceptions and interrupts: native dispatch with the loader's ABI.** GAME.EXE never handles
   Cause or EPC itself and has no `eret`; it installs handlers through the slots and break
   0x409 and expects to be called as the loader calls it: CPU-line handlers with a0 = the
   Cause IP bit, the others with (Cause, frame) on a frame laid out as the loader's (EPC at
   +0x110, FPRs at +0x218). The runtime must keep that frame layout for the FPU
   emulation handler at 0x80150860, or replace that handler. Sure: high for the registration
   path (static, registrations seen in run 3); medium for the frame offsets beyond EPC and the
   FPRs.
4. **Pre-load nothing below 0x800C4000 except what a stand-in needs.** GAME.EXE reads nothing
   below its image directly; its only low-RAM access is `_start`'s copy of the slot table,
   which the runtime replaces. The loader's image and BSS matter only to the loader's own
   routines. If any slot is run under the interpreter instead, the loader image
   (0x80000000 to 0x80015C40, from ROM 0xBFC00F20) and its live BSS state must be loaded too.
   Sure: high (static scan of reached code for constant addresses below 0x800C4000, MAME dump).
5. **Start state:** sp, FCSR and the cleared BSS are set by `_start` before use, so their
   values in `boot_state.toml` matter only for the first instructions; gp is unused. Status is
   not set by `_start`: GAME.EXE's reached code has no `mtc0`, so it runs with the loader's
   Status (0x30010401, FR clear) apart from what loader services change. Sure: high (read,
   `_start` 0x800C4000 to 0x800C4088; static census of reached code in `functions.md`). The loader leaves IE set with IP2 unmasked, so on the board an early IP2
   interrupt would reach the loader's default handler; the runtime should simply not deliver
   interrupts until `main` has registered its handlers. Sure: medium (a design choice, not an
   observation).
6. **Skipping DIAG.EXE loses nothing.** On a normal boot DIAG.EXE does not run (confirmed), so
   GAME.EXE cannot depend on it. On the Test Switch path the loader resets its handler tables
   and takes new argument words on every load, and GAME.EXE ignores its argument block anyway,
   so nothing DIAG.EXE leaves behind reaches GAME.EXE; the image overlap (DIAG at 0x80100000 to
   0x801725CC) is overwritten by GAME.EXE's load and its BSS clear. Anything DIAG.EXE changes
   outside RAM (the NVRAM, the disk) was not traced and is outside this question. Sure: high
   for RAM; DIAG.EXE's own hand-off to GAME.EXE is static only.
7. **The extracted file can be used in full.** Since Task 5b the extractor follows the block
   chain, and the extracted GAME.EXE equals the loaded image in all 918,052 image bytes (see
   the finding above). Sure: high (byte comparison and header checksum).

## Not confirmed

- Config's read-only fields (only K0 = 3 is known).
- The purpose of slots 0x038, 0x110, 0x190, 0x1B8, 0x1C0 and 0x1D8 beyond their shape.
- DIAG.EXE starting GAME.EXE (static only; run 2 stayed in the DIAG menu).
- What the loader does with the argument words (GAME.EXE ignores them; DIAG.EXE's use was not
  read).
- The names of the routines behind slots 0x0D8 to 0x0E8 and the file slots come from reading
  their structure and callers, not from symbols.
- The exception-frame offsets other than Cause (+0x180), EPC (+0x110), the FPRs (+0x218) and
  FCSR (+0x31C).
