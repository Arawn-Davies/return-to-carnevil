# The recompiler

Milestone M1 turned every function of `GAME.EXE` into C. This note says what that C looks
like, what it relies on, and where it stops. The code is `carnevil_tools/emit.py` (one
instruction), `carnevil_tools/recomp.py` (one function, and the whole image),
`runtime/include/recomp.h` (the contract), `runtime/core/` and `runtime/interp/interp.c` (the
reference interpreter). The commands are in [tools.md](../tools.md); the results and the checks
against MAME are in the [M1 report](M1-report.md).

Every count below was produced by `make recomp` on the supported revision, or by the tools
named next to it.

## What comes out

`python3 -m carnevil_tools recompile GAME.EXE -o <folder>` (or `make recomp`) reads
`config/game.functions.toml`, the output of function discovery ([functions](functions.md)), and
writes:

- `funcs.h`: one prototype per function;
- `funcs_000.c` to `funcs_032.c`: 33 files of 64 functions each (the last has 45), in address
  order;
- `func_table.c`: `recomp_lookup`, a binary search over a sorted table of every function start,
  loader slots and interpreted functions included, and `rt_switch_kind`, which names the
  host-switch points (below);
- `manifest.toml`, written last: the EXE's SHA-256 and the counts. For the supported revision:
  2,093 functions in 33 files, 29 loader stubs, 0 interpreted functions, 91 jump tables, 0
  `jr`s left to `rt_jump`.

That is 248,732 lines and 7,991,703 bytes of C in all (M1 left 2 functions to the interpreter
and wrote 248,659 lines and 7,987,844 bytes). Two runs give identical bytes. Since the check
points carry their address and the FP divides store the pc (below; the whole-branch review of
M2, finding I3), the `*.c` files and `funcs.h` hold 249,055 lines and 8,041,049 bytes.

`--trace-pc` (off by default) adds `c->pc = <address>;` before each load and store and before
the call of each `jal` and `jalr`, so a runtime can name the guest instruction that made a bus
access or a call. Without it the only stores of `c->pc` are the two every build has: on the
slow path of each check point, and before each FP divide.

Each MIPS function becomes one C function, `void fn_<ADDR>(uint8_t *ram, cpu_t *c)`, named
after its address in eight upper-case hexadecimal digits, with the analysis name in a comment.
`ram` is the 8 MB of guest RAM and `c` the guest CPU. Every guest register lives in `c`, so a C
function has no state of its own beyond two kinds of local: a branch's decision and an indirect
jump's target, both taken before the delay slot runs. Branches inside a function become `goto`
to labels named `L_<ADDR>`. A `jal` becomes a direct C call. The output is not meant to be
read; every line still talks about MIPS registers.

A made-up example, not taken from the game, of three instructions as they come out: `addiu
v0,a0,16`, `sw v0,24(sp)` and `lui v1,0x80AB`.

```
    c->r[2] = c->r[4] + 0x00000010u;
    mem_w32(ram, c, (c->r[29] + 0x00000018u), c->r[2]);
    c->r[3] = 0x80AB0000u;
```

The shape (one C function per MIPS function over a register context, `goto` labels inside it,
a lookup for indirect calls) is the one N64Recomp uses. The emitter itself is new: N64Recomp
does not handle this game's MIPS IV instructions, is big-endian throughout, keeps one FP
condition bit, takes no jump tables from outside and has no interrupt check points (the M1
plan, "Decisions this plan takes").

## The contract: `recomp.h`

The generated C, the interpreter and any runtime share one header.

- **The CPU** (`cpu_t`): 32 general registers of 32 bits (reached code has no 64-bit integer
  instruction), `hi`, `lo`, 32 FP register halves (FR = 0: a single is one half; a double in
  even register n is half n, low word, and half n + 1), FCSR without its condition bits, the 8
  condition bits apart in `fcc`, the PC (kept at every instruction by the interpreter; in
  generated code stored at a check point that ticks and before an FP divide, and at loads,
  stores and calls only when built with `--trace-pc`), `slice`, the check points left until
  the runtime's clock is next
  advanced (below), and a pointer for the runtime's own data.
