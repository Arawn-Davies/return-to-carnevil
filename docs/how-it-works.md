# How it works

## The problem

CarnEvil runs on Midway's Seattle arcade board: a 150 MHz MIPS R5000, a 3dfx Voodoo 1 and a DCS sound board. MAME emulates all of it faithfully, but faithfully is expensive. It interprets or JIT-compiles every MIPS instruction as the game runs, and it draws every Voodoo pixel on the CPU. On a slow processor such as the PlayStation 4's 1.6 GHz Jaguar, that is not enough for full speed.

This project takes a different route. The aim is a native program that plays CarnEvil at full speed on modest hardware, starting with PS4 Linux.

## Static recompilation

An emulator translates the game's machine code while the game is running. A static recompiler does the translation once, ahead of time:

1. Read the game's executable (`GAME.EXE`, MIPS machine code).
2. Find every function in it.
3. Write each MIPS function out as an equivalent C function.
4. Compile that C with an ordinary optimising compiler for the machine you want to run on.

The result is the game's own logic, running as native code with no interpreter in the way. The same technique is behind the recent native ports of Nintendo 64 games (N64Recomp), and this project builds on that tool.

The translated C is not a decompilation. It is not meant to be read: every line still talks about MIPS registers. It is a faster way of executing the original program.

## What the translated code needs around it

The game's code expects an arcade board. It writes to hardware registers to draw triangles, reads a disk controller to load files, and talks to a sound board. A **runtime** stands in for all of that:

- **Memory:** 8 MB of RAM laid out the way the game expects.
- **Graphics:** the game's writes to the Voodoo 1's registers are turned into OpenGL draw calls, so the GPU does the rendering.
- **Disk:** an IDE controller backed by the raw disk image, so the game's own file system code keeps working unchanged.
- **Inputs:** the light guns, coin and start buttons, fed from a mouse or a game controller.
- **Sound:** the DCS board has its own processor (an ADSP-2115) running its own program. That part is still emulated, on a separate thread. It is cheap.
- **Everything else on the board:** the system controller (timers, interrupts), battery-backed settings, and the security chip if the game checks it.

A small MIPS interpreter is kept as a safety net for any code that was not translated ahead of time, such as routines in the boot ROM. Whatever it has to run is logged, so it can be added to the translated set.

## Two phases

**Phase 1: replace the hardware at register level.** The runtime behaves like the chips. This needs no understanding of the game's own code, so it is the shortest path to a working port.

**Phase 2: replace the graphics library at function level.** `GAME.EXE` contains a copy of 3dfx's Glide library. Once the port runs, those Glide routines can be swapped for native OpenGL versions one at a time. That is faster again and makes higher resolutions and widescreen possible.

## Checking the work

MAME is the reference. The same disk image runs in MAME, and the port is compared against it: processor state at agreed checkpoints, the order of the game's debug messages, and rendered frames. The recompiler itself is tested instruction by instruction against a simple reference interpreter.

## What is in this repository, and what is not

In: the tools that extract and analyse the game's files, the notes on what was found, and later the recompiler and the runtime.

Not in: the game. No code, assets, ROMs or disk images are here, and the C generated from the game is never committed either, because it is a translation of the game's code. You bring your own CHD and everything game-specific is produced on your machine. See [Getting started](getting-started.md).
