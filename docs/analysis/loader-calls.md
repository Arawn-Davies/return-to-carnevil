# Loader calls in order

What GAME.EXE asks the loader for at run time, in the order it asks, for M2's stand-ins and
its boot test. The slot table, the routine behind each slot and the constant ioctl commands at
the 160 call sites are already in `boot-handoff.md` ("What `_start` copies") and
`hardware-usage.md` ("What the game asks the loader's drivers for"); the first call is in
`boot-handoff.md` ("The first loader call"). This page adds what only a run shows: the order,
the arguments and results, the counts, the files opened, and the console output with its
timing. Each statement is marked **measured** (MAME), **read** (disassembly) or **inferred**.

## Evidence

- MAME 0.276 in the Linux container, `carnevil` set and CHD, blank NVRAM, no input (attract mode), `-nodrc`
  with the debugger on, `tools/mame/loadercalls.lua` (command line, outputs and line formats
  in `tools/mame/README.md`). 150 emulated seconds from GAME.EXE's entry (5.004 s after
  power-on) to 155.018 s; 8,408 frames at 54.15 frames per emulated second.
- Every one of the 72 stub-block slots had a breakpoint, so every call into the loader through
  the block is counted. Calls into the loader by other means (break 0x409 and 0x407, the
  exception vectors) are not counted here (`task-switch.md` counts exceptions).
- Outputs on the share, `mame/m2-measure/loader-A/`:
  - `loader-calls-first.txt`: the first 400 calls in order, with slot, a0-a3, ra, sp, v0 and
    v1 at the return, the string at a0 where there is one, and the bytes of each write to fd
    0-2;
  - `report.txt`: the summary below in full (per-slot and per-command counts, hottest ioctl
    call sites, every open and file-system call with its path and result, the console lines);
  - `console.txt`: the console (UART) output, one line each with frame, emulated time and the
    writing pc.
- Not run: the same in play (run B of `task-switch.md`). The attract run already takes 25
  minutes and 670 MB of log, almost all of it the boot's sound download; play would add the
  in-game file loads, which the attract run's later file loads show the shape of.

## The first 400 calls (measured)

They cover the entry (5.18 s) to 9.33 s, frames 295 to 523; 172 are ioctl, 89 read, 72
write, then 19 PCI configuration writes, 16 PCI device set-ups, 5 GT64010 handler
registrations, 5 PCI configuration reads, 4 PCI vendor probes, 4 opens, 3 closes, 2 CPU-line
and 2 FPU handler registrations, 2 calls of slot 0x038, one each of 0x1A8, 0x118, 0x0E8, the
two break hooks. In order:

- n 1, slot 0x038 with `game.env` (from 0x80145348, in `_start`'s second call; the M1 first
  call): returns 0xFFFFFFFF. Later calls of slot 0x038 with existing file names
  (`FONT0001.WMS`, `f_osys.bin`, the `.BNK` sound banks, the `.WMS` files) return 0, each
  followed by an `open` of the same name, so 0x038 is an existence or status check (inferred
  from the results).
- n 2, slot 0x1A8 (argument block): returns 0x800B6E30.
- n 3, `open("pci:", 2, 6)`: fd 6.
- n 4 to 13, 19, 20, 65 and 79: `write(2, ...)` of the banner and status lines (the console
  section below). The console lines after these go to fd 1 (11 writes, from n 808 on).
- n 14 to 18: IOASIC ioctls on fd 4 (0x6902, 0x6903, 0x6901, 0x6900).
- n 21 to 64: Glide's PCI work: vendor probe 0x108 (device 0x121A, a3 6 to 9; the fourth
  finds it), device set-up 0x110 (returns 0xA8000000), configuration reads 0x0F0 and writes
  0x0F8.
- n 66 to 78: `main`'s handler registrations, as `boot-handoff.md` lists them (CPU line 0
  0x800C6030 and line 3 0x8013F678, GT64010 bits, FPU causes 5 and 3, the break hooks), with
  ioctls 0x6910 and 0x690F on fd 4 between them.
- n 80 to 83 (9.297 s): the first ioctls from an interrupt handler: 0x6900, 0x6901, 0x6903,
  0x6902 on fd 4 from 0x80146DD8 to 0x80146E48 with sp on the loader's exception stack
  (0x800C1368). From then on this happens once a frame (the switch scan of the VBLANK
  handler; `task-switch.md`).