- **Memory**: `mem_r8`, `mem_r16`, `mem_r32` (zero-extended; the caller sign-extends) and
  `mem_w8`, `mem_w16`, `mem_w32`. An access is RAM when it lies in kseg0 (0x80000000) or kseg1
  (0xA0000000) below 8 MB, or in kuseg from 0x00040000 to 8 MB, which the loader's TLB maps
  onto the same physical RAM ([boot-handoff](boot-handoff.md), "kuseg is mapped onto RAM").
  RAM is read and written with `memcpy`, little-endian; a big-endian host is a compile error.
  Every other address, an access that straddles the end of RAM included, goes to the runtime's
  `bus_read` and `bus_write`. This is where the Voodoo, the gun, the sound FIFO and the CMOS
  will be modelled. Alignment is not checked: an unaligned access is performed, where the
  board would raise an address error.
- **Arithmetic helpers** (`rc_*`), one per operation whose C is not a plain operator:
  division, multiplication into `hi`/`lo`, the FP register getters and setters, FP divide
  (`rc_div_s`, `rc_div_d`, which raise the divide-by-zero trap, below; `recip.s` is
  `rc_div_s(c, 1.0f, x)`), `trunc.w`, the FP compares and condition codes, `sqrt.s` and the
  multiply-add family. Plain operators stay inline in both the emitter and the interpreter:
  signed shifts and compares through `int32_t` casts, sign extension, FP `+ - *` and negation,
  and the conversion casts. Each such operator is pinned by the MAME vectors in both copies.
- **Entry points a runtime provides**: `bus_read`, `bus_write`, `rt_loader(ram, c, slot)`,
  `rt_break(ram, c, pc, code)`, `rt_tick(ram, c)`, `rt_host_switch(ram, c, kind)`,
  `rt_fp_trap(c, cause_bit)`, `recomp_lookup(addr)` and `rt_switch_kind(pc)` (the generated
  `func_table.c` provides the last two). `recomp.h` gives the contract of each in a comment
  block: the sizes and masking of a bus access; the unit of `slot` (the byte offset from
  0x800C408C), the state at a loader call and what the caller does after it; that returning
  from `rt_break` resumes at the next instruction; that `rt_tick` accounts the check points
  used, runs the events that are due, delivers the pending interrupts and reloads `slice` (at
  least 1), leaving the guest state and the host FP environment as an exception return would
  and running a handler on whatever host context is current; what the caller of
  `rt_host_switch` does after each kind; and that `rt_fp_trap` does not return in a real
  runtime.
- **Provided by `runtime/core`**: `rt_call` and `rt_jump` (the recompiled function at an
  address if there is one, otherwise the interpreter), `rt_set_fcsr` and `rt_get_fcsr` (FCSR's
  rounding mode becomes the host's, and so does flush-to-zero when built with `RT_HONOUR_FS`
  = 1, below: FPCR on arm64, MXCSR on x86-64; any other host is a compile error), and
  `rt_log_unrecompiled`, called once per address the interpreter is entered at. The host FP
  environment is the process's (per thread), and only `rt_set_fcsr` sets it, so a runtime
  that loads a state or switches host context or thread re-applies it with
  `rt_set_fcsr(c, rt_get_fcsr(c))`.
- **`RT_CHECK(ram, c, at)`**: `if (--c->slice <= 0) { c->pc = at; rt_tick(ram, c); }`. Time
  is counted in check points: the runtime's clock advances by the checks used whenever
  `slice` runs out. `at` is the address execution goes on at after the check (below); it is
  stored only on the slow path, so the fast path is what it was.

Every C file is built with `-std=c11 -O2 -ffp-contract=off -frounding-math
-fno-strict-aliasing -fwrapv`, never `-ffast-math`. `-ffp-contract=off` stops the compiler
fusing a multiply and an add; `-frounding-math` stops it folding FP expressions as if the
rounding mode were always round-to-nearest, since `ctc1` can change it. The generated C showed
no undefined behaviour where it was run under `-fsanitize=undefined
-fno-sanitize-recover=undefined`: the test harness builds the instruction and synthetic
functions that way, and the test suite builds `replay` that way over the whole generated image
and runs all 152 function captures through it (both modes for the impure ones).

