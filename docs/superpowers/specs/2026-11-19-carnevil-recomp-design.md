# CarnEvil native port via static recompilation: design

Date: 2026-11-19. Status: draft for approval.

## Intent

Play Midway's CarnEvil (1998) at full speed on the jailbroken PS4 (CachyOS Linux, 8x Jaguar 1.6 GHz, Liverpool GCN 1.1, OpenGL via radeonsi) as a **native program**, not inside MAME. MAME's `seattle` driver runs it below full speed on the PS4: the MIPS core and the software-rendered Voodoo 1 both need more than a 1.6 GHz Jaguar core can give.

The way there: translate the game's own MIPS code (`GAME.EXE`) to C once, ahead of time ("static recompilation"), compile it for x86-64, and link it against a runtime that stands in for the arcade hardware. Rendering goes to the PS4's GPU.

## Goals

- `GAME.EXE` from the user's `carnevil.chd` runs natively on PS4 Linux at full speed (60 fps where the original was 60) with correct graphics, DCS audio and light-gun-style aiming.
- Playable with a DS4 (and a mouse), launched from RetroArch-free plain Linux (a desktop entry / XMB playlist item is fine later).
- Reproducible build from the user's own disk image: the repo holds tools and runtime only.

## Non-goals

- DIAG.EXE (operator diagnostics) and the boot ROM's own menus: the port boots straight into `GAME.EXE`.
- Other Midway Seattle/Vegas games (the runtime may happen to fit them; not a goal).
- Multi-cabinet linking, coin/ticket hardware, operator audits beyond what the game needs to run.
- Windows/macOS releases. (The runtime stays portable C/C++ so it can be developed and tested off the PS4.)

## Anti-goals (failure even if it "works")

- Any game data or recompiled game code committed to the repo or published anywhere. Game files and the generated C (which is a translation of copyrighted code) live only on the user's share.
- Slower than MAME on the PS4.
- A port that only works with hand-patched game binaries instead of the user's original dump.

## Facts established (recon 2026-11-19)

- Source data: `Consoles/ROMs/mame/carnevil/` on the Gaming share : `carnevil.chd` (4,310,046,720-byte disk, data SHA1 `ba32b7ef…`), `carnevi1.chd` (other revision), `boot.u32` / `carnevil1_9.u32` (boot ROM), `sound102.u95` (DCS ADSP-2115 boot ROM), `486_carnevil.u96`.
- Extraction works with xclusivor/CarnEvil-Research scripts: CHD -> raw -> 17 partitions -> part0 has 1,356 files, including `GAME.EXE` (918,060 bytes) and `DIAG.EXE` (468,436 bytes), DCS `.BIN`/`.BNK`, `WMS` textures, `ZM`/`ZA` models. Work tree: `/share/Gaming/Research/carnevil/work/` (the Linux container `/mnt/gaming/Research/carnevil/work/`).
- `GAME.EXE` is a flat little-endian MIPS IV binary (no ELF/COFF header at offset 0). ~2,322 `jr ra`, ~1,862 `addiu sp,sp,-n` prologues, 1,317 distinct `jal` targets: on the order of 2,000 functions. Data references cluster at 0x8017xxxx–0x8023xxxx (KSEG0).
- Built with GCC; statically links the 3dfx Glide library ("Portions Copyright (c) 1996 by 3DFx Interactive"), IDT MIPS runtime and DJ Delorie's libc. Debug strings exist (`graphics_init()`, `main_init_sound`, `grab_save_screen()`).
- Boot chain (per the research repo): boot ROM -> DIAG.EXE -> boot ROM -> GAME.EXE.
- Hardware (MAME `seattle.cpp`): IDT R5000 @150 MHz little-endian, 8 MB RAM, Galileo GT64010 (PCI, DMA, timers, interrupt controller), 3dfx Voodoo 1 (2 MB FB, 1 TMU 4 MB), Midway IOASIC (inputs, DCS comms, UART), IDE disk, DCS2 audio on an ADSP-2115.

## Approaches considered

