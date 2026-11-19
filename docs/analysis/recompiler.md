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
  loader slots and interpreted functions included;
- `manifest.toml`, written last: the EXE's SHA-256 and the counts. For the supported revision:
  2,093 functions in 33 files, 29 loader stubs, 2 interpreted functions, 91 jump tables, 0
  `jr`s left to `rt_jump`.

That is 248,659 lines and 7,987,844 bytes of C in all. Two runs give identical bytes.

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
  condition bits apart in `fcc`, the PC (used by the interpreter only), `irq_pending`, and a
  pointer for the runtime's own data.
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
  (`rc_div_s`, `rc_div_d`: a plain `/` today, and where M2 adds the divide-by-zero trap, below),
  `trunc.w`, the FP compares and condition codes, `sqrt.s` and the multiply-add family. Plain
  operators stay inline in both the emitter and the interpreter: signed shifts and compares
  through `int32_t` casts, sign extension, FP `+ - *` and negation, `recip.s` as `1.0f / x`,
  and the conversion casts. Each such operator is pinned by the MAME vectors in both copies.
- **Entry points a runtime provides**: `bus_read`, `bus_write`, `rt_loader(ram, c, slot)`,
  `rt_break(ram, c, pc, code)`, `rt_interrupt(ram, c)` and `recomp_lookup(addr)` (the
  generated `func_table.c` provides the last). `recomp.h` gives the contract of each in a
  comment block: the sizes and masking of a bus access; the unit of `slot` (the byte offset
  from 0x800C408C), the state at a loader call and what the caller does after it; that
  returning from `rt_break` resumes at the next instruction; and that `rt_interrupt` must
  leave the guest state and the host FP environment as an exception return would, clears
  `irq_pending` before it runs a handler, and runs the handler on whatever host context is
  current.
- **Provided by `runtime/core`**: `rt_call` and `rt_jump` (the recompiled function at an
  address if there is one, otherwise the interpreter), `rt_set_fcsr` and `rt_get_fcsr` (FCSR's
  rounding mode becomes the host's, and so does flush-to-zero when built with `RT_HONOUR_FS`
  = 1, below: FPCR on arm64, MXCSR on x86-64; any other host is a compile error), and `rt_log_unrecompiled`, called once per address the
  interpreter is entered at. The host FP environment is the process's (per thread), and only
  `rt_set_fcsr` sets it, so a runtime that loads a state or switches host context or thread
  re-applies it with `rt_set_fcsr(c, rt_get_fcsr(c))`.
- **`RT_CHECK(ram, c)`**: calls `rt_interrupt` when `irq_pending` is set.

Every C file is built with `-std=c11 -O2 -ffp-contract=off -frounding-math
-fno-strict-aliasing -fwrapv`, never `-ffast-math`. `-ffp-contract=off` stops the compiler
fusing a multiply and an add; `-frounding-math` stops it folding FP expressions as if the
rounding mode were always round-to-nearest, since `ctc1` can change it. The generated C has no
undefined behaviour: the test harness builds it with `-fsanitize=undefined
-fno-sanitize-recover=undefined`.

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
  to 25), then the next instruction. 38 sites are emitted; the 39th `break` of reached code
  (0x80151AD0) is inside an interpreted function.
- **Interrupt check points**: `RT_CHECK` at the entry of every function (and of `interp_run`),
  and before a backward transfer that stays inside the current function: a `goto` to an address
  at or below the transferring instruction (3,159 checks in all). A transfer that leaves through
  a call (a direct call, `rt_call`, `rt_jump`, `rt_loader`) does not check; the callee's entry
  does. The interpreter checks at the same points. The two counts differ only where the
  interpreter runs on inline into another function's start that the C calls (a branch to it, a
  fall-through off the end, a jump to a start the interpreter's lookup does not know): there
  the C makes one more check, the callee's entry. A pending interrupt is only ever delayed to
  the next check point, never lost.

## The reference interpreter

`interp_run(ram, c, pc)` runs from `pc` until the function entered there returns. It covers the
98 census mnemonics plus `cfc1` (which the MAME scripts need), with the same helpers as the
generated C, and stops the process with the address and the word on anything else, on a control
transfer in a delay slot, and on a fetch from outside RAM. `jal` and `jalr` go through
`rt_call` and continue after the slot. Any other `jr`, and a `j`, whose target `recomp_lookup`
knows is a tail call; otherwise it continues at the target.