## How each kind of instruction is handled

- **Straight-line instructions** (77 of the 98 mnemonics of the census): one or a few C
  statements each. A write to register 0 is dropped; a load into register 0 is still made,
  because the bus may have a side effect. `addi` is emitted as `addiu` (MAME takes no overflow
  trap). `cache` becomes a comment. `ctc1` calls `rt_set_fcsr`. An instruction outside the
  census stops the recompile with its address and mnemonic; it is never a silent no-op.
- **Delay slots.** A branch evaluates its condition into a local, then runs the slot, then
  jumps. `jal` sets `ra` before the slot runs; `jalr` and `jr` read their target register
  before the slot runs. So a slot that changes the branch's operand, reads or writes `ra`, or
  overwrites the jump register behaves as on the CPU. A delay slot that is itself a branch
  target is emitted a second time under its own label on the fall-through path. A control
  transfer in a delay slot is an error; none occurs in reached code.
- **Likely branches** (8 in the census: `beql`, `bnel`, `blezl`, `bgtzl`, `bltzl`, `bgezl`,
  `bc1fl`, `bc1tl`): the slot runs only when the branch is taken. A condition that its operands
  decide (`beq r,r`, `bgez zero` and the like) is decided at recompile time.
- **FP condition codes**: all eight live in `c->fcc`, so a compare before a call and a `bc1t`
  after it still agree, and the one function using codes 1 and 2 (0x80155B18) needs nothing
  special.
- **Calls**: a `jal` to a function start is a direct C call; to an address that is not a
  start it is `rt_call`. A `jalr` is always `rt_call` (64 sites), which finds the recompiled
  function through `recomp_lookup` or runs the interpreter.
- **Returns**: `jr ra` is the slot, then `return`.
- **Jump tables** (91: 75 found by the analysis, 16 from the manual file): a `switch` on the
  register's value with one `case` per distinct target, each a `goto`. Its `default`, reached
  only by an index the table does not hold, is `rt_jump` and `return`.
- **Tail jumps**: a `j`, or a branch, to another function's start is the slot, a direct call
  to that function, and `return`. To any other address outside the function it is an error.
  A function whose code can run off its end calls the function starting there and returns.
- **Loader stubs**: the 29 one-slot functions in the stub block 0x800C408C to 0x800C42CC are
  never translated from the file, where they are zero. Each is a thunk whose body is
  `rt_loader(ram, c, slot)`, so a call through `rt_call` or the lookup reaches the runtime's
  loader stand-in. A `jal` straight into the block becomes `rt_loader` inline (279 sites); a
  `j` into it becomes `rt_loader` and `return`. A conditional branch into the block is an
  error; reached code has none.
- **`break`**: `rt_break(ram, c, addr, code)` with the instruction's 20-bit code field (bits 6
  to 25), then the next instruction. All 39 `break`s of reached code are emitted (M1 emitted
  38: the 39th, 0x80151AD0, is in the yield, then interpreted).
- **Host-switch points** (3, from the manual file, below): the slot, then
  `rt_host_switch(ram, c, RT_SWITCH_<KIND>)`; after a resume (a `jr ra`) the code at the
  address after the slot follows, after a yield or an exit (a `j`) the function returns.
- **FP divide**: `div.s`, `div.d` and `recip.s` call `rc_div_s` or `rc_div_d`, which call
  `rt_fp_trap(c, 1 << 15)` (the FCSR cause bit of divide by zero) when the divisor is zero, the
  dividend finite and non-zero and the FCSR enable bit 10 set, then return the IEEE result.
  The generated code stores the instruction's address in `c->pc` before each of them (320
  stores in the game's code), as the interpreter does at every instruction, so the trap is
  raised with the dividing instruction's address in both modes.
