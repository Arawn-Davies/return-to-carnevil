# M1 report: the recompiler

Milestone M1 (19 November 2026) built the recompiler: a Python emitter that turns every
function of `GAME.EXE` into C, a C reference interpreter that shares the generated code's
contract, and the checks of both against MAME. This page is the summary to plan M2 from. How
the generated code works is in [recompiler](recompiler.md); the plan it reports against is
`docs/superpowers/plans/2026-11-19-carnevil-recomp-m1.md`.

Every fact below carries its basis, as in the [M0 report](M0-report.md):

- **static**: measured by the committed tools over the files;
- **read**: read from the code by hand with the `disasm` command;
- **test**: checked by the test suite (`python3 -W error -m unittest`);
- **MAME**: measured in MAME 0.276 (run headless in a Debian container) by the script named.
  All MAME runs cover boot and attract mode only, with no coin or input;
- **inferred**: a conclusion from the above, not measured.

## The numbers

- **Functions**: 2,093 C functions, one per entry of `config/game.functions.toml`: 2,064 of the
  game's own and 29 loader-slot thunks. 2 of the game's are thunks into the interpreter
  (0x80151AC0, 0x80151B50). 91 jump tables (1,619 entries; 75 found by the analysis, 16 from the
  manual file), 0 `jr` left to `rt_jump` (static, `make recomp`).
- **Output**: 33 files of up to 64 functions plus `funcs.h`, `func_table.c` and `manifest.toml`;
  248,659 lines, 7,987,844 bytes of C. Two runs give identical bytes (static, test).
- **What the generated C calls**: `rt_call` at the 64 `jalr` sites, `rt_loader` at 279 call
  sites plus the 29 thunks, `rt_break` at 38 sites, `rt_jump` only in the 91 `switch` defaults,
  `interp_run` in the 2 thunks, and 3,159 interrupt check points (static, counted in the
  output).
- **Time and size**: `make recomp` takes 0.9 s (to a local disk). `make linkcheck` from
  clean takes 3.8 s wall (28 s of CPU) on the Mac (Apple clang 21, arm64, 10 cores) and 27.1 s with GCC 14.2 in a
  4-core Debian container, with zero warnings under `-Wall -Werror` on both; an up-to-date
  rerun takes 0.04 s. `game_stub` is 4,806,904 bytes on the Mac (`__TEXT` 4,620,288) and
  3,570,992 bytes on Linux x86-64 (text 3,445,765) (static, measured now).
- **Instruction vectors**: 1,369 cases from `carnevil_tools/vectors.py`, 12 random ones for
  each of the 98 mnemonics of the census plus 161 edge cases and 32 flush-to-zero cases (16
  with FCSR.FS = 1, each with an FS = 0 control). 1,352 ran in MAME (`tools/mame/insnvec.lua`,
  35 s) and are committed as `tests/vectors/insn.json` (973,468 bytes, synthetic words and
  values only): 1,112 straight-line and 240 branch, jump and call programs. The other 17 have
  no MAME result (below). 52 recorded cases are listed as MAME-specific (MAME, test).
- **Replayed game functions**: 787 functions are statically pure by the census; 781 could be
  armed in MAME, 156 were hit in 90 emulated seconds of attract mode, 152 were captured
  (`tools/mame/funcreplay.lua`, 960 s wall). 127 replay with MAME's results; 25 are impure at
  run time (MAME, test).
- **Tests**: 201 at the start of M1; 309 now, all run with game data and the MAME captures;
  without them 303 run, 27 of them skipped (a skipped `setUpClass` counts as one) (test).

## What was checked against MAME, and how strong it is

### Instruction programs

Each of the 1,352 recorded cases is a short program (the instruction, or a branch layout that
marks which path ran, then `jr ra`) run on MAME's R5000 core from a chosen state. The
interpreter and the generated C each reproduce every one exactly: all general registers, `hi`,
`lo`, the 32 FP halves, FCSR and the scratch page (test). On top of that, the generated C is
compared with the interpreter on 200 random states for each of the 77 straight-line
mnemonics, on 3,874 operand-aliasing forms (destination equal to a source, register 0, double
pairs overlapping singles), and on 100 random states for each synthetic control-flow shape of
the plan's review focus: delay slots that change the branch operand or `ra`, branches into delay
slots, likely branches taken and not taken, jump tables, tail jumps, fall-through, loader
calls, `break` and the interrupt check points (test). The test harness runs under
`-fsanitize=undefined`.

