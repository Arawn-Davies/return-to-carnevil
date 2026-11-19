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
- **Boot ROM:** 512 KB. It is a generic Midway loader: it starts the diagnostics program from the disk, then the game.
- **Settings:** battery-backed memory for operator settings, audits and gun calibration, and a security chip (a PIC) identifying the game.

## How the game starts

1. The boot ROM runs from reset and loads `DIAG.EXE` from the disk. The diagnostics check the hardware and the files' checksums.
2. Control returns to the boot ROM, which loads `GAME.EXE` and jumps to it.
3. `GAME.EXE` sets its stack at the top of RAM, clears its uninitialised data, and starts the game.

Both programs are flat MIPS images with an 8-byte header. `GAME.EXE` loads at `0x800C4000` and `DIAG.EXE` at `0x80100000`.

## How the port handles each part

- **Processor:** not emulated. The game's code is translated to C ahead of time. A small interpreter covers anything that was not translated.
- **Memory:** a plain 8 MB block, with the processor's address aliases mapped onto it.
- **Voodoo 1:** in the first phase, register writes are turned into OpenGL draws, with a shader standing in for the Voodoo's colour and alpha combining. In the second phase the game's Glide routines are replaced directly.
- **GT64010:** timers, interrupts, PCI configuration and DMA are implemented as far as the game uses them.
- **Disk:** an IDE controller backed by the raw disk image, read-only, with the game's writes (audits, settings) going to a separate overlay file.
- **IOASIC:** inputs come from a mouse or game controller; the serial port goes to the terminal, which is very useful for debugging.
- **Sound:** the ADSP-2115 is emulated on its own thread, running the same program the real board runs.
- **Settings and security chip:** settings live in their own file. Whether the security chip needs to be implemented depends on whether the game checks it, which is one of the things the analysis milestone answers.

The port skips the diagnostics program and goes straight to the game. Part of the analysis is working out what state the diagnostics and the boot ROM leave behind that the game relies on.