- **Interrupt check points**: `RT_CHECK` at the entry of every function but the stub thunks
  (and of `interp_run`), and before a backward transfer that stays inside the current
  function: a `goto` to an address at or below the transferring instruction (3,161 checks in
  all, the 3,159 of M1 and the entries of the yield and the exit). A transfer that leaves
  through a call (a direct call, `rt_call`, `rt_jump`, `rt_loader`, `rt_host_switch`) does not
  check; the callee's entry does. The interpreter checks at the same points. The two counts
  differ only where the interpreter runs on inline into another function's start that the C
  calls (a branch to it, a fall-through off the end, a jump to a start the interpreter's
  lookup does not know): there the C makes one more check, the callee's entry. Each check
  counts one unit of the runtime's clock; an interrupt is delivered at the check where `slice`
  reaches 0, so it is only ever delayed to a check point, never lost. A check that ticks
  stores the address execution goes on at in `c->pc` first, the same in both modes: the
  function's address at an entry check, the target at a backward-transfer check. That is the
  resume pc and EPC the interrupt frame records (`tests/test_clock_irq.py`,
  `CheckPointPcTest`, compares the frame in both modes; `tests/test_recomp.py` and
  `tests/test_interp.py` compare the pc `rt_tick` sees). Until the whole-branch review of M2
  (finding I3, 2026-11-20) the generated code never stored it, so every frame recorded the
  game's entry address 0x800C4000, and the interpreter recorded the pc of the instruction it
  ran last (the caller's delay slot at an entry, the branch's delay slot at a back edge).

## The reference interpreter

`interp_run(ram, c, pc)` runs from `pc` until the function entered there returns. It covers the
98 census mnemonics plus `cfc1` (which the MAME scripts need), with the same helpers as the
generated C, and stops the process with the address and the word on anything else, on a control
transfer in a delay slot, and on a fetch from outside RAM. `jal` and `jalr` go through
`rt_call` and continue after the slot. A `j` or `jr` at an address `rt_switch_kind` names is a
host-switch point, handled as the generated C handles it. Any other `jr`, and a `j`, whose
target `recomp_lookup` knows is a tail call; otherwise it continues at the target.

It serves two purposes: the oracle the generated C is compared with, instruction by
instruction and on whole synthetic functions, and the safety net for any address with no
recompiled function. Each address it is entered at is logged once through
`rt_log_unrecompiled`, which is the evidence a later manual entry needs.

## Manual entries added in M1 and M2

`config/game.manual.toml` gained two kinds of entry in M1 and one in M2, each with a `reason`
citing the instructions that establish it. `functions` checks them when it reads them: a
`[[jump_table]]` must name a `jr` on a register other than `ra` in a function's reached code,
every target must lie inside that function, and a table for a `jr` the analysis resolves by
itself is refused; an `[[interpreted]]` address must be a function start; a `[[host_switch]]`
must have the kind `yield`, `exit` or `resume` and name a `j` (yield, exit) or a `jr ra`
(resume) in a function's reached code; a missing or unknown key names the entry.

- **16 `[[jump_table]]` entries**, the 16 indirect jumps M0 left unresolved
  ([functions](functions.md), "Manual jump tables"): two switches whose bound or base lies
  outside the analysis's window (`jr` at 0x8012DCA8, table 0x80174328, 28 entries; `jr` at
  0x80150174, table 0x801757A8, 5 entries), and 14 dispatches in five block-copy routines
  (0x80164484 to 0x801652F8), each a 7-word table of the four labels of its own 28-byte tail.
  With them, 488 bytes of case blocks became reached code and no `jr` is left to `rt_jump`
  ([functions](functions.md), "Unresolved indirect jumps (0)").
- **3 `[[host_switch]]` entries** (M2), which replace M1's 2 `[[interpreted]]` entries for
  0x80151AC0 and 0x80151B50: the transfers of the game's hand-written task switch (below). The
  kind `[[interpreted]]` remains (a thunk calling `interp_run(ram, c, addr)`, whose own words
  are never emitted); no entry uses it.

The 3 `[[manual]]` starts and the stub block are M0's.

## What is left to the interpreter

