# How it works

## The problem

CarnEvil runs on Midway's Seattle arcade board: a 150 MHz MIPS R5000, a 3dfx Voodoo 1 and a DCS sound board. MAME emulates all of it faithfully, but faithfully is expensive. It interprets or JIT-compiles every MIPS instruction as the game runs, and it draws every Voodoo pixel on the CPU. On a slow processor such as the PlayStation 4's 1.6 GHz Jaguar, that is not enough for full speed.

This project takes a different route. The aim is a native program that plays CarnEvil at full speed on modest hardware. The PS4 running Linux is the machine it is aimed at; the first builds are for macOS and then Linux.

## Static recompilation

An emulator translates the game's machine code while the game is running. A static recompiler does the translation once, ahead of time:

1. Read the game's executable (`GAME.EXE`, MIPS machine code).
2. Find every function in it.
3. Write each MIPS function out as an equivalent C function.
4. Compile that C with an ordinary optimising compiler for the machine you want to run on.

The result is the game's own logic, running as native code with no interpreter in the way. The same technique is behind the recent native ports of Nintendo 64 games. The shape of this project's output is the one [N64Recomp](https://github.com/N64Recomp/N64Recomp) uses: one C function per MIPS function, working on a structure that holds the processor's registers, with jumps inside a function as `goto` and a lookup table for calls through pointers. The recompiler itself is new, written in Python on top of this project's own analysis tools, because N64Recomp does not handle the MIPS IV instructions this game uses and assumes a big-endian machine, where the arcade board is little-endian.

The translated C is not a decompilation. It is not meant to be read: every line still talks about MIPS registers. It is a faster way of executing the original program. [The recompiler notes](analysis/recompiler.md) describe it in detail.

## What the translated code needs around it

The game's code expects an arcade board. It writes to hardware registers to draw triangles and talks to a sound board. For files, the disk, settings and most devices it calls a loader that the boot ROM leaves in memory, and that loader also handles the interrupts. A **runtime** stands in for all of that:

- **Memory:** 8 MB of RAM laid out the way the game expects.
- **Graphics:** the game's writes to the Voodoo 1's registers are turned into OpenGL draw calls, so the GPU does the rendering.
- **The loader's services:** files, the disk, settings and device control. The port replaces each of the 29 services the game calls with native code, rather than running the loader on top of an emulated disk controller and system controller; the [M0 report](analysis/M0-report.md) sets out why. Every call into the loader reaches the runtime as one function call naming the service.
- **Inputs:** the light guns, coin and start buttons, fed from a mouse or a game controller.
- **Sound:** the DCS board has its own processor (an ADSP-2115) running its own program. That part is still emulated, on a separate thread. It is cheap.
- **Everything else on the board:** interrupts, the battery-backed settings (which the game reads directly), and three values from the security chip, which the game asks the loader for instead of checking the chip itself.

Every memory access the translated code makes checks whether the address is RAM; anything else goes to the runtime, which is where the graphics chip, the guns, the sound port and the settings memory are modelled.

A small MIPS interpreter is kept as a safety net for any code that was not translated ahead of time. Whatever it has to run is logged, so it can be added to the translated set. Two hand-written functions of the game run in it on purpose: the game switches between its own tasks by swapping stacks, which ordinary translated C cannot follow, and making that work is part of the runtime's job ([M1 report](analysis/M1-report.md)).

## Two phases

**Phase 1: replace the hardware at register level.** The runtime behaves like the chips. This needs no understanding of the game's own code, so it is the shortest path to a working port.

**Phase 2: replace the graphics library at function level.** `GAME.EXE` contains a copy of 3dfx's Glide library. Once the port runs, those Glide routines can be swapped for native OpenGL versions one at a time. That is faster again and makes higher resolutions and widescreen possible.

## Checking the work

MAME is the reference. The same disk image runs in MAME, and the port is compared against it: processor state at agreed checkpoints, the order of the game's debug messages, and rendered frames. The recompiler is checked in three ways: short instruction programs run on MAME's processor emulation, whose results both the translated C and the interpreter must reproduce exactly; the translated C against the interpreter on random inputs; and real game functions captured in MAME during attract mode and replayed through the translated C, which must leave every register and all 8 MB of RAM as MAME did.

## What is in this repository, and what is not

In: the tools that extract and analyse the game's files, the notes on what was found, the recompiler, the reference interpreter and the test runtimes, and later the runtime itself.

Not in: the game. No code, assets, ROMs or disk images are here, and the C generated from the game is never committed either, because it is a translation of the game's code. You bring your own CHD and everything game-specific is produced on your machine. See [Getting started](getting-started.md).
