# The arcade hardware

CarnEvil (1998) runs on Midway's Seattle board, which it shares with games such as Wayne Gretzky's 3D Hockey, NFL Blitz and California Speed. MAME's `seattle` driver is the reference for everything on this page.

## What is on the board

- **Processor:** an IDT R5000, a 64-bit MIPS IV chip at 150 MHz, running little-endian. It has a floating point unit, which the game uses heavily.
- **Memory:** 8 MB of RAM.
- **Graphics:** a 3dfx Voodoo 1 with a 2 MB frame buffer and one texture unit with 4 MB of texture memory. The game drives it through 3dfx's Glide library, which is compiled into the game executable. MAME runs the game at 512×384.
- **System controller:** a Galileo GT64010. It provides the PCI bus the Voodoo and disk controller sit on, plus timers, DMA and the interrupt controller.
- **Disk:** an IDE hard disk of about 4.3 GB, holding the game's programs and all of its data.
- **I/O:** Midway's IOASIC handles the guns, buttons and coin inputs, a serial port the game prints debug messages to, and the link to the sound board.
- **Sound:** a DCS2 sound board, built around an Analog Devices ADSP-2115 signal processor with its own small boot ROM. The game uploads the sound system's program and sample banks from the hard disk.
- **Boot ROM:** 512 KB. It holds a small start-up routine and two programs that run from RAM: the power-on tests and a generic Midway loader, which has the disk driver, the file system, the device drivers and the exception handling.
- **Settings:** battery-backed memory for operator settings, audits and gun calibration, and a security chip (a PIC) identifying the game.

## How the game starts

1. The boot ROM's start-up routine runs from reset, sets up memory and runs the power-on tests.
2. It then copies the loader into RAM. The loader starts `GAME.EXE` from the disk. Only when the Test switch is on does it start the diagnostics program `DIAG.EXE` instead, which can start the game in turn.
3. `GAME.EXE` sets its stack at the top of RAM, clears its uninitialised data, copies the loader's jump table into a block of its own, and starts the game.

The loader stays in RAM while the game runs. The game calls it for files, the disk, settings writes, PCI set-up and device control, and the loader handles all interrupts and exceptions, calling the handlers the game registers with it. [analysis/boot-handoff.md](analysis/boot-handoff.md) has the details.

Both programs are flat MIPS images with an 8-byte header. `GAME.EXE` loads at `0x800C4000` and `DIAG.EXE` at `0x80100000`.

## How the port handles each part

- **Processor:** not emulated. The game's code is translated to C ahead of time. A small interpreter covers anything that was not translated.
- **Memory:** a plain 8 MB block, with the processor's address aliases mapped onto it: the cached and uncached windows, and the low addresses the loader maps onto RAM through the processor's TLB ([analysis/boot-handoff.md](analysis/boot-handoff.md), "kuseg is mapped onto RAM"). Every other address is hardware and goes to the runtime's model of it.
- **Voodoo 1:** in the first phase, register writes are turned into OpenGL draws, with a shader standing in for the Voodoo's colour and alpha combining. In the second phase the game's Glide routines are replaced directly. Until the first phase (milestone M3), the runtime models the card in software as far as the game's start-up needs: its registers, status and command queues, its frame buffer with the card's memory layout, and the few kinds of triangle Glide's start-up checks draw and read back. Everything else is counted and dropped. The card's vertical retrace is also the board's VBLANK interrupt, so the model keeps the card's video timing as well.
- **The loader's services, and the hardware behind them:** the game reaches the disk, the GT64010 (timers, interrupts, PCI configuration, DMA), most of the IOASIC and the settings writes through the boot ROM's loader, not by itself. The port replaces the 29 services the game calls with native code, so the disk controller and most of the system controller and I/O chip need no register-level model; running the loader itself on top of emulated IDE, GT64010 and IOASIC was the alternative. The [M0 report](analysis/M0-report.md) sets out the options and why. Behind those services the runtime models only what the game reaches through them: the GT64010's timers 1 to 3 and its DMA channel 0, which the game uses on every pass of its scheduler to copy register writes and movie frames to the Voodoo. Disk reads come from your raw image, which is never written; the game's disk writes go to an overlay file.
- **IOASIC:** inputs come from a mouse or game controller (for now they read as "nothing pressed"); the game's console output, which the board sends to its serial port, goes to the terminal, which is very useful for debugging.
- **Sound:** the ADSP-2115 is emulated on its own thread, running the same program the real board runs (milestone M5). Until then a stand-in answers the game's sound-board protocol (reset, the program and sample uploads with their checksums, the version request) as the board did in MAME; nothing is played.
- **Settings:** settings live in their own file, the NVRAM file, created blank on the first run. The game reads the settings memory directly and writes it through the loader, so the port provides both.
- **Security chip:** the game never talks to it. It asks the loader for three values the loader read from the chip at start-up, so the port only has to supply those.

[analysis/runtime.md](analysis/runtime.md) describes each of these models in detail.

The port skips the boot ROM and the diagnostics program and goes straight to the game. The diagnostics do not run on a normal boot, so the game cannot depend on them; what the boot ROM leaves behind is in [analysis/boot-handoff.md](analysis/boot-handoff.md).