- **A. Static recompile + register-level hardware runtime (chosen).** Recompile `GAME.EXE` to C; the runtime implements the hardware at its register interface (Voodoo 1 registers translated to OpenGL draws, GT64010, IOASIC, IDE, DCS via an ADSP-2115 interpreter). Boots without first understanding the game's code; removes both MAME bottlenecks.
- **B. Same, with Glide replaced at the function level (Glide HLE).** Find the statically linked Glide routines in `GAME.EXE` and replace them with native OpenGL implementations. Faster and enables widescreen/high resolution, but needs those routines identified before anything shows. **Planned as phase 2 on top of A**, function by function.
- **C. Speed up MAME's driver (GPU Voodoo, DRC tuning).** Not a native port; MAME's architecture makes GPU Voodoo very hard. Dropped, except that MAME is used as the reference oracle.

The user chose A followed by B.

## Design

### 1. Data pipeline (tools/extract)

- Python tools (adapted from CarnEvil-Research, MIT-compatible re-implementation, credited): CHD (via `chdman extractraw`) -> partitions -> files, plus the raw disk image kept for the runtime's IDE emulation.
- Records SHA-256 of `GAME.EXE`; the recompiler refuses unknown hashes (one supported revision first: `carnevil.chd`).

### 2. Binary analysis (tools/analyze)

- Determine the load address and entry point of `GAME.EXE` by disassembling the boot ROM's loader (and confirm with a MAME debugger trace: PC at the jump into GAME.EXE, plus the CPU state the boot ROM leaves: Status/Config, stack pointer, gp).
- Function discovery: `jal` targets, prologue scan, code addresses found in data (function pointers, vtables, interrupt handlers), and jump tables (`lw` from table + `jr`). Output a function list/TOML the recompiler consumes, with manual overrides.
- Symbol hints: string cross-references (debug messages name their functions), Glide signature matching for phase 2.

### 3. Recompiler (recomp/)

- Base on N64Recomp (MIPS III to C, MIT licence), extended for this target:
  - **little-endian** memory access (N64 is big-endian), so loads/stores map straight to host memory;
  - **MIPS IV** additions used by R5000 GCC code: `movn`/`movz`, `movf`/`movt`, FP condition codes 0–7, `lwxc1`/`ldxc1`/`swxc1`/`sdxc1`, `madd.s`/`msub.s` and friends, `prefetch`/`pref` (no-op);
  - FPU register mode (FR bit) as the boot state sets it.
