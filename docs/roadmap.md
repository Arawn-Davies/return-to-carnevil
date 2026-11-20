# Roadmap and status

Last updated: 20 November 2026.

## Where things stand

The first milestone, M0 (tools that extract the game's files and analyse its executable), is complete and summarised in the [M0 report](analysis/M0-report.md). The second, M1 (the recompiler), is complete and summarised in the [M1 report](analysis/M1-report.md). The third, M2 (the runtime), is complete on its branch, pending its final review, and summarised in the [M2 report](analysis/M2-report.md): the game boots headless and runs its attract mode, matching MAME's console output, file activity and tasks. The next step is planning M3, the picture. Nothing is playable yet.

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

**M1: the recompiler** (done)

- Recompiler: done. A Python emitter, built on M0's decoder and function analysis, turns all 2,093 functions of `GAME.EXE` into C. Its output has the shape N64Recomp's has; N64Recomp itself does not handle this game's instructions or byte order.
- Reference interpreter and the contract header the generated C, the interpreter and the runtime share: done.
- Instruction checks against MAME: done. Both the generated C and the interpreter reproduce all 1,352 instruction programs recorded from MAME's processor emulation.
- Whole image: done. `make recomp` writes the C and `make linkcheck` links it against a stub runtime with no undefined symbol, on macOS (Apple clang) and Linux (GCC).
- Real game code against MAME: done. From the entry, the recompiled code reaches the game's first loader call with MAME's registers and RAM, and 127 game functions captured in MAME's attract mode replay with MAME's results.
- M1 report: done.

**M2: boots without graphics** (done on its branch, pending the final review)

- Runtime: done. Native stand-ins for the 29 loader services, the hardware the game drives itself (the Voodoo, the guns, the sound port, the settings memory), interrupt delivery to the handlers the game registers, and the game's cooperative tasks, each on a host thread of its own with only one running at a time. [analysis/runtime.md](analysis/runtime.md) describes it.
- The disk, read-only: the game reads its files from your raw image; its writes go to an overlay file and its settings to an NVRAM file.
- The Voodoo: more than the plan expected. Glide's start-up reads back pixels the card drew, so the port has a software frame buffer and draws the start-up checks' triangles; everything attract mode draws afterwards is counted and dropped until M3.
- The sound board: a responder that gives the replies MAME's sound board gave in attract mode. Nothing is played.
- The program `carnevil`: done. It runs 150 seconds of game time into attract mode on macOS, and on Linux as its implementation task reported (with the same trace as the Mac's; not repeated independently), and its console output, the files it opens, its tasks and its first loader calls match MAME's.
- M2 report: done.

**M3: graphics.** Voodoo 1 register writes become OpenGL draws, building on M2's frame buffer. Attract mode should match MAME frame for frame. This is also where the game gets a window and runs in real time.

**M4: input.** Guns from a mouse or controller, coin and start. A level is playable.

**M5: sound.** The DCS sound board, emulated on its own thread. This completes the first playable build, on macOS: everything from M1 to M5 is developed and tested on a Mac.

**M6: Linux.** The same code built and tested on Linux x86-64, both native and under WSL2 on a Windows PC.

**M7: PlayStation 4.** The PS4 Linux build and a performance pass on its slow processor. Full speed is the pass mark.

**After M7:** replace the game's built-in Glide graphics library with native OpenGL routines, then higher resolutions and widescreen.

## What has been learned so far

The [M0 report](analysis/M0-report.md), the [M1 report](analysis/M1-report.md) and the [M2 report](analysis/M2-report.md) have the full summaries, with the evidence for each point.

About the disk:

- The hard disk image has 17 partitions. Partition 0 holds the 1,523 files the game runs from.
- Each file is a chain of 4 KB blocks. Most files' blocks are adjacent, but three are not, `GAME.EXE` among them, so reading files as one contiguous run (as the public research scripts do) gets those three wrong.
- The format details are in [analysis/disk-format.md](analysis/disk-format.md).

About the executables:

- `GAME.EXE` is 918,060 bytes: an 8-byte header and a flat little-endian MIPS image that loads at `0x800C4000` and ends at `0x801A4224`. Its first instruction sets the stack to the top of the 8 MB of RAM.
- `DIAG.EXE` has the same layout and loads at `0x80100000`.
- The first header word is a checksum (the POSIX `cksum` CRC) of the rest of the file; the boot ROM refuses to start a file that fails it.
- It was built with GCC and contains 3dfx's Glide library, an IDT MIPS runtime and DJ Delorie's C library.

About the boot:

- A normal boot runs the boot ROM's start-up code, its power-on tests, then its loader, which starts `GAME.EXE`. The diagnostics program `DIAG.EXE` runs only when the Test switch is on.
- The loader stays in memory while the game runs. The game uses it for files, the disk, settings writes, PCI set-up, interrupts and exceptions.
- The loader also maps low addresses onto RAM through the processor's TLB, and the game uses them.

About the code:

- The code runs from `0x800C4000` to `0x8016DC4C`, about 695 KB, with data after it.
- There are 2,093 functions and 91 jump tables (16 of them read from the code by hand in M1). 29 of the 2,093 are entries of the block described below, which leaves 2,064 functions of the game's own (3 of them added by hand because MAME showed them running or registered).
- The game runs its own cooperative tasks: a scheduler called once from `main` resumes each task in turn, and a task gives up the processor by calling a hand-written routine that swaps stacks. That routine is called from 70 places.
- Every instruction reached as code decodes cleanly. The MIPS IV features in use are conditional moves and fused multiply-add, plus two extra floating point condition codes in one function; the indexed floating point loads and stores do not occur in reached code.
- A small block near the start of the image is empty in the file and is filled at start-up with a copy of the loader's jump table: 72 slots, each a jump into the loader. The game calls 29 of them, so those 29 services have to be supplied by the port.
- The game's own code contains no exception entry code. The loader's exception vectors and dispatcher run, and call the handlers the game registers with the loader.

About the hardware:

- The game itself drives the Voodoo 1 (registers, frame buffer and texture memory), the light guns and the sound data port, and reads the settings memory directly. Everything else (disk, system controller, the rest of the I/O chip, writes to the settings memory) goes through the loader.
- The game does not talk to the security chip. It asks the loader for three values the loader read from the chip at start-up.
- Most of attract mode's drawing reaches the Voodoo not from the processor but by DMA: on every pass of its scheduler the game hands the system controller a chain of records that copies register writes and movie frames to the card.
- At start-up, Glide tests the card by drawing and reading back pixels: a frame-buffer memory test, a dither calibration from 256 small triangles, and a texture-unit check from 2 more.
- The board's VBLANK interrupt is the Voodoo's vertical retrace, which starts only once the game programs the card's video timing.
- The sound board is talked to through a small protocol: the game resets it, uploads its program and sample banks in checksummed blocks, and asks for its version.

About the tasks:

- The game runs 6 or fewer tasks at once in attract mode, switching about a hundred times a second, and its scheduler is paced by the VBLANK interrupt.

## Target hardware

The builds come in this order: macOS first (the development machine, and the first playable build), then Linux x86-64 including WSL2, then PlayStation 4 Linux. The PS4 is the machine the project exists for: eight 1.6 GHz Jaguar cores and a Radeon GPU with OpenGL through Mesa, too slow to emulate the game at full speed. It comes last because it is the slowest to test on. The code is kept portable throughout.