- No function: the manual file has no `[[interpreted]]` entry since M2.
- Any call or jump to an address that is not a function start: the 7 probable callbacks and the
  141 unreferenced leaf-shaped ranges of [functions](functions.md) are not recompiled, on
  purpose. If one is ever called through a pointer, `rt_call` misses in the lookup, the
  interpreter runs it and logs the address.
- The `default` arm of each of the 91 switches (an index outside the table).
- Nothing else: every `jalr` target that is a function start is recompiled code.

## The task switch: three host-switch points

0x80151AC0 to 0x80152008 is not GCC output. 0x80151AC0 is the game's yield: it puts its
argument into the current task's sleep counter, swaps `sp` and `k1` (so `k1` holds the task's
stack and `sp` the scheduler's), saves the callee-saved registers on the task's own stack,
stores the task's stack pointer in its record and jumps to 0x80151E90, inside the scheduler
0x80151BC0, which reloads the scheduler's registers and carries on with the next task. The
scheduler resumes a task the other way round, ending in `jr ra` into the task. 0x80151B50 is
the exit path of a finished task (read; [task-switch](task-switch.md), "The mechanism", has the
detail, and [functions](functions.md), "Jumps out of their function", the analysis).

In M1 neither the yield nor the exit recompiled: their `j 0x80151E90` lands inside another
function, and a manual start at 0x80151E90 fails too (0x80151BC0 and the new function then jump
into each other's middles). So they were interpreted, and the scheduler's `jr ra` into a task
was a C `return`.

M2 makes the three transfers calls of the runtime, `rt_host_switch`, through
`[[host_switch]]` entries (read, the disassembly):

- 0x80151B48, kind `yield`: the yield's `j 0x80151E90` (slot `sw k1, 8(t1)`, the task's stack
  pointer into its record);
- 0x80151B78, kind `exit`: the exit's `j 0x80151E90` (slot the last `xor` of the swap);
- 0x80151E88, kind `resume`: the scheduler's `jr ra` into a task (slot the last `xor` of the
  swap). The instruction after the slot, 0x80151E90 (`addi sp, sp, 0xCC`), is the restore path
  the other two jump to; after a resume it follows by fall-through, so when the runtime returns
  from a resume (the task has yielded or exited) the scheduler restores its registers there.

With them the `j`s no longer leave their functions and the `jr ra` is no longer a return, so all
three functions are recompiled and nothing is interpreted. The analysis takes the instruction
after a resume as reached (0x80151E90 already was, as the target of the two `j`s; the functions
file is unchanged apart from the entries and its format, 3). Switching tasks is the runtime's
job: the stub runtimes of M1 log the call and return.

## Results that are MAME's, not established hardware facts

The instruction vectors are recorded from MAME's R5000 core, and the port is made to match
them, so the port and its oracle agree. Where MAME's result is not an established hardware
fact, `carnevil_tools/vectors.py` lists the case in `MAME_SPECIFIC` with its reason; a real
board might differ there. 52 cases:

- **Division by zero** (10: `div` and `divu` of 0, 1, 0x7FFFFFFF, 0x80000000 and 0xFFFFFFFF by
  0): MAME leaves `hi` and `lo` unchanged. MIPS leaves the result undefined.
- **`trunc.w.s` and `trunc.w.d` of a NaN or of a value outside int32** (11): MAME gives
  0x80000000, the x86 conversion result. The R5000 may give that, or raise an Unimplemented
  Operation exception; not verified.
- **The multiply-add family** (3: `nmadd.s.5`, `msub.s.9`, `nmsub.s.11`): MAME rounds the
  product to the format and then the sum, two roundings, and `nmadd`/`nmsub` negate the rounded
  sum. All 60 recorded cases of the family equal that result; in these 3 a single rounding
  gives other bits. Whether the R5000 rounds once or twice is not verified.
- **`recip.s`** (12: every recorded case): MAME computes `1.0f / x`, correctly rounded. The
  R5000's `recip` is not verified to be that accurate. The game has one `recip.s` site.
- **FCSR.FS = 1** (16: the `*.edge.fs1.*` cases): MAME does not model FS (below).

