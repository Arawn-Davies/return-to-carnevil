# Roadmap and status

Last updated: 19 November 2026.

## Where things stand

The project is in its first milestone, M0: tools that extract the game's files and analyse its executable. Nothing is playable yet.

## Milestones

**M0: extraction and analysis** (in progress)

- Disk tools: done. The raw disk image splits into its 17 partitions and the files extract cleanly.
- EXE loader: done. Both executables load, and unknown revisions are refused.
- MIPS IV decoder and opcode census: done.
- Function discovery: written, under review.
- Boot hand-off analysis: to do. What the boot ROM and the diagnostics program set up before the game starts, confirmed with a MAME trace.
- Hardware usage census: to do. Which hardware registers the game touches, and whether it checks the security chip.
- M0 report: to do.

**M1: the recompiler.** Extend N64Recomp for this target (little-endian, MIPS IV instructions, a flat image instead of an ELF file) until all of `GAME.EXE` comes out as C that compiles and links.

**M2: boots without graphics.** A runtime with memory, interrupts, the system controller, the disk and the serial port: enough for the game to reach its attract loop, judged by its debug messages.

**M3: graphics.** Voodoo 1 register writes become OpenGL draws. Attract mode should match MAME frame for frame.

**M4: input.** Guns from a mouse or controller, coin and start. A level is playable.

**M5: sound.** The DCS sound board, emulated on its own thread. This completes the first playable build, on macOS: everything from M1 to M5 is developed and tested on a Mac.

**M6: Linux.** The same code built and tested on Linux x86-64, both native and under WSL2 on a Windows PC.

**M7: PlayStation 4.** The PS4 Linux build and a performance pass on its slow processor. Full speed is the pass mark.

**After M7:** replace the game's built-in Glide graphics library with native OpenGL routines, then higher resolutions and widescreen.

## What has been learned so far

About the disk:

- The hard disk image has 17 partitions. Partition 0 holds the 1,356 files the game runs from.
- The format details are in [analysis/disk-format.md](analysis/disk-format.md).

About the executables:

- `GAME.EXE` is 918,060 bytes: an 8-byte header and a flat little-endian MIPS image that loads at `0x800C4000` and ends at `0x801A4224`. Its first instruction sets the stack to the top of the 8 MB of RAM.
- `DIAG.EXE` has the same layout and loads at `0x80100000`.
- The first header word is not a simple checksum of the image. What it is should come out of the boot ROM analysis.
- It was built with GCC and contains 3dfx's Glide library, an IDT MIPS runtime and DJ Delorie's C library.

About the code (preliminary, from the function discovery that is still under review):

- The code runs from `0x800C4000` to `0x8016DC4C`, about 695 KB, with data after it.
- There are about 2,100 functions and 75 jump tables.
- Every instruction reached as code decodes cleanly. The MIPS IV features in use include conditional moves, indexed floating point loads and fused multiply-add, all of which the recompiler will need.
- A small block near the start of the image is empty in the file and is filled in at boot with routines copied from the boot ROM. The game calls into it, so those routines have to come from the boot ROM rather than from `GAME.EXE`.
- The game's own code contains no exception handler, which points at the boot ROM handling interrupts.

## Target hardware

The builds come in this order: macOS first (the development machine, and the first playable build), then Linux x86-64 including WSL2, then PlayStation 4 Linux. The PS4 is the machine the project exists for: eight 1.6 GHz Jaguar cores and a Radeon GPU with OpenGL through Mesa, too slow to emulate the game at full speed. It comes last because it is the slowest to test on. The code is kept portable throughout.