- n 84 to 94: the first scheduler pass's work and the interrupts around it: cache writeback
  0x0E8; `ioctl(5, 0x7403, 0)` from 0x8014FE24, which the per-pass routine 0x80168224 calls at
  0x801682E0 (sp on `main`'s stack); four `ioctl(6, 0x7301)` (`sysctrl`, from 0x80168224);
  select disk partition 0x118; then `ioctl(5, 0x740B)`, `0x7403` and `0x740B` on the exception
  stack (0x740B from 0x8014FE54, which the GT64010 bit-4 handler 0x8014FDF0 calls at
  0x8014FDF8; read).
- n 95 to 102: `FONT0001.WMS`: 0x038, `open` (fd 7), one `read` of 0x2AAD4 bytes, `close`.
- n 103 to 107: `open("pic:", 0, 0)` (fd 7), ioctls 0x7000, 0x7001, 0x7002, `close`.
- n 108 to 113: `open("audits.fmt", 1, 0x905)` (fd 7), writes of 0, 0x2000, 0x2000 and 0x84
  bytes, `close`: the game writes a file on the disk (the open mode 1 with 0x905 is the
  write-and-create form; inferred from the writes).
- n 114 to 400: the CMOS: `ioctl(3, 0x6301)` and `0x6302` (base and size), then pairs of
  `ioctl(3, 0x6300, address)` and a 4-byte `read` or `write` on fd 3, the audit and
  adjustment checks.

## Counts over the whole run (measured)

All slots, 5,939,280 calls in 150 emulated seconds:

- ioctl 0x0C8: 5,921,303
- read 0x090: 3,121
- disk call with timeout 0x1D8: 3,123; partition table 0x128: 3,122; raw IDE transfer 0x158:
  3,122 (about one of each per scheduler pass after the boot: the task `loadfram` streaming
  from the disk, inferred from the per-pass rate)
- data cache writeback 0x0E8: 3,580 (one per scheduler pass)
- write 0x0C0: 1,309 (fd 1: 11, fd 2: 14, fd 3: 1,074, fd 7: 210)
- close 0x018: 192; open 0x088: 193; file-system call 0x038: 132; file-system call 0x190: 20
- PCI configuration write 0x0F8: 19; PCI device set-up 0x110: 16; PCI configuration read
  0x0F0: 5; PCI vendor probe 0x108: 4
- GT64010 handler 0x1E0: 5; select disk partition 0x118: 3; `install_disk_info` 0x220: 3;
  CPU-line handler 0x200: 2; FPU handler 0x1E8: 2
- one each: argument block 0x1A8, isatty 0x0E0 (fd 1), break 7 hook 0x1F0, break 6 hook 0x1F8
- never called in the run: 0x028 (open for writing), 0x0D8 (reset handler tables), 0x198
  (load and run an EXE), 0x1B8, 0x1C0. All 72 slots had a breakpoint; no slot outside the 29
  of `boot-handoff.md` was called.

The rate is anything but even (measured, calls per 10 emulated seconds from power-on): 5,757
for 0 to 10 s, 5,855,103 for 10 to 20 s, then 5,576 to 5,968 for every 10 s up to 130 s (about
600 a second, 11 a frame). The 10 to 20 s burst is the sound-data download: ioctl 0x6908
(1,230,931) and 0x6909 (1,679,830) on fd 4 with 0x7401 and 0x7409 (1,230,978 each) and 0x7405
(448,852) on fd 5, all from 0x8016A47C to 0x8016A7A4, one word to the sound board per few
calls (`main_init_sound(): Loading banks...` on the console, 10.24 s to 10.64 s and again
12.39 s to 12.79 s).

ioctl by descriptor and command over the run:

- fd 3 (`cmos:`): 0x6300 3,579; 0x6301 1; 0x6302 1
- fd 4 (`ioasic:`): 0x6900 7,889; 0x6901 7,888; 0x6902 11,469; 0x6903 11,468; 0x6906 10;
  0x6907 10; 0x6908 1,230,931; 0x6909 1,679,830; 0x690A 112; 0x690E 2; 0x690F 8; 0x6910 8;
  0x6911 60
- fd 5 (`timer:`): 0x7401 1,230,978; 0x7402 16; 0x7403 7,160; 0x7405 448,852; 0x7406 28,554;
  0x7409 1,230,978; 0x740A 16; 0x740B 7,160
- fd 6 (`pci:`, the `sysctrl` driver): 0x7301 14,320 (four per scheduler pass, from
  0x80168300 to 0x80168368)
- fd 7 (`pic:`): 0x7000, 0x7001, 0x7002 once each

Steady-state sources (measured, from the call sites): ioctl 0x6900 to 0x6903 from 0x80146DD8
to 0x80146E48 once a frame (7,885 each), in the VBLANK interrupt handler; 0x6902 and 0x6903
also from 0x80147750 and 0x8014777C once per pass (3,580); 0x7403 from 0x8014FE3C (in
0x8014FE24) and 0x740B from 0x8014FE6C (in 0x8014FE54), 7,158 each, two per scheduler pass.
0x8014FE24 is called from the per-pass routine 0x80168224 and from 0x8015EECC and 0x8015EF40;
0x8014FE54 from the GT64010 bit-4 handler 0x8014FDF0 and from 0x8015EF28, 0x8015EF38 and
0x8015EF50 (read). Some of these calls run inside interrupts (below); which share is not
counted (the later calls were logged without sp). 0x7406 from 0x8016AA30 (28,554).

**Loader services are called from interrupt handlers** (measured): of the first 400 calls, 11
were made with sp on the loader's exception stack (0x800C1320 to 0x800C1368): the 8 fd 4 ioctls
of the VBLANK handler's switch scan and 3 `timer:` ioctls (one 0x7403, two 0x740B) from the
0x8014FE24 / 0x8014FE54 pair. So the ioctl stand-in, at least, must work when called from the
runtime's interrupt dispatch.

## Files the game opens (measured)

In order of first use in the run, with `open`'s mode arguments (a1, a2) and the fd returned:
`pci:` (2, 6) fd 6; `FONT0001.WMS` (0, 4) fd 7; `pic:` (0, 0) fd 7; `audits.fmt`, `adjust.fmt`
and `ADJUST.FMT` (1, 0x905) fd 7; for each of `HSTD01.WMS` to `HSTD10.WMS`, twice: slot 0x190
on the name (returns 0; it is always followed by a fresh write of the same file, so it is
probably a delete; inferred), `open("HSTDDFnn.WMS", 0, 4)` and `open("HSTDnn.WMS", 1, 0x905)`
(the console says `reseting high scores` twice: the defaults are copied over the tables);
then the sound files `f_osys.bin`, `f_comp.bin`, `f_dm_ext.bin`, `f_dm_int.bin`,
`generic1.BNK`, `wavinmus.BNK`, `girl.BNK`, then the attract mode's `.WMS` graphics files, each
checked with 0x038 before its `open`. Every open returned fd 7 (one file open at a time) but
the first two. `report.txt` lists all 193 opens and 152 file-system calls with time, frame,
call site and result.

## Console output (measured)

The UART (IOASIC UARTOUT, physical 0x16000028) took 1,033 bytes in the whole run, from
power-on, all written by the loader's `tty` routine (pc 0x8000F588); the same count as the
UARTOUT writes of the hardware tap run (`hardware-usage.md`). The lines, with frame and
emulated seconds since power-on of each line's first byte (blank lines omitted; the M0
reports did not save this):

