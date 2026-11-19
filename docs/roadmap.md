# Roadmap and status

Last updated: 19 November 2026.

## Where things stand

The first milestone, M0 (tools that extract the game's files and analyse its executable), has its analysis complete and summarised in the [M0 report](analysis/M0-report.md). The next step is planning M1. Nothing is playable yet.

## Milestones

**M0: extraction and analysis** (analysis done)

- Disk tools: done. The raw disk image splits into its 17 partitions and the files extract by following each file's chain of blocks.
- EXE loader: done. Both executables load; a damaged or mis-extracted file and an unknown revision are refused with different errors.
- MIPS IV decoder and opcode census: done.
- Function discovery: done.
- Boot hand-off analysis: done, confirmed with MAME traces.
- Extractor fix: done. An extra task, added when the boot hand-off analysis showed that the first extractor read three files wrongly, `GAME.EXE` among them.
- Hardware usage census: done, checked against MAME memory taps.
- M0 report: done. Still open: the MAME frame rate on the PS4, to be recorded the next time the PS4 is up.

**M1: the recompiler.** Extend N64Recomp for this target (little-endian, MIPS IV instructions, a flat image instead of an ELF file) until all of `GAME.EXE` comes out as C that compiles and links.

**M2: boots without graphics.** A runtime with memory, interrupts, the system controller, the disk and the serial port: enough for the game to reach its attract loop, judged by its debug messages. The game reaches the disk and most devices through the boot ROM's loader, so how the port supplies the loader's services is a decision to take first (see the M0 report).

**M3: graphics.** Voodoo 1 register writes become OpenGL draws. Attract mode should match MAME frame for frame.

**M4: input.** Guns from a mouse or controller, coin and start. A level is playable.

**M5: sound.** The DCS sound board, emulated on its own thread. This completes the first playable build, on macOS: everything from M1 to M5 is developed and tested on a Mac.

**M6: Linux.** The same code built and tested on Linux x86-64, both native and under WSL2 on a Windows PC.

**M7: PlayStation 4.** The PS4 Linux build and a performance pass on its slow processor. Full speed is the pass mark.

**After M7:** replace the game's built-in Glide graphics library with native OpenGL routines, then higher resolutions and widescreen.

## What has been learned so far

The [M0 report](analysis/M0-report.md) has the full summary, with the evidence for each point.

About the disk:

- The hard disk image has 17 partitions. Partition 0 holds the 1,356 files the game runs from.
- Each file is a chain of 4 KB blocks. Most files' blocks are adjacent, but three are not, `GAME.EXE` among them, so reading files as one contiguous run (as the public research scripts do) gets those three wrong.
- The format details are in [analysis/disk-format.md](analysis/disk-format.md).

About the executables:

- `GAME.EXE` is 918,060 bytes: an 8-byte header and a flat little-endian MIPS image that loads at `0x800C4000` and ends at `0x801A4224`. Its first instruction sets the stack to the top of the 8 MB of RAM.
- `DIAG.EXE` has the same layout and loads at `0x80100000`.
- The first header word is a checksum (the POSIX `cksum` CRC) of the rest of the file; the boot ROM refuses to start a file that fails it.
- It was built with GCC and contains 3dfx's Glide library, an IDT MIPS runtime and DJ Delorie's C library.

About the boot:

- A normal boot runs the boot ROM's start-up code, its power-on tests, then its loader, which starts `GAME.EXE`. The diagnostics program `DIAG.EXE` runs only when the Test switch is on.
- The loader stays in memory while the game runs. The game uses it for files, the disk, the settings memory, PCI set-up, interrupts and exceptions.

About the code:

- The code runs from `0x800C4000` to `0x8016DC4C`, about 695 KB, with data after it.
- There are 2,093 functions and 75 jump tables. 29 of the 2,093 are entries of the block described below, which leaves 2,064 functions of the game's own (3 of them added by hand because MAME showed them running or registered).
- Every instruction reached as code decodes cleanly. The MIPS IV features in use are conditional moves and fused multiply-add, plus two extra floating point condition codes in one function; the indexed floating point loads and stores do not occur in reached code.
- A small block near the start of the image is empty in the file and is filled at start-up with a copy of the loader's jump table: 72 slots, each a jump into the loader. The game calls 29 of them, so those 29 services have to be supplied by the port.
- The game's own code contains no exception entry code. The loader's exception vectors and dispatcher run, and call the handlers the game registers with the loader.

About the hardware:

- The game itself drives the Voodoo 1 (registers, frame buffer and texture memory), the light guns and the sound data port, and reads the settings memory directly. Everything else (disk, system controller, the rest of the I/O chip, writes to the settings memory) goes through the loader.
- The game does not talk to the security chip. It asks the loader for three values the loader read from the chip at start-up.

## Target hardware

The builds come in this order: macOS first (the development machine, and the first playable build), then Linux x86-64 including WSL2, then PlayStation 4 Linux. The PS4 is the machine the project exists for: eight 1.6 GHz Jaguar cores and a Radeon GPU with OpenGL through Mesa, too slow to emulate the game at full speed. It comes last because it is the slowest to test on. The code is kept portable throughout.