It serves two purposes: the oracle the generated C is compared with, instruction by
instruction and on whole synthetic functions, and the safety net for any address with no
recompiled function. Each address it is entered at is logged once through
`rt_log_unrecompiled`, which is the evidence a later manual entry needs.

## Manual entries added in M1

`config/game.manual.toml` gained two kinds of entry in M1, each with a `reason` citing the
instructions that establish it. `functions` checks them when it reads them: a `[[jump_table]]`
must name a `jr` on a register other than `ra` in a function's reached code, every target must
lie inside that function, and a table for a `jr` the analysis resolves by itself is refused; an
`[[interpreted]]` address must be a function start; a missing or unknown key names the entry.

- **16 `[[jump_table]]` entries**, the 16 indirect jumps M0 left unresolved
  ([functions](functions.md), "Manual jump tables"): two switches whose bound or base lies
  outside the analysis's window (`jr` at 0x8012DCA8, table 0x80174328, 28 entries; `jr` at
  0x80150174, table 0x801757A8, 5 entries), and 14 dispatches in five block-copy routines
  (0x80164484 to 0x801652F8), each a 7-word table of the four labels of its own 28-byte tail.
  With them, 488 bytes of case blocks became reached code and no `jr` is left to `rt_jump`
  ([functions](functions.md), "Unresolved indirect jumps (0)").
- **2 `[[interpreted]]` entries**, 0x80151AC0 and 0x80151B50: the game's hand-written task
  switch (below). Each is emitted as a thunk calling `interp_run(ram, c, addr)`; its own words
  are never emitted.

The 3 `[[manual]]` starts and the stub block are M0's.

## What is left to the interpreter

- The 2 interpreted functions above.
- Any call or jump to an address that is not a function start: the 7 probable callbacks and the
  141 unreferenced leaf-shaped ranges of [functions](functions.md) are not recompiled, on
  purpose. If one is ever called through a pointer, `rt_call` misses in the lookup, the
  interpreter runs it and logs the address.
- The `default` arm of each of the 91 switches (an index outside the table).
- Nothing else: every `jalr` target that is a function start is recompiled code.

## The task switch, and why it is interpreted

0x80151AC0 to 0x80152008 is not GCC output. 0x80151AC0 is the game's yield: it puts its
argument into the current task's sleep counter, swaps `sp` and `k1` (so `k1` holds the task's
stack and `sp` the scheduler's), saves the callee-saved registers on the task's own stack,
stores the task's stack pointer in its record and jumps to 0x80151E90, inside the scheduler
0x80151BC0, which reloads the scheduler's registers and carries on with the next task. The
scheduler resumes a task the other way round, ending in `jr ra` into the task. 0x80151B50 is
the exit path of a finished task. [functions](functions.md), "Jumps out of their function",
has the detail, and the [M1 report](M1-report.md) the options for M2.

Neither function recompiles: their `j 0x80151E90` lands inside another function, and a manual
start at 0x80151E90 fails too (0x80151BC0 and the new function then jump into each other's
middles). So they are interpreted, which makes the image compile and link. It does not make the
switch work. The scheduler is recompiled, and its `jr ra` into a task is a C `return` to
whatever C frame called it; the interpreter also ends a run at `jr ra`. Switching tasks needs
the runtime of M2.

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
  manuals (no trap, the quotient wraps), with a guard in `rc_div` so the host never traps. The guard matters on x86-64 only: arm64's divide
  never traps.

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

## A divergence from MAME: the FP divide-by-zero trap

The game enables an FP exception that the port does not raise. `main` installs two FPU
exception handlers through the loader's slot 0x1E8, which also sets the matching FCSR enable:
from 0x800C46A0 for cause 5, unimplemented operation (handler 0x80150860), and from 0x800C46C0
for cause 3, divide by zero (handler 0x8014FEF0) (read). MAME raises the divide-by-zero
exception when its enable is set, so on MAME a `div.s` or `div.d` by zero after that point
enters the game's handler. The port does not trap: it returns the IEEE result (an infinity, or
a NaN for 0 / 0) and carries on. The vectors cannot show this (they keep FCSR's enables clear),
so it is not checked against MAME. Both the generated C and the interpreter divide through
`rc_div_s` and `rc_div_d` in `recomp.h`, which is where M2 adds the trap.