- **Code outside `GAME.EXE`** (boot ROM routines the game calls, anything copied into RAM at runtime): a small MIPS IV interpreter in the runtime runs any address with no recompiled function, sharing the same register context and memory map. The recompiled path is the fast path; the interpreter is the safety net, and every address it executes is logged so it can be moved to the recompiled list. *(Builder's call, from the review.)*
- Each MIPS function becomes a C function over a register context; `jal`/`jalr` become direct/indirect calls (indirect through a lookup table of recompiled functions); jump tables become `switch`.
- Uncached/cached aliases (KSEG0/KSEG1) and MMIO: memory accesses go through a fast path for RAM and a slow path for MMIO address ranges, decided at recompile time where the address is known and at runtime otherwise.
- Interrupt delivery: the runtime checks a pending-interrupt flag at safe points the recompiler inserts (function entries and loop back-edges) and runs the recompiled exception handler (vector at 0x80000180) with COP0 Status/Cause/EPC emulated.
- Idle-loop detection (wait-for-vblank polls) so the host thread sleeps instead of spinning.

### 4. Runtime (runtime/)

C/C++17, SDL2 for window/input/audio, OpenGL 3.3 core for rendering (radeonsi on Liverpool; also runs on desktop Linux for development).

- **Memory map:** 8 MB RAM, KSEG0/KSEG1 aliases, boot ROM image mapped for any calls the game makes into it.
- **GT64010:** PCI config space, DMA channels used to move data to Voodoo/IDE, timers, interrupt cause/mask.
- **Voodoo 1 (register level):** a command processor based on the register semantics in MAME's `voodoo` implementation; triangle setup registers (`triangleCMD`/`ftriangleCMD`, gradients, `fbzMode`, `alphaMode`, `textureMode`, fog table) are turned into OpenGL draws with a shader that reproduces the Voodoo 1 color/alpha combine units; texture memory uploads become GL textures (with a cache keyed on TMU address/format); LFB reads/writes are supported for the cases the game uses (e.g. `grab_save_screen`). Output rendered at the original 512x384 (seattle mode) first, scaled to the screen.
- **IOASIC:** inputs, the DCS command/data latches, UART (game debug prints go to the host console, very useful for bring-up).
- **IDE:** register-level ATA controller backed by the raw disk image (read-only, writes to an overlay file so audits/settings persist), keeping the game's own file system code untouched.
- **CMOS/NVRAM** (battery-backed settings, audits, gun calibration): its own persistent file. Seeded from a MAME `nvram` file for carnevil if one exists; otherwise left blank and the game initialises its defaults, accepting a one-off setup/calibration screen. *(Builder's call.)*
- **Security PIC (`486_carnevil.u96`):** M0 establishes whether `GAME.EXE` checks it; if so the runtime implements it the way MAME's driver does. *(Builder's call.)*
- **DCS audio:** ADSP-2115 interpreter (ported from MAME's BSD-licensed adsp2100/dcs code) running `sound102.u95` and the `.BIN` OS the game uploads, on its own host thread; output to SDL audio.
- **Input:** light-gun aiming from the DS4 right stick (and mouse on desktop), trigger = R2, reload = L2 (off-screen shot), start/coin mapped; the IOASIC/gun registers report the aim position the way the cabinet's gun hardware does.

### 5. Verification

- **MAME as the oracle:** run the same disk in MAME headless in a container in the Linux container (no GPU needed: MAME's Voodoo is a software renderer) with its debugger/trace; compare register/RAM state at agreed checkpoints (boot hand-off, after graphics_init, first attract frame), and compare rendered frames (dumped from MAME vs the port) with a perceptual diff.
- Unit tests for the recompiler: per-instruction semantics tests (especially MIPS IV and FP), run on the host against a small MIPS reference interpreter.
- The game's own console prints (via the UART) as bring-up breadcrumbs.

### 6. Milestones

- **M0** Extraction + analysis: load address, entry, boot state, function list; boot ROM and DIAG.EXE hand-off analysis (hardware state and RAM contents GAME.EXE expects); security PIC check; MAME baseline fps on the PS4 recorded when the PS4 is next up, so "not slower than MAME" has a number.
- **M1** Recompiler emits C for all of `GAME.EXE` that compiles and links (stub runtime).
- **M2** Boots and reaches the attract loop in the log (UART output, no graphics).
- **M3** Voodoo-to-GL: attract mode renders correctly versus MAME.
- **M4** Input and gun: a game can be played.
- **M5** DCS audio.
- **M6** PS4 build: performance pass, full speed, launcher entry. Full speed is the pass line; if register-level Voodoo can't reach it on Jaguar, the Glide HLE work is pulled forward into M6. *(Builder's call.)*
- **Phase 2** Glide HLE function by function; then widescreen/high-resolution options. Planned separately after M6; the first plan ends at M6. *(Builder's call.)*

## Constraints

- **Development hosts, in order (user's decision):** (1) the Mac natively first: Apple clang (already present), SDL2 built from source inside the project tree, OpenGL 3.3/4.1 core; the generated C and runtime stay portable, so arm64 macOS is a first-class dev target; (2) then the Linux PC (the PC with the PCIe HD60 Pro, reserved <address>) for Linux x86-64 testing; (3) the PS4 last. PS4 sessions are costly: they need the Linux PC running for the HD60 Pro, plus a USB HDMI capture device after it (two capture cards), on top of the jailbreak and Linux boot, so PS4 time is kept to M6.
- Builds: PS4 builds natively on the Linux container (Debian 13, GCC 14; binaries need glibc <= 2.38 to run on the PS4's 2.44, as with the tuned emulator cores) with `-O2 -march=btver2`.
- No host installs on the user's Mac; toolchains live in containers/CTs.
- Game data and generated C stay on the Gaming share (Research/carnevil); the git repo stays local/private and contains only tools, runtime, recompiler and tests.
- PS4 time is expensive (see development hosts): PS4 is only needed from M6, apart from one MAME baseline measurement; earlier milestones are tested on the Mac, then the Linux PC.
- The extracted game files, raw disk image and generated C live on the Gaming share under `Research/carnevil/`; the Mac reaches them over the existing SMB mount (`/Volumes/archive`), or works on local copies kept outside the repo.

## Left to the builder

- Exact fork strategy for N64Recomp (vendored fork vs patches).
- Internal structure of the runtime, shader design for the Voodoo combine units, texture cache policy.
- Test harness layout.