- f 277, 4.868 s: `Phoenix Seattle R5000 BIOS for Carnevil - $Revision: 9 $`
- f 277, 4.868 s: `Copyright (c) 1997,1998 by Midway Video Inc.`
- f 277, 4.868 s: `Booting game.exe`
- f 295, 5.192 s: `Mother GOOSE - R5000` (centred), then `$Revision: 8 $`, then the five
  copyright lines (`Copyright (c) 1997 by Midway Games Inc.`, `Portions Copyright (c) 1994 by
  DJ Delorie.`, `... 1996 by 3DFx Interactive Inc.`, `... 1996 by Integrated Device
  Technologies Inc.`, `... 1983 by Regents of the University of California.`), all at 5.192 s
- f 295, 5.193 s: `Setting 400x256 54Hz screen resolution` (`Setting ` and `400x256 54Hz`
  written at 5.1927 s, ` screen resolution` at 9.290 s, after Glide's set-up)
- f 521, 9.290 s: `Starting process dispatcher`
- f 524, 9.346 s: `Errors in check_adjustments 1`
- f 525, 9.369 s: `reseting high scores`
- f 546, 9.753 s: `reseting high scores`
- f 572, 10.238 s: `main_init_sound: (information) requesting version number`
- f 573, 10.244 s: `main_init_sound(): Loading banks...`
- f 594, 10.641 s: `main_init_sound(): done`
- f 620, 11.126 s: `NSS reset = e`
- f 620, 11.126 s: `invalid calib, got 9999 (0x0000270F), wanted 37373737`
- f 689, 12.388 s: `main_init_sound: (information) requesting version number`
- f 689, 12.393 s: `main_init_sound(): Loading banks...`
- f 711, 12.790 s: `main_init_sound(): done`

Nothing more was printed up to 155 s. The game's lines up to `Starting process dispatcher` are
`write(2, ...)` calls, the later ones `write(1, ...)` calls, all from 0x8014565C. Those 25
writes total 902 bytes, exactly the UART bytes after the entry (131 came before it, from the
loader's own boot), and the 902 UARTOUT writes after the hand-off of the split tap run
(`hardware-usage.md`). `Errors in check_adjustments 1`, the high-score resets and `invalid
calib` are what a blank NVRAM gives (inferred; the CMOS checks fail and the defaults are
written). The two `main_init_sound` blocks match the two `loadwave` task starts
(`task-switch.md`).

For the port's boot test the milestones, in order, are the banner (`Mother GOOSE`), `Setting
400x256 54Hz screen resolution`, `Starting process dispatcher` (the scheduler's start, frame
521), the sound initialisation and the two blocks of `main_init_sound`.

## Not measured

- Play (run B): not run with this script.
- v0/v1 of calls after the first 400, except the opens, file-system calls and writes.
- sp of calls after the first 400, so the number of loader calls from interrupt context over
  the whole run is not counted; the call sites named above are those seen on the exception
  stack in the first 400 and their counts over the run.