How strong: every mnemonic's semantics is pinned on the operands chosen, edge values included
(0, 1, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF, shifts of 32 and more, 3e9, NaN, denormals). 52
of the recorded results are MAME's behaviour rather than an established R5000 fact (division
by zero, `trunc.w` of a NaN or an out-of-range value, three multiply-add results where one
rounding would differ from MAME's two, `recip.s`, and the 16 cases with FCSR.FS = 1;
[recompiler](recompiler.md) lists them).

The game runs with FCSR.FS = 1 (flush to zero) from `_start` on (read). MAME 0.276 ignores FS:
in all 16 FS = 1 cases it kept the denormal result, or used the denormal operand as it is,
exactly as in the FS = 0 control (MAME). The port therefore follows MAME and does not flush
by default (`RT_HONOUR_FS` = 0 in `runtime/core/fpu.c`; set to 1, it flushes as the R5000
documents, and a test checks that build too) (test). Matching those means
the port agrees with its oracle, not necessarily with the board. That matters because the
game would see the difference: on the board, an out-of-range `trunc.w` may raise an
Unimplemented Operation exception, and the game registers an FPU handler for exactly that cause
(0x80150860, [boot-handoff](boot-handoff.md)), whereas MAME returns 0x80000000 and the handler
never runs (inferred).

### From the entry to the first loader call

`smoke` loads MAME's RAM dump at `GAME.EXE`'s entry (`tools/mame/handoff.lua`), sets the
registers of `config/boot_state.toml` and calls the recompiled `_start`. It reaches its first
loader call at slot 0x038 with all 32 general registers, `hi` and `lo` equal to MAME's
(`tools/mame/firstcall.lua`, recorded in `boot_state.toml` [first_loader_call]), and with RAM
from 0x800C4000 up to sp identical to MAME's byte for byte. The port made no bus access,
`break`, interrupt or interpreter entry on the way (test). `lo` matches because its value at
the entry, a loader leftover, is now in `boot_state.toml` too. FP halves 0 and 8 are also
leftovers at the entry; they are neither set nor compared. Outside the compared range 38 bytes
differ: 37 in the loader's exception frame and data
(0x80014332-0x80014D8C) and one in the debug monitor's area (0x800B89D2) (test, printed and
not asserted).

MAME took two IP2 interrupts on the way, in `_start`'s clear loop. A write watchpoint on
0x800C4000-0x807FFFFF that fired only for code below `GAME.EXE` logged no write, so the
handlers did not touch the compared range (MAME, `firstcall.lua`).

How strong: one path, with 0x73BE98 bytes of RAM compared (7,585,432 bytes: 7.6 MB, 7.2
MiB). It covers `_start` (the BSS clear, the
copy of the slot table) and its first two calls (0x80143EB8, and 0x801412D0 down to
0x80145318). The first loader call comes before `main` starts, so `main` itself is not covered
by this check (static: no direct call path leads from `main` to 0x80145318).

### Function replay

`funcreplay.lua` set a breakpoint on each statically pure function and, at its first hit in
attract mode, saved the registers and the 8 MB of RAM, ran to the return and saved them again;
an attempt during which an exception was taken was dropped and retried (3 attempts dropped, all
of 0x800E8510, never captured). `replay` loads the before-state, calls the function through
`recomp_lookup` and compares every general register, `hi`, `lo`, the FP halves, the FCSR and
all 8 MB with MAME's after-state.

- **127 match MAME** over every register and all 8 MB (test). Of these, 117 have an observable
  effect, 5 change nothing at all and 5 change only `at`. In their own code, 79 have
  conditional branches, 53 make calls, 32 use the FPU and 8 hold a jump table (static, counted
  now from the captures and the functions file).
- **25 are impure at run time**: they reach hardware through pointers held in memory or passed
  as arguments, which the static census cannot see. 21 touch Voodoo registers, 3 Voodoo
  texture memory and 1 the CMOS (0x801458C0, the CRC loop M0 found), counted by the region of
  the first bus access; "New facts" below counts by kind of routine (22, 2 and 1), and the one
  that moves is 0x80163864, a Glide routine whose first access is to texture memory. `replay` stops at the
  first bus access; the test accepts a capture as impure only when that address is a known
  hardware region and `replay --interp` (every call in the interpreter) makes the identical
  access, with the same written value, from the same registers and the same RAM (a 64-bit hash
  of the 8 MB). For these 25 the only evidence is that the recompiled C and the interpreter
  agree up to the first hardware access. That is not a match with MAME (test).
