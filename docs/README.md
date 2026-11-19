# Documentation

Start here.

## For people

- [How it works](how-it-works.md): what static recompilation is and how this port is put together.
- [Getting started](getting-started.md): going from your own CHD to a working folder, and running the tools and tests.
- [Tools reference](tools.md): every command in `carnevil_tools`, what it reads and what it writes.
- [The arcade hardware](hardware.md): what is on the Seattle board and how the port deals with each part.
- [Roadmap and status](roadmap.md): the milestones, what is done, and what has been learned so far.

## Analysis notes

Findings about the game's files and binary, written as the work goes. They hold layouts, addresses and counts, never code listings.

- [M0 report](analysis/M0-report.md): the summary of milestone M0, what it changed in the design, and the decisions it leaves for M1 and M2. Read this first.
- [Disk image format](analysis/disk-format.md): the partition table and the file system inside each partition.
- [EXE format](analysis/exe-format.md): the 8-byte header and the flat image behind it.
- [Opcode census](analysis/opcode-census.md): which MIPS instructions appear in `GAME.EXE`.
- [Function discovery](analysis/functions.md): the functions, jump tables and the code/data boundary.
- [Boot hand-off](analysis/boot-handoff.md): what runs before `GAME.EXE`, the state it starts in, and the boot ROM loader services it calls.
- [Hardware usage](analysis/hardware-usage.md): which hardware the game and the loader touch, measured statically and in MAME.

## Working documents

These were written to drive the implementation with AI coding agents. They are precise and useful, but they are task instructions rather than explanations.

- [Design](superpowers/specs/2026-11-19-carnevil-recomp-design.md): goals, constraints, the approaches considered and the chosen design.
- [Plan](superpowers/plans/2026-11-19-carnevil-recomp.md): the task-by-task plan for milestone M0 and outlines for M1 to M7.