Two more results have no MAME vector at all (`vectors.NO_MAME`):

- **`break`** raises an exception in MAME, so a case cannot reach its end. The port's handling
  (`rt_break` with the code, then the next instruction) is tested between the generated C and
  the interpreter only.
- **`div` of 0x80000000 by 0xFFFFFFFF** (INT_MIN / -1): MAME divides on the host, and x86-64
  faults. The port gives `lo` = 0x80000000 and `hi` = 0, our reading of the R4000/R5000
  manuals (no trap, the quotient wraps), with a guard in `rc_div` so the host never traps. The
  guard matters on x86-64 only: arm64's divide never traps.

And one divergence between hosts: a NaN that an FP operation generates (0/0, inf - inf, the
square root of a negative number) has the host's bits, 0x7FC00000 on arm64 and 0xFFC00000 on
x86-64, where the R5000's default NaN is 0x7FBFFFFF. Nothing canonicalises it: that would cost a
branch on every FP operation, for bits the game would have to inspect as integers to notice. A
NaN operand's payload propagates the same way on both hosts.

MAME-only behaviour the vectors cannot show: `cache` is a no-op, no FP exception is raised
except division by zero with its enable set, and `addi` never traps on overflow
([tools/mame/README.md](../../tools/mame/README.md)).

## Flush to zero (FCSR.FS): the port follows MAME

`_start` writes FCSR with FS = 1 (the `ctc1` at 0x800C4064, the only one in reached code), so
the game runs in flush-to-zero mode throughout (read). MAME 0.276 does not model FS: the 16
FS = 1 vector cases (denormal results from normal operands of `mul`, `div`, `add` and `sub` in
both formats and `cvt.s.d` of a tiny double; denormal operands of `mul`, `add`, `cvt.d.s`,
`c.eq.s` and `c.lt.s`) give exactly the results of their FS = 0 controls: denormal results are
kept, and denormal operands are used as they are (MAME, `insnvec.lua`). The R5000 documents
flushing both to zero.

The port follows MAME, because every later milestone is checked against it. One compile-time
switch in `runtime/core/fpu.c` decides: `RT_HONOUR_FS` = 0 (the default) keeps FS in FCSR but
does not change the host FP environment for it; `RT_HONOUR_FS` = 1 sets host flush-to-zero
when FS = 1, as the R5000 documents (arm64 FPCR.FZ; x86-64 MXCSR FTZ and DAZ). The FS = 1
cases are listed in `MAME_SPECIFIC` ("MAME does not model FS"), and the test suite checks the
flush in a separate build with the switch set to 1. On x86, denormal arithmetic is much slower
than normal arithmetic, so the default can cost speed where the board would have flushed.

## The FP divide-by-zero trap

The game enables an FP exception. `main` installs two FPU exception handlers through the
loader's slot 0x1E8, which also sets the matching FCSR enable: from 0x800C46A0 for cause 5,
unimplemented operation (handler 0x80150860), and from 0x800C46C0 for cause 3, divide by zero
(handler 0x8014FEF0) (read). MAME raises the divide-by-zero exception when its enable is set,
so on MAME a `div.s` or `div.d` by zero after that point enters the game's handler. M1 did not
trap; since M2, both the generated C and the interpreter divide through `rc_div_s` and
`rc_div_d` in `recomp.h`, which call the runtime's `rt_fp_trap(c, 1 << 15)` when the divisor
is zero, the dividend finite and non-zero, and the FCSR enable bit 10 set. That is the IEEE 754
divide-by-zero case; 0 / 0 (invalid), an infinity divided by 0 (exact) and a NaN divided by 0
do not call it, and what MAME does with them is not measured. `recip.s` divides 1 by its
operand through `rc_div_s`, so `recip.s` of 0 traps the same way (inferred: the M1 re-review
read the R5000 as raising divide by zero there; not measured in MAME). A real runtime does not
return from `rt_fp_trap`; the stub runtimes and the test harness log it and return, and the
division then gives its IEEE result, so the instruction vectors (which keep FCSR's enables
clear) are unaffected. The trap is tested between the generated C and the interpreter only.