- **No undefined behaviour on the way.** `replay` built with `-fsanitize=undefined
  -fno-sanitize-recover=undefined` over the whole generated image and the runtime gives the
  same verdicts on all 152 captures, with no sanitizer report: the whole-image build takes
  about 9 s on the Mac and the 152 runs (plus the 25 `--interp` runs) about 8 s (test).

How strong: 127 real functions, from short leaves to code with loops, calls, FP and switches,
agree with MAME on everything they leave behind. The impure rule has a hole: a recompiler bug
that turned a RAM address into a known hardware address in exactly these functions would be
classed as impure; the interpreter cross-check closes it only for a bug the interpreter does
not share.

### What was not checked

- **No gameplay code beyond attract mode.** Every MAME run had no coin and no input.
- **No interrupt delivery.** The check points are tested on synthetic code; `rt_interrupt` is
  a stub and no interrupt handler of the game has run in the port.
- **Nothing that touches hardware**, and no loader service: the stub bus returns 0 and the
  stub `rt_loader` records the slot.
- **No FP exception.** The vectors keep FCSR's enables clear, so the divide-by-zero trap the
  game enables (below) was never compared: MAME takes it, the port returns the IEEE result.
- **No task switch.** No function that yields has run end to end, in either mode.
- **The 7 probable callbacks and the 141 unreferenced leaf-shaped ranges** are not recompiled.
  If one is ever called, the interpreter runs it and logs the address.
- **GCC and x86-64**: the whole image compiles and links with GCC 14.2 without a warning, and
  the interpreter built with GCC gave byte-identical results to the Mac build on the vector
  cases when checked by hand during the work. The suite itself runs on the Mac only, so no
  game code has run on x86-64 yet.

## Departures from the plan

Each was ruled on during the milestone; the plan was corrected where its text was simply
wrong.

- **The census has 98 mnemonics, with 8 likely branches.** The plan said 105 and ten. The list
  in `functions.md` sums to exactly its 165,638 reached words (165,760 since the manual tables),
  so 98 is complete; the plan had miscounted.
- **`break` and `div` of INT_MIN by -1 have no MAME vector.** `break` raises an exception in
  MAME, so its case cannot reach `ra`; the port's handling (`rt_break` with the 20-bit code,
  then the next instruction) is checked between the generated C and the interpreter only.
  MAME divides on the host, and x86-64 faults on INT_MIN / -1 (the first run died of SIGFPE).
  The port gives `lo` = 0x80000000 and `hi` = 0, our reading of the R4000/R5000 manuals, with a
  guard so the host never traps. On arm64 the guard cannot be seen to work (its divide never
  traps); it was shown on x86-64 by hand.
- **`-frounding-math` was added to the C flags** for every file, so that the compiler does not
  fold FP expressions as if the rounding mode were fixed once `ctc1` can change it.
- **FCSR.FS does not flush by default.** The plan had `rt_set_fcsr` set host flush-to-zero
  from FS, as the R5000 documents. MAME does not model FS (the FS = 1 vectors keep denormals),
  and every later milestone is checked against MAME, so the port follows it: host
  flush-to-zero is behind one compile-time switch, `RT_HONOUR_FS` in `runtime/core/fpu.c`
  (0, the default, follows MAME; 1 flushes). The FS = 1 cases are listed as MAME-specific.
- **The bits of a generated NaN are the host's** (0x7FC00000 on arm64, 0xFFC00000 on x86-64;
  the R5000's default is 0x7FBFFFFF). Accepted and documented in `recomp.h`: canonicalising
  would cost a branch per FP operation, for bits the game would have to inspect as integers.
- **Helpers only where C has no plain operator.** Signed shifts and compares, sign extension,
  FP addition, subtraction, multiplication and negation, `recip.s` and the conversions stay
  inline in both the emitter and the interpreter. FP divide alone goes through a helper
  (`rc_div_s`, `rc_div_d`), still a plain `/`, as the seam for M2's divide-by-zero trap. The plan's wording allowed this; a broader reading during the work would
  have wrapped single operators in helpers for no gain.
