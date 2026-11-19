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

The loader stays in RAM while the game runs. The game calls it for files, the disk, the settings memory, PCI set-up and device control, and the loader handles all interrupts and exceptions, calling the handlers the game registers with it. [analysis/boot-handoff.md](analysis/boot-handoff.md) has the details.

Both programs are flat MIPS images with an 8-byte header. `GAME.EXE` loads at `0x800C4000` and `DIAG.EXE` at `0x80100000`.

## How the port handles each part

- **Processor:** not emulated. The game's code is translated to C ahead of time. A small interpreter covers anything that was not translated.
- **Memory:** a plain 8 MB block, with the processor's address aliases mapped onto it.
- **Voodoo 1:** in the first phase, register writes are turned into OpenGL draws, with a shader standing in for the Voodoo's colour and alpha combining. In the second phase the game's Glide routines are replaced directly.
- **The loader's services, and the hardware behind them:** the game reaches the disk, the GT64010 (timers, interrupts, PCI configuration, DMA), most of the IOASIC and the settings writes through the boot ROM's loader, not by itself. How the port supplies those services is an open design decision: native replacements for the services, or the loader itself run on top of emulated IDE, GT64010 and IOASIC. The [M0 report](analysis/M0-report.md) sets out the options.
- **IOASIC:** inputs come from a mouse or game controller; the serial port goes to the terminal, which is very useful for debugging.
- **Sound:** the ADSP-2115 is emulated on its own thread, running the same program the real board runs.
- **Settings:** settings live in their own file. The game reads the settings memory directly and writes it through the loader, so the port has to provide both.
- **Security chip:** the game never talks to it. It asks the loader for three values the loader read from the chip at start-up, so the port only has to supply those.

The port skips the boot ROM and the diagnostics program and goes straight to the game. The diagnostics do not run on a normal boot, so the game cannot depend on them; what the boot ROM leaves behind is in [analysis/boot-handoff.md](analysis/boot-handoff.md).
