# CarnEvil Native Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Midway's CarnEvil natively at full speed on PS4 Linux by statically recompiling `GAME.EXE` to C and linking it against a runtime that stands in for the Seattle arcade hardware.

**Architecture:** Python tools extract and analyse the game binary (M0); a recompiler based on N64Recomp turns the MIPS IV code into C (M1); a C/C++ runtime implements the hardware at register level with OpenGL rendering (M2–M5); then the Linux build (M6), and the PS4 build and performance pass last (M7). MAME is the reference the port is checked against.

**Tech Stack:** Python 3.12+ (stdlib only, `unittest`) for tools; C/C++17, SDL2 (built in-tree), OpenGL 3.3 core for the runtime; N64Recomp fork for the recompiler; MAME 0.276 (Debian package, in the Linux container) as the oracle.

**Spec:** `docs/superpowers/specs/2026-11-19-carnevil-recomp-design.md`

**Scope of this document:** M0 is planned task by task. M1–M7 are outlined with deliverables and exit checks; each gets its own detailed plan (`docs/superpowers/plans/…-m<N>.md`) when the previous milestone is done, because its decisions depend on that milestone's findings (for example the opcode census in M0 decides what M1 must add to N64Recomp).

## Global Constraints

- No game data, extracted files, ROMs or generated C in the git repo, ever. They live under the Gaming share: `reincarnevilation/work/` on the archive share (Mac: `/Volumes/archive/reincarnevilation/work`; the Linux container: `/mnt/reincarnevilation/work`). Analysis notes may quote addresses, opcodes counts and short strings, not code listings.
- The repo is published privately (GitHub `origin`, mirror `mirror`) on a BYOCHD basis: it ships no copyrighted game material, and users bring their own CHD. Implementers never `git push`; pushes happen only on the user's say-so. No Claude attribution in commits.
- Nothing is installed on the Mac. Python tools use the stdlib only and run with the existing `python3`. Anything else runs in the Linux container (`ssh root@<host> 'pct exec <id> -- su roms -c "…"'`), where apt installs are fine.
- Never run `head` or `tail`. Never print secrets.
- One supported game revision: `GAME.EXE` SHA-256 `f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6` (from `carnevil.chd`, data SHA1 `ba32b7ef9721d730ce58bd753d47fb947b6ae9b6`). Tools refuse other hashes.
- Other pinned hashes: `DIAG.EXE` `38b2810864c85d6971cedc3a6b0e507c8c6d593e4d7c355c880f91dbcaaaa2b9`; `boot.u32` = `carnevil1_9.u32` `05346e09d90a8e5aadc5e0aae8bd53b80816e96ea1e649ba548e4c6b07dac4a6`.
- Build order (user's decision): macOS first (M1–M5 end in a playable Mac build), then Linux x86-64 native and WSL2 on the Linux PC (M6), then PS4 (M7 only).
- Tests that need game data read `CARNEVIL_WORK` (default `/Volumes/archive/reincarnevilation/work`) and **skip** when it is missing. Unit tests use synthetic fixtures built in the test.

## Review Focus

1. **Wrong game revision** (`carnevi1.chd`, a hacked EXE): the tools must stop with a message naming the expected and actual hash, not analyse it silently. Test in Task 2.
2. **Truncated or corrupt disk/partition image:** a clear error, no partial garbage files left looking valid. Test in Task 1.
3. **File names inside the partition containing `/`, `..` or NULs:** nothing may be written outside the output directory. Test in Task 1.
4. **Data that looks like code:** words in the data section that decode as `jal` must not create functions. Test in Task 4.
5. **No game data present:** `python3 -m unittest` passes (skips) on a clean checkout, and each CLI says where the data is expected. Test in Task 1.

---

## M0: Extraction and analysis

Already known (recon 2026-11-19), to be confirmed by the tests below:
- `GAME.EXE` is 918,060 bytes: an 8-byte header then a flat little-endian MIPS image. Header word 0 = `0x2CAE3F54` (meaning unknown), word 1 = `0x800C4000` (load address). Image bytes = 918,052, so the image ends at `0x801A4224`.
- The first instructions at `0x800C4000` are `lui sp,0x8080` (stack top `0x80800000` = 8 MB RAM) and a loop clearing BSS from `0x801A4228`.
- The raw disk has 17 partitions; partition 0 holds 1,356 files.

### File structure

- `carnevil_tools/__init__.py`
- `carnevil_tools/disk.py`: partition table and partition file system
- `carnevil_tools/exe.py`: EXE header and image loading, hash gate
- `carnevil_tools/mips.py`: instruction decoding and classification
- `carnevil_tools/functions.py`: function discovery, TOML output
- `carnevil_tools/hwcensus.py`: hardware address census
- `carnevil_tools/cli.py`: `python3 -m carnevil_tools <command>`
- `tests/test_*.py`: one per module; `tests/gamedata.py`: the skip helper
- `config/game.functions.toml`, `config/boot_state.toml`: analysis outputs (addresses and names only)
- `docs/analysis/*.md`: findings

### Task 1: Disk tools

**Files:**
- Create: `carnevil_tools/__init__.py`, `carnevil_tools/disk.py`, `carnevil_tools/cli.py`, `tests/gamedata.py`, `tests/test_disk.py`, `README.md`

**Interfaces:**
- Produces:
  - `disk.read_partition_table(raw_path: str) -> list[Partition]` where `Partition` is a dataclass `(index: int, offset: int, size: int)` in bytes
  - `disk.split_partitions(raw_path: str, out_dir: str) -> list[str]` (paths `part0`…)
  - `disk.list_files(partition_path: str) -> list[FileEntry]` where `FileEntry` is `(name: str, offset: int, size: int)`
  - `disk.extract_files(partition_path: str, out_dir: str) -> list[str]`
  - `disk.DiskFormatError(Exception)`
  - `tests/gamedata.py`: `work_dir() -> str | None` and `requires_gamedata` (a `unittest.skipUnless` decorator)
  - CLI: `python3 -m carnevil_tools split <raw> -o <dir>`, `python3 -m carnevil_tools extract <partition> -o <dir>`

Format reference: `scripts/split_raw_img.py` and `scripts/split_partition.py` in https://github.com/xclusivor/CarnEvil-Research (a copy is at `/home/roms/CarnEvil-Research` in the Linux container). Read them to learn the on-disk layout and write the format down in `docs/analysis/disk-format.md`. Re-implement from that description (the repo has no licence file: do not copy its code); credit it in `README.md`.

- [ ] **Step 1: Write the failing tests** in `tests/test_disk.py`
  - `test_extract_rejects_path_escape`: build a synthetic partition (helper in the test, using the documented layout) whose file names are `../evil`, `a/b` and `ok.bin`; `extract_files` must raise `DiskFormatError` or write only inside `out_dir` with the separators replaced by `_`; assert no file exists outside `out_dir`.
  - `test_truncated_partition_raises`: a synthetic partition cut in half raises `DiskFormatError` and leaves `out_dir` empty.
  - `test_roundtrip_synthetic`: three files of known bytes come back byte-identical.
  - `test_part0_matches_reference` (`@requires_gamedata`): `list_files(work/parts/part0)` has 1,356 entries; extracting to a temp dir gives `GAME.EXE` with SHA-256 `f9455ce8…d3a6` and `DIAG.EXE` `38b28108…a2b9`.
  - `test_partition_table` (`@requires_gamedata`): `read_partition_table(work/carnevil.raw)` returns 17 partitions; partition 0 size is 98,816,000 bytes.
  - `test_cli_without_data_explains`: running the CLI on a missing path exits non-zero and prints the expected data location.
- [ ] **Step 2: Run** `python3 -m unittest discover -s tests -v`. Expected: import errors.
- [ ] **Step 3: Implement** `disk.py`, `cli.py`, `tests/gamedata.py`; write `docs/analysis/disk-format.md`.
- [ ] **Step 4: Run the tests.** Expected: all pass (game-data tests pass on the Mac with the share mounted, skip otherwise).
- [ ] **Step 5: Commit** `git commit -m "tools: disk partition and file extraction"`.

### Task 2: EXE loader and revision gate

**Files:**
- Create: `carnevil_tools/exe.py`, `tests/test_exe.py`, `docs/analysis/exe-format.md`

**Interfaces:**
- Produces:
  - `exe.ExeImage` dataclass: `(header_word0: int, load_addr: int, data: bytes, sha256: str)` with methods `end_addr() -> int`, `word(addr: int) -> int` (little-endian u32, raises `IndexError` outside the image), `contains(addr: int) -> bool`
  - `exe.load_exe(path: str, expect_sha256: str | None = GAME_EXE_SHA256) -> ExeImage`
  - `exe.GAME_EXE_SHA256`, `exe.DIAG_EXE_SHA256` constants (values from Global Constraints)
  - `exe.UnknownRevisionError(Exception)`

- [ ] **Step 1: Write the failing tests** in `tests/test_exe.py`
  - `test_unknown_hash_refused`: a synthetic EXE raises `UnknownRevisionError` whose message contains both the expected and the actual hash.
  - `test_synthetic_header`: bytes `struct.pack("<II", 0x11223344, 0x80010000) + 16 bytes` load (with `expect_sha256=None`) as `load_addr == 0x80010000`, `end_addr() == 0x80010010`, `word(0x80010000)` correct.
  - `test_game_exe_layout` (`@requires_gamedata`): `header_word0 == 0x2CAE3F54`, `load_addr == 0x800C4000`, `end_addr() == 0x801A4224`, `word(0x800C4000) == 0x3C1D8080`.
  - `test_diag_exe_loads` (`@requires_gamedata`): loads with `DIAG_EXE_SHA256`; record its `load_addr` in `docs/analysis/exe-format.md`.
- [ ] **Step 2: Run to see them fail.**
- [ ] **Step 3: Implement** `exe.py`.
- [ ] **Step 4: Find what header word 0 is.** Try, in order: 32-bit sum of image words, sum of bytes, CRC-32 (zlib), negated sum. Write the result (or "not identified by these four") in `docs/analysis/exe-format.md`; Task 5 confirms it from the boot ROM's loader.
- [ ] **Step 5: Run the tests, commit** `git commit -m "tools: EXE loader with revision gate"`.

### Task 3: MIPS decoder and opcode census

**Files:**
- Create: `carnevil_tools/mips.py`, `tests/test_mips.py`, `docs/analysis/opcode-census.md`

**Interfaces:**
- Consumes: `exe.ExeImage`
- Produces:
  - `mips.Insn` dataclass: `(word: int, mnemonic: str, rs: int, rt: int, rd: int, sa: int, imm: int, target: int | None, kind: str)` where `imm` is sign-extended, `target` is the absolute branch/jump target given the address, and `kind` is one of `"normal" | "branch" | "branch_likely" | "jump" | "call" | "jump_reg" | "call_reg" | "trap" | "unknown"`
  - `mips.decode(word: int, addr: int) -> Insn`
  - `mips.census(image: ExeImage, start: int, end: int) -> dict[str, int]` (mnemonic to count)
  - CLI: `python3 -m carnevil_tools census <exe>`

Coverage required: MIPS I–III integer and FPU (COP1 S/D/W/L), COP0 (`mfc0`/`mtc0`/`eret`/`tlb*`/`cache`), and the MIPS IV additions `movn`, `movz`, `movf`, `movt`, `movf.fmt`/`movt.fmt`/`movn.fmt`/`movz.fmt`, FP condition codes 0–7 on `c.cond.fmt`/`bc1*`, COP1X (`lwxc1`, `ldxc1`, `swxc1`, `sdxc1`, `madd.s/d`, `msub.s/d`, `nmadd`, `nmsub`, `prefx`), `pref`. Anything else decodes as `mnemonic="unknown"`, `kind="unknown"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_mips.py`, each asserting mnemonic, kind and operands:
  - `0x3C1D8080` at any address: `lui`, `rt == 29`, `imm == 0x8080 - 0x10000` or raw `0x8080` (pick: `imm` holds the raw 16 bits for `lui`, sign-extended for the rest; assert `imm == 0x8080`).
  - `0x03E00008`: `jr`, `rs == 31`, kind `jump_reg`.
  - `0x0C031000` at `0x800C4000`: `jal`, target `0x800C4000`, kind `call`.
  - `0x1000FFFF` at `0x80010000`: `beq` (or `b`), target `0x80010000`, kind `branch`.
  - `0x0085100B`: `movn` `rd == 2`, `rs == 4`, `rt == 5`.
  - `0x4C850000 | 0x20` style COP1X `madd.s` (encode with a helper in the test from the field layout) decodes as `madd.s`.
  - `0xFFFFFFFF`: kind `unknown`.
- [ ] **Step 2: Run to see them fail.**
- [ ] **Step 3: Implement** `mips.py` (table-driven on opcode / funct / fmt fields).
- [ ] **Step 4: Run the tests.**
- [ ] **Step 5: Run the census on `GAME.EXE`** over the whole image and write `docs/analysis/opcode-census.md`: the mnemonic counts, the list of MIPS IV instructions actually present with counts, the COP0 instructions present, and the number and first 20 addresses of `unknown` words (most will be data; Task 4 narrows this to the code section).
- [ ] **Step 6: Commit** `git commit -m "tools: MIPS IV decoder and opcode census"`.

### Task 4: Function discovery

**Files:**
- Create: `carnevil_tools/functions.py`, `tests/test_functions.py`, `config/game.functions.toml`, `docs/analysis/functions.md`

**Interfaces:**
- Consumes: `exe.ExeImage`, `mips.decode`
- Produces:
  - `functions.Function` dataclass: `(addr: int, size: int, name: str, sources: set[str])`, sources drawn from `"entry" | "call" | "pointer" | "prologue" | "manual"`
  - `functions.JumpTable` dataclass: `(jr_addr: int, table_addr: int, targets: list[int])`
  - `functions.Analysis` dataclass: `(text_start: int, text_end: int, functions: list[Function], jump_tables: list[JumpTable], unreached: list[tuple[int, int]])`
  - `functions.analyse(image: ExeImage, entry: int, manual: list[int] = ()) -> Analysis`
  - `functions.write_toml(analysis: Analysis, path: str) -> None` and `functions.read_toml(path: str) -> Analysis`
  - CLI: `python3 -m carnevil_tools functions <exe> -o config/game.functions.toml`

Algorithm (the order matters):
1. Recursive descent from `entry` following branches (both arms), delay slots, and `jal` targets inside the image; stop a path at `jr ra`, `eret`, or an unconditional jump out of the function. Every word reached is code.
2. Jump tables: at each `jr` on a non-`ra` register, look back up to 12 instructions for the `sltiu` bound and the `lui`/`addiu` table base; read that many aligned pointers; each target inside the image continues the descent and is recorded in a `JumpTable`.
3. `text_end` = the end of the last code word reached, rounded up to 4; refine in step 5.
4. Pointer scan: every aligned word in `[text_end, image end)` whose value lies in `[text_start, text_end)` and whose target starts with a stack-adjust prologue (`addiu sp,sp,-n`) or is already a known function start becomes a function (source `pointer`), then descend from it.
5. Prologue scan inside gaps of `[text_start, text_end)` not yet reached: a gap starting with `addiu sp,sp,-n` becomes a function (source `prologue`); other gaps are listed in `unreached`.
6. Function size = up to the next function start or `text_end`. Names: `func_<ADDR>`; the entry is `_start`.

- [ ] **Step 1: Write the failing tests** in `tests/test_functions.py` using a tiny assembler helper in the test (encode from fields) to build synthetic images:
  - `test_call_graph`: `_start` calls `f1`, `f1` calls `f2`, each ends with `jr ra` + delay slot: three functions with the right addresses and sizes.
  - `test_delay_slot_is_code`: the instruction after `jr ra` belongs to the function.
  - `test_jump_table`: a `sltiu` + `lui/addiu` + `lw` + `jr` dispatch over a 4-entry table gives one `JumpTable` with 4 targets, and the case blocks are code.
  - `test_function_pointer_in_data`: a function referenced only from a data word is found with source `pointer`.
  - `test_data_jal_lookalike_ignored`: a data word that decodes as `jal` to an address in the data section creates no function, and a data word equal to a non-prologue text address creates no function.
  - `test_toml_roundtrip`: `read_toml(write_toml(a)) == a`.
  - `test_game_exe` (`@requires_gamedata`): with `entry = 0x800C4000`: between 1,800 and 2,600 functions; functions sorted and non-overlapping; every in-image `jal` target reached is a function start; `unreached` totals under 5% of `[text_start, text_end)`; `text_end` is below `0x801A4224`.
- [ ] **Step 2: Run to see them fail.**
- [ ] **Step 3: Implement** `functions.py`.
- [ ] **Step 4: Run the tests.**
- [ ] **Step 5: Generate** `config/game.functions.toml` and write `docs/analysis/functions.md`: counts by source, `text_end`, the jump-table count, the unreached ranges, and the census of `unknown` instructions restricted to reached code (this must be zero; any non-zero entry is a decoder gap to fix in `mips.py` with a new test).
- [ ] **Step 6: Name functions from strings.** For each debug string of the form `name(): …` or `name: …` in the data section, find the functions that reference its address (`lui`/`addiu` pair) and, when exactly one function references it, rename that function. Add `test_string_naming` on a synthetic image; record how many functions were named.
- [ ] **Step 7: Commit** `git commit -m "tools: function discovery and functions.toml"`.

### Task 5: Boot hand-off analysis

**Files:**
- Create: `docs/analysis/boot-handoff.md`, `config/boot_state.toml`, `tests/test_boot_state.py`
- Modify: `carnevil_tools/cli.py` (add `disasm <file> --base <addr> --start <addr> --count <n>` printing decoded instructions to the terminal only)

**Interfaces:**
- Consumes: `mips.decode`, `exe.load_exe`, `functions.read_toml`
- Produces: `config/boot_state.toml` with keys `entry`, `sp`, `gp`, `status`, `config`, `fcr31`, `ra`, and tables `[[ram_preload]]` (`addr`, `size`, `source`) and `[[external_call]]` (`addr`, `region`, `callers`)

- [ ] **Step 1: Static analysis of `boot.u32`** (mapped at `0xBFC00000`, 512 KB). Find and document in `boot-handoff.md`: the reset path; the routine that loads an EXE from disk (what it does with header word 0 and word 1: confirm or correct `exe-format.md`); the order boot ROM → `DIAG.EXE` → boot ROM → `GAME.EXE` and how control returns; what the ROM installs at the exception vectors (`0x80000000`, `0x80000080`, `0x80000180`) and whether it copies code to RAM.
- [ ] **Step 2: External calls.** List every `jal`/`j` target and every `jalr` whose target register is loaded with a constant, in `GAME.EXE` reached code, that lies outside `[text_start, text_end)`: these are calls into the boot ROM or into RAM the ROM set up. Also list every place `GAME.EXE` writes code to RAM (stores to `0x80000000`–`0x800001FF`, `cache` instructions). Record them as `[[external_call]]`.
- [ ] **Step 3: Dynamic confirmation with MAME.** In the Linux container: `apt-get install -y mame` (as root), then as `roms` run MAME headless with a ROM path holding `carnevil.zip` and the `carnevil/` CHD folder, `-video none -sound none -debug -debugger none -debugscript <script>`, where the script sets a breakpoint at `0x800C4000` that logs the register state and the COP0 Status/Config and COP1 FCR31 values, dumps RAM `0x80000000`–`0x800C3FFF` to a file on the share, and quits. If `-debugger none` does not execute scripts in this MAME build, use `-debugger gdbstub` and drive it with a short Python socket client instead. Compare the dump against the static findings and fill `config/boot_state.toml`; `[[ram_preload]]` lists every non-zero RAM range below the image at entry and where it came from (ROM copy, DIAG leftover, loader data).
- [ ] **Step 4: Write** `tests/test_boot_state.py`: `boot_state.toml` parses (`tomllib`), `entry == 0x800C4000`, `sp` is inside `0x80000000`–`0x80800000`, every `external_call.addr` is outside the game's text range, and every `ram_preload` range is below `0x800C4000`.
- [ ] **Step 5: Decide and record** in `boot-handoff.md`: which external calls the runtime replaces with native stand-ins and which run under the interpreter; whether skipping `DIAG.EXE` loses any state `GAME.EXE` reads.
- [ ] **Step 6: Commit** `git commit -m "analysis: boot hand-off and boot state"`.

### Task 5b: Extractor follows block chains; EXE checksum; re-pin hashes

Inserted by the controller after Task 5 found a load-bearing defect in Task 1's output.

**The defect.** `carnevil_tools/disk.py` reads a file as `size` contiguous bytes from its first block. The file system on the disk chains a file's blocks; the boot ROM's loader follows the chain. For `GAME.EXE` the two disagree in the last 4 KB block: 315 bytes (113 words) differ within file offsets 917,504–918,060 (load addresses `0x801A3FF8`–`0x801A4224`, data only; the text is unaffected). The SHA-256 pinned so far for `GAME.EXE` (`47aea64a3e5d15c15f28b3907294e706f078a4e9a47c422f551f420d9ba7da9e`) is the hash of the wrong bytes. Other files on the disk may be affected too. See the extractor section of `docs/analysis/boot-handoff.md`.

**Two independent oracles for a correct `GAME.EXE`:**
- Header word 0 is the POSIX `cksum` CRC of the file's bytes from offset 4 to the end (`0x2CAE3F54` for `GAME.EXE`, `0x5799F785` for `DIAG.EXE`; the currently extracted `DIAG.EXE` already satisfies it, the currently extracted `GAME.EXE` does not).
- MAME's RAM at the moment the game starts: `/Volumes/archive/reincarnevilation/work/mame/dumps/ram-at-game-entry.bin` (8 MB, physical address 0 at file offset 0). The image the loader placed at `0x800C4000` (file offset `0xC4000`, length = `GAME.EXE` size − 8) must equal the extracted file's bytes from offset 8.

**Files:**
- Modify: `carnevil_tools/disk.py`, `tests/test_disk.py`, `carnevil_tools/exe.py`, `tests/test_exe.py`, `carnevil_tools/cli.py`, `docs/analysis/disk-format.md`, `docs/analysis/exe-format.md`, `docs/analysis/boot-handoff.md` (extractor section only: mark it fixed and give the new hash), `docs/analysis/functions.md`, `config/game.functions.toml`, `README.md`, `docs/getting-started.md`, `docs/superpowers/plans/2026-11-19-carnevil-recomp.md` and `docs/superpowers/specs/2026-11-19-carnevil-recomp-design.md` (the pinned hash lines only)

**Interfaces:**
- Unchanged signatures: `disk.read_partition_table`, `disk.split_partitions`, `disk.list_files(partition_path) -> list[FileEntry]`, `disk.extract_files(partition_path, out_dir) -> list[str]`, `disk.DiskFormatError`. `FileEntry` keeps `name`, `offset` (byte offset of the file's first block) and `size`; add a field only if a test needs it.
- Produces:
  - `exe.posix_cksum(data: bytes) -> int` (the POSIX `cksum` algorithm: CRC-32 polynomial `0x04C11DB7`, MSB first, initial value 0, the length appended as its minimal little-endian byte string, final complement)
  - `exe.ChecksumError(ValueError)`; `exe.load_exe` raises it when header word 0 differs from `posix_cksum(file_bytes[4:])`, after the SHA-256 gate and with both values in the message; `load_exe(path, expect_sha256=None)` still verifies the checksum
  - `exe.GAME_EXE_SHA256` and `exe.DIAG_EXE_SHA256` re-pinned to the hashes of correctly extracted files
  - CLI: `python3 -m carnevil_tools verify <partition> ` (see Step 6)

- [ ] **Step 1: Learn the chain.** Work out from the boot ROM's loader (`boot.u32` at `0xBFC00000`; the loader body is ROM `0xBFC00F20`–`0xBFC16B60`, running at `0x80000000`; `python3 -m carnevil_tools disasm` prints to the terminal) and from the disk itself how a file's blocks are chained and how the end of a chain is marked. Rewrite `docs/analysis/disk-format.md` to describe it, and say there that the earlier contiguous reading (shared with the public research scripts) was wrong for files whose blocks are not adjacent.
- [ ] **Step 2: Write the failing tests** in `tests/test_disk.py`
  - `test_noncontiguous_file_follows_chain`: a synthetic partition (extend the existing builder) with a three-block file whose blocks are stored out of order and interleaved with another file's; `extract_files` returns the bytes in chain order.
  - `test_chain_loop_raises` and `test_chain_link_out_of_range_raises`: `DiskFormatError`, output directory left empty.
  - `test_chain_shorter_than_size_raises`: a chain that ends before `size` bytes are covered raises `DiskFormatError`.
  - `test_game_exe_matches_loader` (`@requires_file("parts", "part0")` and `@requires_file("mame", "dumps", "ram-at-game-entry.bin")`): extract `part0` to a temp dir; `GAME.EXE`'s bytes from offset 8 equal the dump's bytes at `0xC4000` for the same length.
  - Update `test_part0_matches_reference`: still 1,356 entries; `GAME.EXE` and `DIAG.EXE` assert their **full** new SHA-256 values.
- [ ] **Step 3: Write the failing tests** in `tests/test_exe.py`
  - `test_posix_cksum_check_value`: `posix_cksum(b"123456789") == 0x377A6011`; `posix_cksum(b"") == 0xFFFFFFFF`.
  - `test_bad_header_checksum_refused`: a synthetic EXE with a wrong word 0 raises `ChecksumError` naming both values; with the right word 0 it loads.
  - Existing synthetic tests build their header word 0 with `posix_cksum`.
  - `test_game_exe_layout` and `test_diag_exe_loads` keep their layout assertions (`load_addr`, `end_addr()`), and `test_diag_exe_loads` also asserts `load_addr == 0x80100000` and `end_addr() == 0x801725CC`.
- [ ] **Step 4: Run the tests to see them fail, then implement** the chain following in `disk.py` and the checksum in `exe.py`.
- [ ] **Step 5: Re-extract and re-pin.** Extract `parts/part0` to `/Volumes/archive/reincarnevilation/work/part0_files_chain/` (a new folder). Confirm `GAME.EXE` there satisfies both oracles and `DIAG.EXE` satisfies the checksum. Then, on the share, rename the old folder `part0_files` to `part0_files_contiguous_wrong` and `part0_files_chain` to `part0_files` (renames only; delete nothing). Pin the new SHA-256 values in `exe.py`, and replace every occurrence of the old `GAME.EXE` hash in the repo (`git grep 47aea64a`) and of the old `DIAG.EXE` hash if it changed (`git grep 38b28108`). Report how many of the 1,356 files changed between the two extractions (count and total bytes; names of the first ten).
- [ ] **Step 6: Whole-disk check.** `FILESYS.CHK` (in `part0_files`) is the game's own table of per-file checksums: records of `0x126` bytes, with the file name near offset `0x1E`, the size as a little-endian u32 at `0x1B` and a checksum as a little-endian u32 at `0x123` (from the public research notes; verify the layout rather than trusting it). Find out whether that checksum is `posix_cksum` of the file (try it first, then zlib CRC-32) and, if identified, add `python3 -m carnevil_tools verify <partition>` that extracts in memory and reports how many listed files match, listing the mismatches. Record the result in `docs/analysis/disk-format.md`. If the algorithm cannot be identified with those two candidates, say so there and skip the command (and the CLI entry) rather than guessing.
- [ ] **Step 7: Regenerate the analysis.** `python3 -m carnevil_tools functions <GAME.EXE> -o config/game.functions.toml`; run the whole suite. The text is unchanged, so the function list should change only through data-section pointers in the corrected last block: report exactly what changed (functions added or removed, by address), and update the numbers in `docs/analysis/functions.md` and the census there if any moved.
- [ ] **Step 8: Update `docs/analysis/exe-format.md`:** header word 0 is the POSIX `cksum` of the file from offset 4 (replace the "not identified" section, keep the list of what was ruled out), with the two EXEs' values; and mark the extractor section of `docs/analysis/boot-handoff.md` as fixed by this task, with the corrected hash.
- [ ] **Step 9: Commit** `git commit -m "tools: follow block chains when extracting; verify EXE header checksum; re-pin hashes"`.

### Task 6: Hardware usage census

**Files:**
- Create: `carnevil_tools/hwcensus.py`, `tests/test_hwcensus.py`, `docs/analysis/hardware-usage.md`

**Interfaces:**
- Consumes: `exe.ExeImage`, `mips.decode`, `functions.Analysis`
- Produces:
  - `hwcensus.REGIONS: list[Region]` with `Region = (name: str, start: int, end: int)` (physical addresses) taken from MAME's `seattle.cpp` memory map: RAM, Voodoo, GT64010, IOASIC, IDE, CMOS/NVRAM, interrupt control, boot ROM, and the others that map lists
  - `hwcensus.constant_addresses(image, analysis) -> dict[int, list[int]]` (address formed by a `lui` + `addiu`/`ori`/load-store offset pair, to the code addresses forming it)
  - `hwcensus.classify(addr: int) -> str` (region name or `"unknown"`, after stripping the KSEG0/KSEG1 bits)

- [ ] **Step 1: Write the failing tests:** `test_lui_addiu_pair`, `test_lui_load_offset` (negative offset borrows from the high half), `test_classify_kseg1_alias` (`0xA8000000 + x` and `0x88000000 + x` classify the same), `test_game_exe_regions` (`@requires_gamedata`: Voodoo, GT64010, IOASIC and IDE regions each have at least one reference; no address at or above physical `0x08000000` classifies as `"unknown"`).
- [ ] **Step 2: Run to see them fail, implement, run to pass.**
- [ ] **Step 3: Write** `docs/analysis/hardware-usage.md`: per region, the registers touched (offset, read/write, number of sites, the named functions that touch them). Answer three questions explicitly: does `GAME.EXE` talk to the security PIC through the IOASIC (and which commands), does it read CMOS/NVRAM (which offsets), and does it use Voodoo linear frame buffer access or only the command registers.
- [ ] **Step 4: Commit** `git commit -m "analysis: hardware usage census"`.

### Task 7: M0 report

**Files:**
- Create: `docs/analysis/M0-report.md`

- [ ] **Step 1: Write the report:** one page with the confirmed facts (load address, entry, text/data/bss ranges, function count, MIPS IV instructions in use, boot state, external calls, hardware regions, PIC/NVRAM/LFB answers) and a list of every assumption in the spec that M0 changed.
- [ ] **Step 2: Add the open item:** MAME baseline fps on the PS4 for CarnEvil attract mode and first level (RetroArch statistics overlay, MAME core, current settings), to be recorded the next time the PS4 is up for another reason.
- [ ] **Step 3: Run the whole suite** `python3 -m unittest discover -s tests -v`; expected: all pass with the share mounted, and all pass with skips with `CARNEVIL_WORK=/nonexistent`.
- [ ] **Step 4: Commit** `git commit -m "analysis: M0 report"`. Then write the M1 plan.

---

## M1–M7 outlines (each planned in detail when reached)

### M1: Recompiler emits C for all of `GAME.EXE`
- **Done** (complete on branch `m1-recompiler`, pending its final review): the results, the departures from its plan and the decisions left for M2 are in [docs/analysis/M1-report.md](../../analysis/M1-report.md).
- **Planned in detail in [2026-11-19-carnevil-recomp-m1.md](2026-11-19-carnevil-recomp-m1.md).** That plan replaces this outline: the recompiler is a Python emitter on M0's analysis, not an N64Recomp fork, and it is checked against MAME vectors and replayed game functions.
- Fork N64Recomp into `recomp/` (vendored, MIT, history noted). Add: little-endian memory access, a flat-image loader fed by `config/game.functions.toml` instead of an ELF, the MIPS IV instructions listed in `docs/analysis/opcode-census.md`, FP condition codes 0–7, the FR mode from `boot_state.toml`, interrupt check points at function entries and loop back-edges, jump tables as `switch`.
- Reference interpreter `runtime/interp/` (C): the same instruction semantics, used for unrecompiled code and as the test oracle.
- Tests: per-instruction semantic tests comparing recompiled C against the interpreter on random register states (every mnemonic in the census); a whole-image test that the generated C for all functions compiles and links against a stub runtime.
- Exit: `make recomp` produces `generated/` on the share and it links with zero unresolved functions.

### M2: Boots to the attract loop in the log
- **Done** (complete on branch `m2-runtime`, pending its final review): the results, the comparisons with MAME, every ruling and departure from its plan, and what M3 needs are in [docs/analysis/M2-report.md](../../analysis/M2-report.md).
- **Planned in detail in [2026-11-19-carnevil-recomp-m2.md](2026-11-19-carnevil-recomp-m2.md).** That plan replaces this outline: the loader is replaced by native stand-ins, so IDE, the GT64010's PCI and timers, the UART and the security PIC are not emulated as registers.
- `runtime/`: memory map (8 MB RAM, KSEG0/KSEG1), COP0 and exceptions, GT64010 (PCI config, timers, interrupt cause/mask, DMA), IOASIC with UART to the host console, IDE backed by `carnevil.raw` with a write overlay, CMOS/NVRAM file, the external-call stand-ins from `boot-handoff.md`, security PIC if `hardware-usage.md` says it is used. Voodoo accepts and discards writes; vblank interrupts are generated.
- Tests: device unit tests against register sequences captured from a MAME trace; a boot test asserting the UART log reaches the same milestones as MAME's, in order.
- Exit: the game runs its attract-mode loop headless on the Mac with no interpreter fallbacks left unexplained.

### M3: Voodoo to OpenGL
- Register-level Voodoo 1: triangle setup to GL draws, a shader for the colour/alpha combine, fog, depth, alpha test and blend; texture memory to GL textures with a cache; LFB paths the census found. SDL2 built in-tree for the window.
- Tests: golden frames dumped from MAME at fixed frame numbers in attract mode compared with the port's frames (per-pixel tolerance and a maximum differing-pixel fraction).
- Exit: attract mode matches MAME visually on the Mac.

### M4: Input
- Gun position and trigger through the IOASIC/gun registers the way the cabinet reports them; mouse on desktop, DS4 right stick + R2/L2 via SDL game controller; coin and start.
- Tests: input injection tests asserting the register values the game reads; a scripted run that starts a game and scores a hit.
- Exit: a full level is playable on the Mac.

### M5: DCS audio
- ADSP-2115 interpreter and DCS2 glue ported from MAME's BSD-licensed sources into `runtime/dcs/`, running `sound102.u95` and the OS image the game uploads, on its own thread, to SDL audio.
- Tests: the first N output samples for a fixed command sequence match a MAME capture.
- Exit: music and effects play correctly through attract mode and a level. This is the first playable build (macOS).

### M6: Linux x86-64
- The runtime and generated C built on Linux x86-64: natively, and under WSL2 on the Linux PC (OpenGL through WSLg/Mesa). SDL2 from the distribution or built in-tree.
- Tests: the M3 golden-frame comparison, the M4 input tests and the M5 audio test pass on Linux; a full level is playable.
- Exit: CarnEvil is playable on Linux and under WSL2 with the same results as the Mac build.

### M7: PS4
- PS4 build in the Linux container (`-O2 -march=btver2`, glibc <= 2.38), thread pinning off CPU1, exclusive fullscreen, a launcher entry.
- Performance pass with the MAME baseline from M0 as the floor and full speed as the pass line; if register-level Voodoo cannot reach it on Jaguar, start the Glide-level replacement for the hottest paths here.
- Exit: CarnEvil plays at full speed on the PS4 from the menu.