- **One interrupt check-point rule.** The plan put `RT_CHECK` at entry and before every
  backward jump. Applied literally, a backward jump to another function's start checked twice
  (before the call and at the callee's entry). The rule is now: at function entry, and before a
  backward transfer that stays inside the function; never at a transfer that leaves through a
  call. The interpreter follows the same rule. The counts of the two modes still differ by one
  check where the interpreter runs on inline into another function's start that the C calls (a
  branch to it, running off the end, a jump to a start the interpreter's lookup does not
  know), as `recomp.h` documents. An interrupt is only ever delayed to the next check point.
- **Stub thunks from the start.** The function emitter itself, not the whole-image step, emits each loader
  slot as a thunk calling `rt_loader`, so the interpreter reaches the loader through the same
  lookup and the two modes can be compared on loader calls.
- **A conditional branch into the stub block is a recompile error** instead of a loader call:
  the interpreter would run the zero words there, so the two modes would differ. Reached code
  has none.
- **The output folder is replaced through temporary folders next to it** (unique hidden names),
  not `<out_dir>.partial` and `.old`. The first version deleted whatever was at those two
  names; now nothing the run did not create is ever deleted, except an output folder holding a
  `manifest.toml`, by replacement.
- **The manual file is validated.** A `[[jump_table]]` must name a `jr` on a register other
  than `ra` in a function's reached code, its targets must lie inside that function, a table
  for a `jr` the analysis resolves itself is refused, and a missing or unknown key names the
  entry. The plan only required a `reason`.
- **The unresolved-`jr` count test reads `functions.md`**, not `recompiler.md`: the test was
  written before this note, and the count is an output of the analysis, which
  `functions.md` documents ("Unresolved indirect jumps (0)").
- **Purity has a run-time half.** The plan's rule was static only. The static census cannot
  see hardware reached through pointers, so 25 captured functions touched the Voodoo or the
  CMOS. Instead of a hand-kept `[[impure]]` list or a new taint analysis, a capture whose replay
  makes its first bus access to a known hardware region, identically in both modes, counts as
  impure at run time and is reported, not compared. A bus access anywhere else is a failure.
- **kuseg is RAM from 0x00040000 to 8 MB.** The plan's memory model knew only kseg0 and kseg1.
  31 replays failed until the loader's TLB mapping was modelled (below).
- **`boot_state.toml` gained every leftover register at the entry**, not only
  `[first_loader_call]`: without them a3 differed at the first loader call.
- **The first loader call comes before `main` (accepted amendment).** The exit check said "the
  recompiled `_start` and the start of `main`"; the call is in `_start`'s second callee, so the
  check covers `_start` only. The amended bullet, accepted by the owner and now in the plan:
  the recompiled `_start` reaches the first loader call (slot 0x038, before `main`) with
  MAME's registers and RAM. `main` cannot be entered without a stand-in for that service (M2).
- **Smaller ones**: the RAM dumps the tests read are under `mame/dumps/` on the share, where
  M0's dump already was; `funcreplay.lua` retries a function up to 3 times after a dropped
  attempt, where the plan said first hit; `replay` gained `--interp`, and so provides its own
  `rt_call` and `rt_jump`; `recomp.h` gained `interp_forget_entries()` for test harnesses that
  run many cases in one process; the whole-image link check is fast enough (under 5 s on the
  Mac) not to need `CARNEVIL_SLOW`.

## New facts about the game

- **The loader maps kuseg onto RAM.** Its routine at 0x80004160 writes 16 TLB entries of two
  256 KB pages mapping virtual 0x00000000-0x007FFFFF onto the same physical addresses, with the
  first page invalid; `GAME.EXE` writes no TLB entry (read). 31 game functions store through
  such addresses (0x00237xxx and 0x00317xxx-0x0031Fxxx) in attract mode (MAME captures, counted
  now by replaying them without the mapping). Where those pointers come from was not traced.
  [boot-handoff](boot-handoff.md), "kuseg is mapped onto RAM".
- **The first loader call is slot 0x038**, from 0x80145318 under `_start`'s call of
  0x801412D0, before `main`, with a0 0x80174EC0, a1 0, a2 0x807FFEC0, a3 0x000E0228 (a loader
  leftover passed on), ra 0x80145350, sp 0x807FFE98 (MAME, `firstcall.lua`; read for the
  caller). [boot-handoff](boot-handoff.md), "The first loader call".
- **The loader's leftover registers at the entry** (17 non-zero general registers besides sp
  and ra) are in `config/boot_state.toml` (MAME, four identical runs).
