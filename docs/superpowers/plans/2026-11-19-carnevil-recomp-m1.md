# CarnEvil M1: Recompiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn every function of `GAME.EXE` into C that compiles, links against a stub runtime and is shown correct against MAME, one instruction at a time and on real game functions.

**Architecture:** A Python emitter in `carnevil_tools` reads `config/game.functions.toml` (M0's output) and writes one C function per MIPS function. The generated code and a small C reference interpreter share one header, `runtime/include/recomp.h`, which defines the CPU context and the memory and arithmetic helpers. Three implementations are compared: MAME's R5000 core (recorded as test vectors), the interpreter, and the generated C.

**Tech Stack:** Python 3.12+ (stdlib only, `unittest`); C11 built with the host's `cc` (Apple clang on the Mac, GCC 14 in the Linux container) and `make`; MAME 0.276 in the Linux container as the oracle.

**Spec:** `docs/superpowers/specs/2026-11-19-carnevil-recomp-design.md` (section 3, and "Corrections from M0" and "Corrections from M1 planning" at its end). Findings this plan builds on: `docs/analysis/M0-report.md`, decisions 1 to 4.

## Decisions this plan takes

Three choices are made here. Each is recorded in the spec's corrections so the spec stays the authority.

- **The recompiler is our own Python emitter, not a fork of N64Recomp.** The spec says "base on N64Recomp" and leaves the fork strategy to the builder. Its source (commit `598ce20`, read 2026-11-19) rejects every MIPS IV instruction the game uses (`movz`, `movn`, `movf`, `movt`, `madd.s` and the rest, `recip.s`), and so does the decoder library it depends on. It hard-codes big-endian in the tool and in every memory macro. It keeps one FP condition bit as a function-local, takes no jump tables from outside, has no interrupt check points, and handles FR = 0 through asserts. It also needs function boundaries supplied, which is the part M0 already built. A fork would rewrite most of its 3,000 lines of translation and C back end and add C++20 and five vendored libraries. M0's decoder and analysis already cover everything up to the emitter. What is kept from N64Recomp is the shape of its output: one C function per MIPS function over a register context, `goto` labels inside a function, a lookup for indirect calls. It is credited in the docs.
- **Loader services (M0 decision 1): native stand-ins, option (a).** In M1 this only means a call into the stub block becomes `rt_loader(ram, c, slot)`. The runtime behind it is M2's.
- **Voodoo and other hardware traffic (M0 decision 2): caught at memory-access level, option (b).** Every load and store checks whether the address is RAM and goes to the runtime's bus otherwise.

## Global Constraints

- No game data, extracted files, ROMs, RAM dumps or generated C in the git repo, ever. Generated C goes to `$CARNEVIL_WORK/generated`, objects and test binaries to `$CARNEVIL_WORK/build/<uname -s>-<uname -m>` or a temp dir. `CARNEVIL_WORK` defaults to `/Volumes/archive/reincarnevilation/work` (the Linux container: `/mnt/reincarnevilation/work`). Notes and config may hold addresses, counts and short strings, not code listings.
- Test vectors made of synthetic instruction words and register values may be committed. Anything captured from the running game may not.
- BYOCHD: implementers never `git push`; no Claude attribution in commits.
- Nothing is installed on the Mac. Python is stdlib only. C is built with the `cc` and `make` already present. MAME runs only in the Linux container.
- Never run `head` or `tail`. Never print secrets.
- One supported revision: `GAME.EXE` SHA-256 `f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6`. `boot.u32` is `05346e09d90a8e5aadc5e0aae8bd53b80816e96ea1e649ba548e4c6b07dac4a6`.
- Tests that need game data use `tests/gamedata.py` and skip when the file is missing. Tests that need a C compiler skip, with a message naming `cc`, when there is none. The suite must pass with `python3 -W error -m unittest`.
- C flags for every C file, generated or not: `-std=c11 -O2 -ffp-contract=off -frounding-math -fno-strict-aliasing -fwrapv`, and never `-ffast-math`. `-frounding-math` stops the compiler folding FP expressions as if the rounding mode were always round-to-nearest. The R5000's `madd` is a multiply then an add with two roundings; a host compiler that fuses them gives different bits. Runtime C also builds with `-Wall -Wextra -Werror`; generated C with `-Wall -Werror -Wno-unused-label -Wno-unused-variable -Wno-unused-but-set-variable`.
- Generated C and the interpreter have no undefined behaviour: the test harness is also built with `-fsanitize=undefined -fno-sanitize-recover=undefined`.
- The instruction set to support is the census of reached code in `docs/analysis/functions.md` ("Census of reached code"): 98 mnemonics. An instruction outside it is an error naming the address and mnemonic, never a silent no-op.
- General registers are 32 bits wide. Reached code has no 64-bit integer instruction.
- FR = 0: there are 32 FP register halves of 32 bits. A single is one half. A double in register `n` (even) is half `n` (low word) and half `n + 1` (high word).

## Review Focus

1. **A delay slot that changes what its branch reads or writes:** `beq a0,zero,L` with `addiu a0,a0,-1` in the slot, `jal` with a slot that reads or writes `ra`, `jalr t9` with a slot that overwrites `t9`. The branch decides, and the call target is read, before the slot runs. Tests in Task 4.
2. **A branch whose target is a delay slot,** and a branch-likely whose slot must not run when the branch is not taken. Tests in Task 4.
3. **Arithmetic that is undefined in C but defined on MIPS:** division by zero, `INT_MIN / -1`, a variable shift by 32 or more, signed overflow in `addiu`, `trunc.w.s` of a value outside the `int32` range or of a NaN. No crash, no sanitizer report, and the result MAME gives; for `INT_MIN / -1`, which MAME cannot run (its host division faults), `lo = 0x80000000` and `hi = 0`, what the R5000 produces. Tests in Tasks 2 and 3.
4. **A call or jump to an address with no recompiled function** (an unreached leaf, a mid-function address, a null pointer) and **a load or store outside RAM:** the interpreter or the bus takes it and the address is logged; the host process does not crash. Tests in Tasks 2 and 5.
5. **An input the generated code was not made from:** a `GAME.EXE` whose hash differs from `exe_sha256` in the functions file, or an instruction the emitter does not know. `recompile` stops with a message naming both hashes or the address and mnemonic, and leaves no output folder that `make linkcheck` would accept. Tests in Task 5.

---

## File structure

- `runtime/include/recomp.h`: the contract between generated code, the interpreter and any runtime. Context struct, memory access, arithmetic helpers, the entry points a runtime must provide.
- `runtime/core/call.c`: `rt_call` and `rt_jump` (lookup, then the interpreter). `runtime/core/fpu.c`: FCSR to host rounding and flush-to-zero.
- `runtime/interp/interp.c`: the reference interpreter.
- `runtime/stub/stub.c`: the smallest runtime (bus that logs, loader that records and stops). `runtime/stub/smoke.c` and `runtime/stub/replay.c`: the two programs of Task 6.
- `carnevil_tools/vectors.py`: builds the instruction test cases. `tools/mame/insnvec.lua`: runs them in MAME. `tests/vectors/insn.json`: the recorded results.
- `carnevil_tools/emit.py`: one instruction to C statements. `carnevil_tools/recomp.py`: one function to a C function, and the whole image to files.
- `tests/charness.py` and `tests/harness/harness.c`: build and run C against test cases from Python.
- `Makefile`: `recomp`, `linkcheck`, `smoke`, `replay`, `test`.
- `docs/analysis/M1-report.md`, `docs/analysis/recompiler.md`.

---

### Task 1: Instruction vectors from MAME

**Files:**
- Create: `carnevil_tools/vectors.py`, `tools/mame/insnvec.lua`, `tests/vectors/insn.json`, `tests/test_vectors.py`
- Modify: `tools/mame/README.md`

**Interfaces:**
- Produces: `vectors.State` (dataclass: `r: list[int]` 32 words, `hi`, `lo`, `fpr: list[int]` 32 words, `fcsr` with the condition bits in place, `mem: dict[int, int]` aligned address to 32-bit word); `vectors.Case` (`name: str`, `words: list[int]`, `before: State`); `vectors.cases(seed=1) -> list[Case]`; `vectors.load(path) -> list[tuple[Case, State]]` (case and the state after); `vectors.assemble(mnemonic, **fields) -> int` for the tests of later tasks.
- A case is a small program, not one word. It is placed at `0x80100000`, entered with `ra = 0x80100F00`, and is finished when the PC reaches `ra`. Its memory operands lie in `0x80200000` to `0x80200FFF`. Programs end in `jr ra` and `nop`. A branch case sets a marker register differently on the two paths, so no PC needs comparing.

- [ ] **Step 1: Write the failing tests** in `tests/test_vectors.py`:
  - `test_every_census_mnemonic_has_cases`: the set of mnemonics decoded from all case words (with `mips.decode`) contains every mnemonic of the reached census (the 98 names, kept as a constant in `vectors.py` and asserted equal to a list copied from `functions.md`).
  - `test_cases_are_deterministic`: `cases(1) == cases(1)`, and `cases(2) != cases(1)`.
  - `test_edge_operands_present`: for `div`, `divu`, `sllv`, `addiu`, `mult`, `trunc.w.s`, `trunc.w.d` there are cases whose operands include 0, 1, `0xFFFFFFFF`, `0x80000000`, `0x7FFFFFFF` (and for the two `trunc`, 3e9, -3e9 and a NaN).
  - `test_fp_cc_cases`: there are `c.lt.s` and `bc1f`/`bc1fl` cases with condition codes 1 and 2, and `movf`/`movt` cases.
  - `test_recorded_vectors_match_cases`: `load("tests/vectors/insn.json")` has one entry per case of `cases(1)`, same names and words.
  - `test_known_results`: in the recorded file, an `addu` case's result equals the 32-bit sum, a `mult` case's `hi:lo` equals the signed 64-bit product, an `ldc1` case fills halves `n` and `n + 1` from the words at the address and the address plus 4.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_vectors`: fails on the missing module.
- [ ] **Step 3: Implement `vectors.py`.** At least 12 random cases per mnemonic plus the edge cases above; FP operands drawn from finite normal numbers except in the named edge cases. Keep the total under 3,000 cases.
- [ ] **Step 4: Implement `tools/mame/insnvec.lua`** and run it in the Linux container with `-nodrc`. It stops the machine at `GAME.EXE`'s entry (`0x800C4000`), clears Status IE so no interrupt runs, then for each case writes the words and memory, sets the registers, sets the PC, runs to a breakpoint at `ra`, and records every register and the words of the scratch page. Input is a JSON file written by `python3 -m carnevil_tools vectors --write-cases <file>`; output is one JSON line per case. `python3 -m carnevil_tools vectors --merge <cases> <results> -o tests/vectors/insn.json` writes the committed file, storing for each case only the fields of the after-state that differ from the before-state.
- [ ] **Step 5: Run** the tests: pass. Record in `tools/mame/README.md` the command line and run time, and which results look like MAME rather than hardware (see below).
- [ ] **Step 6: Commit** `git commit -m "vectors: instruction test cases and their results from MAME"`.

Where MAME's result is not a hardware fact (the bits of a generated NaN, `trunc` out of range, division by zero), list those cases in `vectors.py` as `MAME_SPECIFIC` with a one-line reason each. Later tasks still match them, so the port and its oracle agree; the list says where a real board might differ.

### Task 2: The contract header, the interpreter and the C harness

**Files:**
- Create: `runtime/include/recomp.h`, `runtime/core/call.c`, `runtime/core/fpu.c`, `runtime/interp/interp.c`, `tests/harness/harness.c`, `tests/charness.py`, `tests/test_interp.py`

**Interfaces:**
- Consumes: `vectors.load`, `vectors.State`, `vectors.Case`.
- Produces, in `recomp.h`:
  - `typedef struct cpu { uint32_t r[32]; uint32_t hi, lo; uint32_t fpr[32]; uint32_t fcsr; uint8_t fcc; uint32_t pc; volatile uint32_t irq_pending; void *rt; } cpu_t;` (`fcsr` holds FCSR without its condition bits; bit `n` of `fcc` is condition code `n`; `pc` is used by the interpreter only).
  - `typedef void (*recomp_fn)(uint8_t *ram, cpu_t *c);` and `#define RAM_SIZE 0x800000u`.
  - Memory, as `static inline`: `mem_r8/r16/r32(ram, c, addr)` returning `uint32_t` (zero-extended; the caller sign-extends), `mem_w8/w16/w32(ram, c, addr, value)`. An access is RAM when `addr` is in `0x80000000`–`0xBFFFFFFF` and `(addr & 0x1FFFFFFF) + size <= RAM_SIZE`; RAM is read with `memcpy`, little-endian. Anything else goes to `bus_read(c, addr, size)` / `bus_write(c, addr, size, value)`.
  - Helpers as `static inline`, one per operation whose C is not a plain operator: `rc_div`, `rc_divu`, `rc_mult`, `rc_multu`, the FP getters and setters (`rc_fs(c, n)` as `float`, `rc_fd(c, n)` as `double`, and their setters), `rc_trunc_w_s`, `rc_trunc_w_d`, the compares, `rc_madd_s` and the rest of that family (two statements, never one expression).
  - Provided by a runtime: `uint32_t bus_read(cpu_t *, uint32_t addr, int size); void bus_write(cpu_t *, uint32_t addr, int size, uint32_t value); void rt_loader(uint8_t *ram, cpu_t *c, uint32_t slot); void rt_break(uint8_t *ram, cpu_t *c, uint32_t pc, uint32_t code); void rt_interrupt(uint8_t *ram, cpu_t *c); recomp_fn recomp_lookup(uint32_t addr);` (`recomp_lookup` returns `NULL` for an address with no recompiled function; `code` is the 20-bit field, bits 6 to 25 of the word).
  - Provided by `runtime/core`: `void rt_call(uint8_t *ram, cpu_t *c, uint32_t addr)` (the recompiled function if there is one, otherwise `interp_run`), `void rt_jump(uint8_t *ram, cpu_t *c, uint32_t addr)` (the same, for a jump that leaves its function), `void rt_set_fcsr(cpu_t *c, uint32_t value)`, `uint32_t rt_get_fcsr(const cpu_t *c)`, and `extern void (*rt_log_unrecompiled)(uint32_t addr);` called once per distinct address the interpreter is entered at.
  - `#define RT_CHECK(ram, c)`: calls `rt_interrupt` when `irq_pending` is non-zero.
  - Provided by `runtime/interp`: `void interp_run(uint8_t *ram, cpu_t *c, uint32_t pc);`
- `interp_run` contract: it fetches from RAM and runs until the function it was entered in returns. `jal`/`jalr` call `rt_call` and continue after the delay slot. `jr ra` ends the run. Any other `jr`, and a `j`, whose target `recomp_lookup` knows is a tail call: `rt_call`, then end the run; otherwise execution continues at the target. A stub-block address reaches `rt_loader` because the generated table maps it (Task 5); the interpreter needs no special case.
- Produces, in `tests/charness.py`: `build(sources: list[str], extra_flags=()) -> str` (compiles with the global flags plus the sanitizer flags, caches by content hash in a temp dir, raises `unittest.SkipTest` when `cc` is missing); `run(exe: str, mode: str, cases: list[Case]) -> list[State]` with `mode` `"interp"` now and `"recomp"` from Task 3. The harness provides its own `bus_*`, `rt_loader`, `rt_break` and `rt_interrupt`, each of which records the call in the result, and a `recomp_lookup` over the functions linked into it.

- [ ] **Step 1: Write the failing tests** in `tests/test_interp.py`:
  - `test_interp_matches_mame_vectors`: for every entry of `insn.json`, the interpreter's after-state equals the recorded one (all registers, `hi`, `lo`, FP halves, FCSR, the scratch page). The failure message names the case and the first differing field.
  - `test_division_edges_do_not_trap`: `div` and `divu` by zero and `INT_MIN / -1` run under the sanitizer build and match the vectors.
  - `test_load_outside_ram_goes_to_the_bus`: `lw` from `0xA8000000` and from `0x00000010`, and `sw` to `0xB6100000`, each produce one recorded bus call with that address and size 4, and no crash.
  - `test_ram_aliases`: a word stored through `0xA0200000` is read back through `0x80200000`.
  - `test_access_straddling_the_end_of_ram`: `lw` at `0x807FFFFE` goes to the bus.
  - `test_call_to_unrecompiled_address_is_interpreted_and_logged`: `jal` to an address the lookup does not know runs that code in the interpreter, returns, and logs the address once across two calls.
  - `test_unsupported_instruction_stops_with_its_address`: a word outside the census (`0x7C000000`) makes the harness exit non-zero with the address and the word in its message.
  - `test_fcsr_round_trip_and_rounding`: after `ctc1` with rounding mode 1, `rt_get_fcsr` returns the value written, and `cvt.s.w` of `0x01000001` rounds toward zero.
  - `test_flush_to_zero`: with FS set, the product of two singles whose exact result is denormal is zero; with FS clear it is the denormal.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_interp`: fails to build.
- [ ] **Step 3: Implement** the header, `call.c`, `fpu.c`, `interp.c`, the harness and `charness.py`. The interpreter covers the 98 census mnemonics plus `cfc1`.
- [ ] **Step 4: Run** the tests: pass, including under the sanitizer.
- [ ] **Step 5: Commit** `git commit -m "runtime: contract header, reference interpreter and C test harness"`.

If the interpreter and a MAME vector disagree and the vector is the one at fault (MAME differs from documented R5000 behaviour in a way that matters), do not bend the interpreter silently: report it, with the case name.

### Task 3: Emit straight-line instructions

**Files:**
- Create: `carnevil_tools/emit.py`, `tests/test_emit.py`
- Modify: `tests/harness/harness.c`, `tests/charness.py` (the `"recomp"` mode)

**Interfaces:**
- Consumes: `mips.Insn`, `mips.decode`, `recomp.h`.
- Produces: `emit.statement(insn: mips.Insn, addr: int) -> list[str]`: the C statements for one instruction that is not a control transfer, over `ram` and `c`. `emit.UnsupportedInstruction(addr, mnemonic)`. `emit.CONTROL`: the set of mnemonics Task 4 handles (branches, jumps, `break`).
- Rules: a write to register 0 is dropped, but a load into register 0 is still performed (the bus may have a side effect). `addi` is emitted as `addiu`. `cache` is emitted as a comment. `ctc1` calls `rt_set_fcsr`.

- [ ] **Step 1: Write the failing tests** in `tests/test_emit.py`:
  - `test_recompiled_matches_mame_vectors`: every straight-line case of `insn.json` is emitted as a function (its words, ending at the `jr ra`), all compiled into one harness binary, and each after-state equals the recorded one.
  - `test_recompiled_matches_interpreter_on_random_states`: for each straight-line mnemonic, 200 random before-states (seeded) give the same after-state in `"interp"` and `"recomp"` mode.
  - `test_write_to_zero_is_dropped`, `test_load_into_zero_still_reads` (one bus call recorded for `lw zero, 0(t0)` with `t0 = 0xA8000000`).
  - `test_madd_is_not_fused`: the `madd.s` case chosen so that fused and unfused results differ gives the unfused bits.
  - `test_unknown_mnemonic_raises`: `statement` on an `mfc0` raises `UnsupportedInstruction` carrying the address.
  - `test_no_undefined_behaviour`: the whole vector run passes in the sanitizer build.
- [ ] **Step 2: Run:** fails on the missing module.
- [ ] **Step 3: Implement `emit.py`** and the harness's `"recomp"` mode.
- [ ] **Step 4: Run:** pass.
- [ ] **Step 5: Commit** `git commit -m "recomp: emit C for straight-line instructions"`.

### Task 4: Emit functions: control flow

**Files:**
- Create: `carnevil_tools/recomp.py`, `tests/test_recomp.py`

**Interfaces:**
- Consumes: `emit.statement`, `functions.Analysis`, `functions.Function`, `functions.JumpTable`, `functions.reached_words`.
- Produces: `recomp.function_c(image, analysis, fn: Function) -> str`: the C for one function, named `fn_<ADDR>` (eight upper-case hex digits) with the analysis name in a comment, signature `void fn_800C4524(uint8_t *ram, cpu_t *c)`. `recomp.RecompileError(addr, reason)`.
- Rules, each with a test below:
  - Only reached words are emitted. Every branch target inside the function gets a label `L_<ADDR>`.
  - A conditional branch evaluates its condition into a local first, then runs the delay slot, then jumps. A branch-likely runs its slot only when taken.
  - A delay-slot instruction that is also a branch target is emitted a second time under its own label on the fall-through path.
  - `jal`: set `ra` to the address after the slot, run the slot, call `fn_<target>` directly. `jalr`: read the target into a local, set `rd`, run the slot, `rt_call`.
  - `jr ra`: slot, then `return`. `jr` with a jump table: `switch` on the register's value with one `case` per distinct target address, `default` calling `rt_jump` and returning. `jr` without one: `rt_jump`, `return`.
  - `j` or a branch to another function's start: slot, a direct call to it, `return`. To an address outside the function that is not a function start: `RecompileError`.
  - A call or jump into a stub block: `rt_loader(ram, c, addr - block_start)`.
  - `break`: `rt_break(ram, c, addr, code)`.
  - `RT_CHECK(ram, c)` at function entry and before every backward jump.
  - A function whose last reached instruction can fall off its end: a direct call to the function starting there, then `return`; `RecompileError` if none starts there.

- [ ] **Step 1: Write the failing tests** in `tests/test_recomp.py`. Each builds a small synthetic image, analyses it with `functions.analyse`, emits it, and compares final state and the recorded runtime calls with the interpreter running the same image, over 100 seeded random before-states:
  - `test_loop_with_backward_branch`, `test_if_else`, `test_branch_likely_skips_slot_when_not_taken` (all ten likely branches of the census).
  - `test_delay_slot_changes_branch_operand`, `test_jal_slot_reads_and_writes_ra`, `test_jalr_slot_overwrites_target_register`.
  - `test_branch_into_delay_slot`.
  - `test_jump_table_becomes_switch` (5 cases, two sharing a target, index out of range hits `default`), `test_unresolved_jr_calls_rt_jump`.
  - `test_tail_jump_to_function`, `test_jump_outside_function_to_non_start_raises`, `test_fall_through_into_next_function`.
  - `test_stub_call_becomes_loader_call` (slot `0x1A8` recorded for a `jal` to `block_start + 0x1A8`), `test_break_passes_code_and_address` (word `0x0007000D` gives code `0x1C00`).
  - `test_rt_check_at_entry_and_back_edge`: with `irq_pending` set by the harness, `rt_interrupt` is recorded at entry and once per loop iteration.
  - `test_bc1_with_condition_code_2`, `test_fp_condition_survives_a_call` (a compare, a `jal` to a function that does not compare, then `bc1t`).
  - `test_branch_cases_match_mame_vectors`: the branch and jump cases of `insn.json`, recompiled, match the recorded after-states.
  - `test_output_is_deterministic`: two runs give identical text.
- [ ] **Step 2: Run:** fails on the missing module.
- [ ] **Step 3: Implement `recomp.function_c`.**
- [ ] **Step 4: Run:** pass, sanitizer build included.
- [ ] **Step 5: Commit** `git commit -m "recomp: emit whole functions with control flow"`.

### Task 5: The whole image: files, CLI, manual entries, link check

**Files:**
- Modify: `carnevil_tools/recomp.py`, `carnevil_tools/functions.py`, `carnevil_tools/cli.py`, `config/game.manual.toml`, `config/game.functions.toml`, `tests/test_recomp.py`, `tests/test_functions.py`, `docs/tools.md`, `.gitignore`
- Create: `runtime/stub/stub.c`, `Makefile`, `tests/test_whole_image.py`

**Interfaces:**
- Consumes: everything above; `exe.load_exe`.
- Produces:
  - `recomp.recompile(image, analysis, out_dir) -> Manifest`: writes `funcs.h` (prototypes), `funcs_000.c` … (64 functions per file, in address order), `func_table.c` (`recomp_lookup`: binary search over a sorted table of address and function, stub slots and interpreted functions included) and, last, `manifest.toml` (`format = 1`, `exe_sha256`, counts of functions, files, stubs, interpreted functions, jump tables, unresolved `jr`). Output is written to a working folder created next to `out_dir` under a unique hidden name and renamed into place when complete; an existing `out_dir` is replaced only when it holds a `manifest.toml`, and nothing the run did not create is deleted.
  - CLI: `python3 -m carnevil_tools recompile <GAME.EXE> -o <dir> [--functions config/game.functions.toml]`. It refuses an EXE whose SHA-256 is not the functions file's `exe_sha256`, naming both.
  - Manual file, two new kinds of entry, each with a `reason`: `[[jump_table]]` (`jr_addr`, `targets` list) for a `jr` the analysis cannot resolve; `[[interpreted]]` (`addr`) for a function left to the interpreter, emitted as a thunk calling `interp_run(ram, c, addr)`.
  - `functions.FORMAT = 2`: `Analysis` gains `interpreted: list[int]` (default empty) after its existing fields; a manual jump table is a `JumpTable` with `table_addr = 0`. `read_toml` accepts format 2 only and says so.
  - Stub slot thunks: a function with source `stub` is emitted as `rt_loader(ram, c, addr - block_start)`.
  - `Makefile`: `make recomp` (runs `recompile` into `$(CARNEVIL_WORK)/generated`), `make linkcheck` (compiles the generated files in parallel and links them with `runtime/core`, `runtime/interp` and `runtime/stub/stub.c` into `$(BUILD)/game_stub`; fails without `manifest.toml`), `make test`.
  - `runtime/stub/stub.c`: `bus_read` returns 0 and both bus functions log the address once; `rt_loader`, `rt_break` and `rt_interrupt` log their arguments and return.

- [ ] **Step 1: Write the failing tests.**
  - In `tests/test_recomp.py`, on a synthetic image: `test_recompile_writes_manifest_last_and_no_partial_dir`; `test_unsupported_instruction_leaves_no_output` (an image with `mfc0` in a function: `RecompileError` names the address, `out_dir` does not exist afterwards); `test_wrong_exe_hash_is_refused` (CLI exit status 1, both hashes in the message); `test_lookup_table_is_sorted_and_complete`; `test_interpreted_function_is_a_thunk`; `test_manual_jump_table_is_used`; `test_synthetic_image_links_and_runs` (compiled with the stub runtime, entered through `recomp_lookup`, returns the expected register).
  - In `tests/test_functions.py`: `test_format_1_file_is_refused_with_a_clear_message`, round trip with `interpreted` and a manual jump table.
  - In `tests/test_whole_image.py` (game data and `cc`): `test_whole_image_recompiles` (no error; the manifest's function count equals the functions file's; unresolved `jr` count equals the number documented in `recompiler.md`); `test_whole_image_compiles_and_links` (runs `make linkcheck` with a temp build dir; zero undefined symbols; skipped unless `CARNEVIL_SLOW=1` if it takes more than 2 minutes on the Mac, in which case say so in the report); `test_recompile_is_deterministic` (two runs, identical bytes).
- [ ] **Step 2: Run:** fail.
- [ ] **Step 3: Implement** the driver, the CLI, format 2, the stub runtime and the `Makefile`.
- [ ] **Step 4: Make the real image go through.** Run `recompile` on `GAME.EXE` and resolve every `RecompileError` with an entry in `config/game.manual.toml`, each with a reason citing evidence:
  - The 2 switches whose tables are known (`jr` at `0x8012DCA8`, table `0x80174328`; `jr` at `0x80150174`, table `0x801757A8`, 5 entries): `[[jump_table]]` entries, targets read from the tables.
  - The 14 label-table dispatches in the block-copy routines (`0x80164484` … `0x801652F8`): read each with `disasm`, and give each a `[[jump_table]]` whose targets are the labels of its own 28-byte tail. Where the targets cannot be established, leave the `jr` to `rt_jump` and list it.
  - The 2 `j` to `0x80151E90` from the hand-written save and restore code (`0x80151AC0`–`0x80152008`): a manual start at `0x80151E90` if the result recompiles cleanly, otherwise `[[interpreted]]` for the functions involved.
  - Do not add starts for the 7 probable callbacks or the 141 unreferenced leaves: if they are ever called, the interpreter runs them and logs the address, which is the evidence a manual entry needs.
  Regenerate `config/game.functions.toml` and update every count that moves in `docs/analysis/functions.md` and `docs/analysis/M0-report.md`.
- [ ] **Step 5: Run** the whole suite: pass. Record the generated line count, the compile time on the Mac, and the size of `game_stub`.
- [ ] **Step 6: Commit** `git commit -m "recomp: recompile the whole image, manual jump tables, link check"`.

### Task 6: Check real game code against MAME

**Files:**
- Create: `runtime/stub/smoke.c`, `runtime/stub/replay.c`, `tools/mame/firstcall.lua`, `tools/mame/funcreplay.lua`, `tests/test_against_mame.py`
- Modify: `config/boot_state.toml`, `Makefile`, `tools/mame/README.md`, `carnevil_tools/cli.py`

**Interfaces:**
- Consumes: `game_stub`'s objects, `config/boot_state.toml`, the RAM dump at `GAME.EXE`'s entry from M0 (`$CARNEVIL_WORK/mame/ram-at-game-entry.bin`).
- Produces:
  - `config/boot_state.toml` gains `[first_loader_call]`: `slot`, `a0`, `a1`, `a2`, `a3`, `ra`, `sp`, from `firstcall.lua`, which also saves RAM at that moment (`ram-at-first-loader-call.bin`, on the share).
  - `smoke <ram-at-entry> <boot_state values>`: loads the 8 MB dump, sets the registers of `boot_state.toml`, calls `fn_800C4000`, and at the first `rt_loader` call writes the slot, the registers and the 8 MB of RAM, then exits 0. Any bus access, `rt_break` or interpreter entry before that is printed.
  - `python3 -m carnevil_tools pure-functions [--functions …]`: the functions whose call closure has no stub call, no hardware access (`hwcensus.hardware_accesses`), no `break`, no `jalr` and no unresolved `jr`. One address per line.
  - `funcreplay.lua`: given that list, sets a breakpoint on each during attract mode; at the first hit of each it saves the registers and RAM, runs to `ra`, saves again, and drops the capture if any exception was taken in between (a breakpoint on the general exception vector, `0x80000180`), since the handlers write RAM. Captures go to `$CARNEVIL_WORK/mame/replay/<ADDR>/`.
  - `replay <capture dir>`: loads the before-state, calls the function through `recomp_lookup`, and compares all general registers, `hi`, `lo`, the FP halves, the condition codes and all 8 MB with the after-state. It prints the first differences and exits non-zero on any.

- [ ] **Step 1: Write the failing tests** in `tests/test_against_mame.py` (game data, `cc`, and the captures; each skips when its files are missing):
  - `test_smoke_reaches_the_first_loader_call`: slot and `a0`–`a3`, `ra`, `sp` equal `[first_loader_call]`; no bus access, break or interpreter entry was printed before it.
  - `test_ram_at_first_loader_call_matches_mame`: the smoke program's RAM equals `ram-at-first-loader-call.bin` byte for byte from `0x800C4000` up to the stack pointer at the call. Interrupts run in MAME during that stretch and the loader's handlers write its own data and the stack below `sp`, so those are outside the comparison; `firstcall.lua` records whether an exception was taken.
  - `test_pure_functions_replay`: every capture directory replays with no difference, and there are at least 20 of them.
  - `test_pure_function_list_excludes_stub_callers` (no game data needed beyond the functions file): `main` (`0x800C4524`) is not in the list.
- [ ] **Step 2: Run:** fail or skip for want of captures.
- [ ] **Step 3: Implement** the two programs, the CLI command and the two Lua scripts; run the scripts in the Linux container (`-nodrc`).
- [ ] **Step 4: Run** the tests: pass. A replay difference is a recompiler or interpreter bug until shown otherwise: find the first differing instruction by replaying the same capture through `interp_run`, fix it, and add the instruction pattern as a case to `vectors.py` or a test in `test_recomp.py`.
- [ ] **Step 5: Commit** `git commit -m "recomp: smoke run to the first loader call and function replay against MAME"`.

If fewer than 20 pure functions are hit in 90 s of attract mode, report the number reached and which functions; do not pad the list with functions that fail the purity rule.

### Task 7: M1 report and documentation

**Files:**
- Create: `docs/analysis/M1-report.md`, `docs/analysis/recompiler.md`
- Modify: `docs/README.md`, `docs/how-it-works.md`, `docs/tools.md`, `docs/getting-started.md`, `docs/roadmap.md`, `README.md`, `docs/superpowers/plans/2026-11-19-carnevil-recomp.md` (M1 marked done, pointer to this plan)

- [ ] **Step 1: Write `docs/analysis/recompiler.md`:** what the generated C looks like (described, with a made-up three-line example, never a real function), the contract in `recomp.h`, how delay slots, likely branches, jump tables, tail jumps, stubs and breaks are handled, the manual entries added and why, what is left to the interpreter, the `MAME_SPECIFIC` cases.
- [ ] **Step 2: Write `docs/analysis/M1-report.md`:** the numbers (functions, files, lines, compile time, vectors, replayed functions), what was checked against MAME and what was not (gameplay code is still unexercised), every departure from this plan, and what M2 needs from here: the runtime entry points to implement, the first loader call, the list of interpreted functions and unresolved jumps.
- [ ] **Step 3: Update the human guides** so a reader can go from their own `GAME.EXE` to `make linkcheck`, and the roadmap shows M1 done. Credit N64Recomp for the output shape in `how-it-works.md`.
- [ ] **Step 4: Run** the whole suite once more, both with and without game data: pass.
- [ ] **Step 5: Commit** `git commit -m "docs: M1 report, recompiler notes, guides"`.

---

## Exit check for M1

- `make recomp` writes the generated C for all of `GAME.EXE`, and `make linkcheck` links it with zero undefined symbols.
- The interpreter and the generated C both match every MAME instruction vector.
- The recompiled `_start` reaches the first loader call (slot 0x038, which happens before `main`) with MAME's registers and MAME's RAM. (Amended and accepted: `main` cannot be entered without a stand-in for that loader service, which is M2's.)
- At least 20 real game functions replay with MAME's results.
- Every function left to the interpreter and every unresolved jump is listed in config with a reason.