- **All 16 formerly unresolved indirect jumps are resolved** by manual tables read from the
  code, each with its index bound and base register established by reaching definitions
  (read; checked against the binary in review). [functions](functions.md), "Manual jump tables".
- **The game runs a cooperative task scheduler** in hand-written code; the next section
  describes it.
- **25 statically pure functions reach hardware through pointers**: 22 Glide and sst1 routines
  through a Voodoo base held in memory, 2 memory-fill or flush routines writing texture memory
  through kseg0, and the CMOS CRC loop (MAME captures).

## What M2 needs from here

- **The runtime entry points** of `recomp.h`, each specified there in a comment block (what
  it is called with, what it must leave, what it may call):
  - `bus_read` and `bus_write`: the Voodoo (registers, frame buffer, texture memory, through
    kseg0 and kseg1), the gun, the sound FIFO, the CMOS reads, 0x17F00000, and IOASIC
    SOUNDSTAT (M0 report, "Hardware"). kuseg RAM is already handled before the bus.
  - `rt_loader(ram, c, slot)`: the stand-ins for the 29 slots (M0 report, decision 1, option
    a). The first one the game reaches is 0x038, whose meaning is still to be read.
  - `rt_break(ram, c, pc, code)`: code 0x1C00 (`break 7`, divide by zero) and 0x1800 (`break
    6`, overflow) call the hooks the game registers; 0x409 installs an exception handler; 0x407
    is an assert.
  - `rt_interrupt(ram, c)`: dispatch to the handlers the game registered, with the loader's
    ABI ([boot-handoff](boot-handoff.md), "Exceptions and interrupts"). The runtime sets
    `irq_pending`.
  - `recomp_lookup` is generated. `rt_call`, `rt_jump`, the FPU control and the interpreter
    are done.
- **The state to start from**: `config/boot_state.toml` (entry registers, leftovers, Status
  and FCSR) and the first loader call to check the first stand-in against.
- **Interpreted functions**: 0x80151AC0 and 0x80151B50, the task switch (below). Both are
  listed with their reason in `config/game.manual.toml`.
- **Unresolved jumps**: none. `rt_jump` is reached only by an out-of-range index in one of the
  91 switches.
- **Not recompiled, on purpose**: the 7 probable callbacks (0x800C6F28, 0x800FE924,
  0x800FEDB0, 0x80101CD8, 0x80102454, 0x801150C8, 0x801478C0) and the 141 unreferenced leaf
  ranges ([functions](functions.md)). The interpreter's log will name any that run.

## Risks and decisions for M2

These are options with a recommendation each, not decisions already taken.

### 1. The game's cooperative task switch

What it is (read, unless marked):

- `main` calls the scheduler 0x80151BC0 once (0x800C4740). It loops forever: each pass does
  some work that is probably per frame (inferred), including up to two `ioctl` calls on fd 5
  (commands 0x7403 and 0x7407, which are probably the `timer:` set of the M0 report;
  inferred), then walks the task list (head at 0x801A3110, current task
  at 0x801A3118). A task record holds the next task (+0), its saved stack pointer (+8), a
  halfword sleep counter (+0xC), an argument (+0x10) and its function (+0x24). For each task
  whose counter is positive the scheduler decrements it, and when it reaches 0 resumes the
  task: it saves its own registers (the general ones but `k0`, `k1`, `gp` and `sp`, and 22 of
  the 32 FP halves: the even ones and 21 to 31) below its stack, loads the task's
  saved `s0`-`s7`, `fp`, `ra` and `f20`-`f31` from the task's stack, swaps `sp` and `k1`, and
  returns into the task with `jr ra` (0x80151E88).
- 0x80151AC0 is the yield, called from 70 `jal` sites in 32 functions (static). It ORs its
  argument (read as the number of scheduler passes to sleep; inferred) into the current task's
  counter, swaps `sp` and
  `k1` (so `sp` is the scheduler's stack again), saves the task's callee-saved registers just
  below the task's stack pointer, stores that stack pointer at +8 and jumps to 0x80151E90, the
  scheduler's restore path, which reloads the scheduler's registers and moves to the next
  task. The caller sees the `jal` return passes later, with its callee-saved registers intact.
- A task is created by 0x80148FA4 or 0x80149200 (static: the only two places that build the
  address 0x8014A930). They put 0x8014A930 where the restore path reloads `ra`, so the first
  resume of a new task enters 0x8014A930, which calls the task's function with its argument
  and, if it returns, jumps to 0x80151B50 (from its one `j` at 0x8014A950). 0x80151B50 calls
  0x80149C58 on the current task, swaps `sp` and `k1` and jumps to the restore path without
  saving anything: the task's exit.
- While a task runs, `k1` holds the scheduler's stack pointer. So `k1` must survive every
  interrupt delivered while a task runs (inferred). The loader's general exception entry uses
  only `k0` in its first instructions; whether its dispatcher ever writes `k1` was not traced.
- MAME run 3 (`tools/mame/gamerun.lua`, M0) logged 7 entries to 0x8014A930 in 150 emulated
  seconds of attract mode, on 6 distinct stack pointers (MAME): at least 6 task stacks, one
  reused.

What is not known: how many tasks exist at once (in attract mode and in play) and how many
yields a frame makes; the size of each task stack and the deepest call chain on it; what
0x80149C58 and the creation routines do with the list; whether interrupt handlers or loader
services wake tasks or touch the list (the scheduler's own counters suggest not, but nothing
was traced); the per-frame work's dependence on the `timer:` ioctls.

Why the M1 output cannot run it: recompiled code keeps the game's call chain on the host stack.
When the scheduler "returns" into a task with `jr ra`, the C code returns to whatever C frame
called the scheduler, not into the task's C frames, which were left when the task yielded. The
interpreter as written has the same limit: it ends a run at `jr ra`, and each guest call it
makes through `rt_call` is a host call too.

The options:

- **(a) One host stack per game task.** The runtime gives each task a host stack of its own
  and switches host stacks where the game switches guest stacks. 0x80151AC0 and 0x80151B50
  become native stand-ins that do what the guest code does to guest state (the counter, the
  saved registers on the guest stack, the record's +8) and then switch to the scheduler's host
  stack; the resume path in 0x80151BC0 (0x80151D4C to 0x80151E8C) becomes a native stand-in
  that switches to the task's host stack, creating it on the first resume, where it calls
  0x8014A930 through `rt_call`. That needs a manual start or a hand-written replacement for the
  scheduler, since the resume path is inside it. The switch itself is a few instructions per
  host: `ucontext` (deprecated on macOS but present), or a small hand-written switch for arm64
  and x86-64 (macOS arm64, Linux x86-64, PS4 Linux x86-64), which is what fiber libraries do.
  Cost: two host stacks' worth of design (stack size per task, guard pages), sanitizer and
  debugger support for switched stacks (UBSan does not mind; ASan needs annotations), and a
  native stand-in for hand-written code that must keep exactly the game's guest-visible
  effects. A switch costs tens of nanoseconds.
- **(b) One host thread per game task, with a baton.** The same stand-ins, but each task is a
  host thread and only the thread holding the baton runs; a yield passes the baton back. Fully
  portable (pthreads) and easy to debug, at about a microsecond or more per hand-over instead
  of nanoseconds, and with thread start-up per task. With a few tasks yielding once a frame
  that is affordable; with many yields per frame on the PS4's slow cores it may not be.
  Threads also make every piece of runtime state shared, though only one thread runs at a time.
- **(c) Run everything that can yield in the interpreter.** This works only if the interpreter
  keeps the whole guest call chain in guest state, without a host call per guest call, so that
  switching `sp` and returning with `jr ra` just continues somewhere else; the interpreter
  would need that mode. Then every function that can be on a task's stack at a yield must be
  interpreted: at least the 54 functions that reach 0x80151AC0 by direct calls and jumps (6,692
  reached words, about 4% of the code; static), plus whatever reaches them through pointers,
  starting with every task function, which 0x8014A930 calls through `jalr`. Those are the game
  logic tasks, so this puts a large and hot share of the game in the interpreter, and a single
  missed caller fails at run time. Viable as a stop-gap to get M2 booting, not as the design.
- **(d) Transform the recompiled code**, so that every function that can yield is re-entrant:
  on a yield it returns up the C chain recording where each frame was, and on the resume it is
  re-entered and jumps back to that point (a state machine per function, or continuation
  passing). Guest registers already live in `cpu_t`, so the C frames hold little, which makes
  this less heavy than in general. But it needs the complete set of functions that can yield,
  including those reached only through pointers, which the static analysis cannot give; every
  call site in them gains a check; and a mistake is a silent wrong resume. Heavy and fragile
  for a mechanism with two entry points.

**Recommendation: (a), behind an interface that (b) can also implement.** It confines the
change to the two interpreted functions and the scheduler's resume path, keeps all game code
recompiled, and costs almost nothing per switch. (b) is the fallback where a host makes stack
switching awkward. Before writing it, measure in MAME (no new run was made in M1): a script
like `gamerun.lua` with breakpoints on 0x80151AC0, 0x8014A930, 0x80151B50 and the scheduler's
resume at 0x80151D4C that logs, per frame, the yields and the distinct task records (the word
at 0x801A3118) and stack pointers seen, the minimum `sp` reached on each task stack (from the
stack's top at creation), and whether `k1` ever differs from the scheduler's `sp` when an
interrupt is taken; once in attract mode and once with scripted coin and start inputs. That
gives the number of tasks, the yield rate and the stack sizes the design needs.

**A reviewer's alternative way to build (a)** (not a decision): keep the yield, the exit and
the scheduler recompiled, and turn only the three guest transfers between them into host
switches. The `j 0x80151E90` at 0x80151B48 (in the yield) and at 0x80151B78 (in the exit)
become a switch to the scheduler's host context; the `jr ra` at 0x80151E88 (the resume, with
the third `xor` in its delay slot) becomes a switch to the task's host context, created on the
first resume with `rt_call(0x8014A930)` (read). The scheduler's context then carries on at
0x80151E90, which is the instruction after that `jr ra` and its slot, so the yield's guest
saves and the scheduler's guest loads all run as recompiled code on the shared `cpu_t`, and
nothing hand-written has to reproduce guest effects. It needs a new kind of manual entry
(a transfer that is a host switch). The considerations it brings, whichever way (a) is built:

- the host FP control is per context (glibc `swapcontext` saves and restores MXCSR; each
  thread under (b) has its own FPCR or MXCSR), so every switch must be followed by
  `rt_set_fcsr(c, rt_get_fcsr(c))`, or a `ctc1` in one task is silently undone in another;
- `rt_interrupt` runs on whichever context is current, so interrupt handlers must not yield;
- the yield's guest saves land below the task's `sp` without moving it, so no stand-in and no
  interrupt path may use the guest stack below `sp`;
- each task's host stack must cover the deepest recompiled call chain on it plus the nesting
  `rt_jump` and the interpreter add;
- `k1`: the port's exception path is native and the C compiler never allocates `k0` or `k1`,
  so `k1` survives interrupts by construction, and the open question above about the loader's
  dispatcher does not arise in the port.

### 2. Other risks

- **Host stack depth.** Guest call depth is host call depth: every recompiled call is a C
  call, and the interpreter makes a host call per guest call. `rt_jump` to an address that is
  not a function start runs the interpreter as a nested call, so a loop through such a jump
  grows the host stack each pass. No `jr` is left to `rt_jump` now except the switch defaults,
  but a jump-table index out of range, a callback reached through a pointer, or a new manual
  entry can bring it back. Under (a) each task's host stack must be sized for its deepest chain.
- **Interrupt timing.** The board takes an interrupt at any instruction once the mask allows
  it; the port only at its check points (function entries and backward branches inside a
  function). Interrupts are therefore delayed, never lost, but a handler can see state the board
  would never show it, and the loader may mask interrupts around its services in ways the
  stand-ins must reproduce. What the game depends on (the VBLANK handler's timing, the
  `vdstatus` busy-wait, the scheduler's pacing through `timer:`) is not measured.
- **kuseg.** The mapping covers 0x00040000 to 8 MB. Any other kuseg address goes to the bus,
  where the board would take a TLB exception. Where the game's kuseg pointers come from is not
  traced, and a loader service that changes the TLB at run time would not be noticed.
- **No guest PC at a bus access.** The generated code passes `bus_read` and `bus_write` no
  guest PC, so the Voodoo bring-up cannot tell which instruction made an access; an emitter
  debug mode that stores the PC before each memory operation would.
- **Interpreted loops grow the host stack.** In the interpreter, a `j` or `jr` to a registered
  function start (its own entry included) is a tail call through `rt_call`, so an interpreted
  loop that jumps back to such a start nests one host frame per pass; generated code uses a
  `goto` there.
- **The 25 hardware-touching functions** are the first users of the Voodoo and CMOS models.
  Their captures are ready-made tests: once the bus models exist, the same captures can be
  replayed and must then match MAME too, not just the interpreter.
- **NaN bits and MAME-specific arithmetic.** A generated NaN has the host's bits, so the Mac
  and Linux builds can differ from each other and from the board if the game ever inspects
  them. The MAME-specific results (division by zero, out-of-range `trunc.w`, the multiply-add
  roundings) follow MAME; where the board would trap into the game's own FPU handler, the port
  does not. Logging the first occurrence of each in the runtime would show whether attract mode
  or play ever reaches them.
- **Flush to zero.** The game runs with FS = 1, which MAME ignores and the port, by default,
  ignores too. If the board flushes and the game ever depends on it (a denormal that becomes 0
  on the board, a compare against 0), the port and MAME agree with each other and both differ
  from the board, unseen by every check. `RT_HONOUR_FS` = 1 gives the R5000 behaviour for a
  comparison run. For the performance work (M7): denormal arithmetic is much slower than normal
  arithmetic on many x86 cores (not measured here), so the default can cost speed exactly where
  the board would have flushed; the switch exists, and measuring how often the game makes
  denormals tells whether it matters.
- **The FP divide-by-zero trap is not implemented.** `main` installs FPU exception handlers
  through the loader (from 0x800C46A0 for cause 5, handler 0x80150860; from 0x800C46C0 for
  cause 3, divide by zero, handler 0x8014FEF0), and the loader service sets the matching FCSR
  enable (read). MAME then raises the exception on a division by zero; the port returns the
  IEEE result, so after `main`'s setup a `div.s` or `div.d` by zero diverges from MAME. Both
  modes divide through `rc_div_s` / `rc_div_d` in `recomp.h`; M2 adds the trap there, with a
  runtime entry point that enters the game's handler.
- **Code size and compile time.** 8 MB of C compiles in 27 s with GCC on a 4-core container,
  where the PS4 build is made, so build time is not a problem. The result is 3.4 MB of x86-64
  code; how it behaves in the instruction caches of the PS4's Jaguar cores is not measured and
  is a question for M7.

## Known open items (minor, deferred)

Found in reviews and left for later; none affects the results above.

Tests:

- `test_edge_operands_present` checks the union of operand values, not the pairs.
- Interpreter test gaps: `lb`/`lhu` at 0x807FFFFF, `jalr` with `rd` equal to `rs`, a visible
  nullified slot (the delay-slot shapes are covered in the function tests).
- `first_difference` compares with `zip`, so a length mismatch would pass.
- The INT_MIN / -1 guard is only provable on x86-64; there is no Rosetta variant of the
  interpreter tests in the suite.
- `test_no_undefined_behaviour` repeats the MAME comparison.
- Tests read private names of `vectors` (`_GENERATORS`, `_state`, `_FCC_SHIFT`,
  `_SIGN_CYCLE`).
- The jump-table test lacks GCC's `sltiu`/`beqz` bounds-check shape.
- The determinism tests compare two runs in one process.
- Untested: `j` or `jr` to a start registered in only one mode; a backward conditional branch
  to another function's start.
- Two MAME tests copy game RAM to a local temporary folder (removed afterwards).
- `REGISTERS` is defined in two test modules.
- No isolated test for a differing written value alone at the first bus access; the merge gap
  of `differing_ranges` is coarse (4 KB).
- The harness's lookup by name is quadratic.

Tools and runtime:

- `assemble`'s docstring has a first line over 100 characters.
- `insnvec.lua` parses a hand-written JSON subset without escapes.
- The interpreter exits on a fetch outside RAM (for example the boot ROM) instead of handing it
  on (documented in `recomp.h`; the change is M2's); its entry log merges kseg0 and kseg1
  aliases, and logs a non-RAM entry every time.
- `recompile`: a short `umask(0)` window; a failure to remove the set-aside folder raises after
  a successful swap; a failed restore rename masks the original exception.
- The Makefile cannot handle spaces in paths (it now stops with one line saying so), keys
  objects by file name, and `make recomp`
  rewrites every generated file, so the next link check rebuilds everything
  ([tools.md](../tools.md), "make").
- `stub.c` reads the EXE's header word in host byte order.
- `.gitignore` ignores `funcs.h` and `manifest.toml` anywhere.
- A passing suite prints its diagnostics (the replay summary and the impure captures, the RAM
  differing outside the compared range) to stderr even when not verbose: `unittest` has no
  public channel for verbose-only output, so gating them is not a two-line change.
