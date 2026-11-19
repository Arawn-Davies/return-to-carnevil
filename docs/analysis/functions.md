# GAME.EXE function discovery

Produced by `python3 -m carnevil_tools functions GAME.EXE -o config/game.functions.toml`
(`carnevil_tools/functions.py`, names from `carnevil_tools/naming.py`). The
command prints every number below. It reads the hand-maintained
`config/game.manual.toml` (the output's name with `.functions.toml` replaced
by `.manual.toml`): the stub block, three function starts MAME showed, 16 jump
tables read from the code ("Manual jump tables") and 2 functions left to the
interpreter ("Jumps out of their function"), each with the evidence for it.
`config/game.functions.toml` holds the result: `format = 2`, the SHA-256 of the
EXE it came from, text bounds, the stub block, functions (address, size, name,
sources), jump tables (the `jr`, the table address, 0 for a manual table, and
the targets), the interpreted functions and the unreached ranges. Counts and
addresses only. A game-data test checks that the committed file equals a fresh
analysis.

The numbers below are from the run with the full manual file of M1 Task 5. The
16 manual jump tables made 488 bytes of case blocks reached code; the counts
that moved are given with their earlier value ("was ...").

Re-run in Task 5b on the chain-extracted GAME.EXE (SHA-256 f9455ce8...d3a6,
`disk-format.md`): `config/game.functions.toml` was byte-for-byte unchanged
from the run on the earlier, contiguously extracted file (no function added or
removed). The corrected bytes (0x801A3FF8 to 0x801A4224) are data, past the
16-byte code island at 0x801A355C. The numbers below are from the later run
with the manual input and the stub block ("What the manual input changed").

Entry: 0x800C4000 (the load address).

## Result in one paragraph

The text runs from 0x800C4000 to 0x8016DC4C (695,372 bytes). There are 2,093
functions: 29 one-slot stubs in the boot-ROM stub block and 2,064 of the
game's own. There are 91 jump tables (1,619 entries, the largest has 121): 75
found by the analysis and 16 from the manual file (was 75 and 1,488). One of
the functions sits in the data, past text_end (0x801A355C, below). 165,760
words are reached as code (was 165,638) and none of them decodes as unknown.
No function runs straight into the next one. 253 ranges (31,772 bytes, 4.57% of
the text; was 262, 32,260 bytes, 4.64%) are not reached; 15 KB of that is one
data block inside the text. The analysis alone leaves 16 indirect jumps
unresolved; the manual file gives the table of each, so none is left
(below).

## Functions by source

A function counts once under each of its sources:

- entry: 1
- call: 1,214 (`jal`, plus tail calls by `j`, see departure 4; was 1,213: a
  case block of the manual table at 0x80150174 calls 0x8014BD78)
- pointer: 396
- prologue: 981
- manual: 3 (`config/game.manual.toml`)
- stub: 29 (call targets inside the stub block)

The combinations that occur: call 907, call+prologue 296, call+pointer 9,
call+pointer+prologue 2, pointer 163, pointer+prologue 219, prologue 464,
manual+pointer 3, stub 29, entry 1. (0x8014BD78 moved from prologue to
call+prologue.)

The lui/addiu (and lui/ori) pass over reached code found 146 function starts
that no other pass had found when it ran. These are callbacks whose address is
built in code: the `pointer` source covers both this pass and the data scan.
The pass runs before the prologue scan in each round, so some of the 146 have a
prologue and would also have been found by it. Which pass finds a start first
depends on how far the code reached in the earlier rounds extends, so the
sources of a function, unlike the set of functions, shift when the inputs
change (see "What the manual input changed").

## Names from debug strings

21 functions are named after a debug string of the form `name(): …` or
`name: …` that no other function references:

main_init_sound 0x800C5E50, coin_volume_proc 0x800C6F44, CREDITS 0x800C7DD0,
Movie_load 0x800E7188, Movie 0x800E7CF8, ENVIRONMENT 0x801412D0, MATHERR
0x801450D0, create_texture 0x8014E528, Command 0x801517EC, sst1InitSli
0x8015C4F8, grSstOpen 0x801615E8, grSstSelect 0x801620F0, grTexCombineFunction
0x80163684, grTexMipMapMode 0x80163F74, grTexMultibase 0x801654D4,
grTexMultibaseAddress 0x80165504, block 0x80168080, snd_dbg_boot_info
0x801695B4, snd_get_debug 0x8016A510, snd_dump_port 0x8016A7CC,
snd_load_bank_playing 0x8016C6E8.

The `name: …` form, which the rule allows, also matches format labels.
CREDITS, ENVIRONMENT, MATHERR, Command, block and Movie come from strings like
`CREDITS: %d` or `block: %d` and are probably not the functions' real names.
grab_save_screen and grab_write_file are not used, because one function
references both of them.

## Text boundary

The last code word reached is not the end of the text. The function at
0x801A355C (`break 0x409; nop; jr ra; nop`, called 18 times from the text)
sits in the data section, 219 KB past the rest of the code. text_end is the
end of the last code word before the widest gap between code words. That rule
applies only when the gap holds data (a word that does not decode) and is
wider than all the code beyond it. On this image it gives 0x8016DC4C.
Functions past text_end take their size from the code reached (16 bytes here)
and are not part of the text range.

## The boot-ROM stub block at 0x800C408C to 0x800C42CC (end exclusive)

In the file these 576 bytes are all zero. 29 call targets (0x800C40A4 to
0x800C42AC) lie inside the block. `_start` fills it at boot by copying from
0xA0000000, which is physical address 0 and was left there by the boot ROM.
The block is declared in `config/game.manual.toml` and recorded as
`[[stub_block]]` in functions.toml. Discovery never decodes its words or
descends into them: each call or jump target inside it is a function of one
slot (8 bytes, a `j` and its delay slot once filled) with the single source
`stub`, and the block's words are neither reached code nor unreached ranges.
`boot_state.toml` maps each stub to its loader slot (stub = 0x800C408C +
slot). The recompiler must not translate this block from the file: its
contents come from the boot ROM (Task 5).

The three words after the block, 0x800C42CC to 0x800C42D8 (the first is the
bytes "spri"), are unreached. Before the block was declared, the descent read
the zeros as `nop`s and ran through them into these words, which is how one
`ldl` came to count as reached code.

## What the manual input changed

Against the run without `config/game.manual.toml` (2,091 functions; the list
committed before the stub block and the manual starts were added):

- Added: 0x800C6030 (size 236), 0x8014A930 (40) and 0x80151A70 (20), each
  with sources manual and pointer. 0x800C6030 swallows 0x800C6070, a start
  the prologue scan had found inside it, which is gone (172 bytes).
- Resized: the 29 stub-block entries are 8 bytes each and have the source
  `stub` instead of `call`; 14 of them had other sizes (16 to 80 bytes) read
  from the zeros. No other function changed size and none was renamed.
- Unreached: 263 ranges (32,396 bytes) became 262 (32,260 bytes). Gone:
  0x800C408C to 0x800C40A4 (now stub block), 0x800C6030 to 0x800C6070,
  0x8014A92C to 0x8014A958 and 0x80151A70 to 0x80151A88. New: 0x800C42CC to
  0x800C42D8, 0x8014A92C to 0x8014A930 and 0x80151A84 to 0x80151A88.
- The jump tables, the text bounds and the 16 unresolved jumps are unchanged.
- The sources of 353 other functions changed, though the functions did not:
  with 0x8014A930 and 0x80151A70 reached from the start, the second round
  works with a text range up to 0x800EC188 instead of 0x800CA4D0, and the
  passes find starts in a different order: 227 functions lose the prologue
  source (the start was already code when the scan ran), 124 gain it, and 2
  gain pointer. In the same way, 20 of the starts the hoist had overshot are
  now never proposed, which is why fewer are withdrawn.
- 0x800DE094, an `addiu sp,sp,-16` in the delay slot of a branch inside
  0x800DE084, was proposed by the prologue scan before 0x800DE084 was reached
  in this order. A prologue-only start that reached code takes as a delay slot
  is now withdrawn as well, like one that reached code runs into.

Against that run, the 16 `[[jump_table]]` and 2 `[[interpreted]]` entries of
M1 Task 5 changed this:

- Reached: the 9 unreached ranges holding the case blocks of the 16 jumps
  (488 bytes) are code now: 0x8012DCB0 to 0x8012DD50, 0x8015017C to
  0x80150200 and the seven 28-byte tails (listed under "Manual jump tables").
  No function was added, removed or resized; 0x8014BD78 gains the source
  `call` (a case block of the table at 0x80150174 calls it).
- 91 jump tables instead of 75; no `jr` is left unresolved.
- `interpreted` lists 0x80151AC0 and 0x80151B50. They stay in the function
  list and the analysis descends into them as before.

## Where the algorithm departs from the brief, and why

1. **text_end** follows the island rule above. Without it, the stub at
   0x801A355C moves text_end to 0x801A356C. The data pointer scan then sees
   only the last 3 KB of the image, and unreached jumps to 34%.
2. **Prologue scan.** Every `addiu sp,sp,-n` inside a gap starts a function,
   not only one at the start of a gap. The function's start is then moved
   back over instructions scheduled above the stack adjustment. GCC hoists
   loads there: the most common pattern is `lui; lw; addiu sp,sp,-n`. The
   start moves back by up to 8 instructions. It stops at a delay slot or a
   control transfer, and drops any instruction that reads a register not live
   at entry (zero, a0-a3, gp, sp and ra are live), reads hi/lo or COP0, or
   writes $zero other than the canonical nop. Leading zero words are padding.
   This rule set leaves out the padding words that come before a function
   (for example 0x0000003F at 0x800D3FFC and 0x8C63F0BC at 0x80100DBC).
   With the brief's literal rule (a gap starting with the prologue), the
   result was 1,815 functions and 12.19% unreached, much of it real code
   behind a dead tail or a leaf in the same gap. (That was measured before
   the withdrawal step below was added.) With this rule it is 2,093
   functions and 4.57% (with the manual input; 4.64% before its jump tables), still with no unknown word
   in reached code.

   Because the passes run in rounds, a later pass can prove a prologue start
   wrong. After each round, a start whose only source is `prologue` is
   withdrawn in two cases:
   - reached code runs straight into it, or reaches it as the delay slot of
     a branch. One case: 0x800D1588 is inside 0x800D1574, whose stack
     adjustment is 13 instructions in, past the 8-instruction limit. The
     `jal` to 0x800D1574 was found only after the scan. The delay-slot case
     is 0x800DE094, inside 0x800DE084.
   - its hoisted run of plain instructions runs straight into another start
     that a later pass found. Its words become unreached again.

   5 starts are withdrawn: 0x800D1588, 0x800DE094, and 3 single words before
   a function, which the hoist took in before a pointer or call found the
   real start (0x80150DFC, 0x80151A84 and 0x80160624). A withdrawn start is
   never proposed again.
3. **Jump-table base.** GCC builds the table address as
   `lui at,%hi; addu at,at,idx; lw r,%lo(at)`, with the low half in the `lw`
   offset. The brief's `lui`/`addiu` form is also accepted. No entry of the
   75 tables falls outside the function that holds its `jr` (a manual table is
   refused if one does).
4. **Tail calls.** A `j` to another known function start is a tail call, as
   the ruling says. A `j` out of the function it sits in, to an address that
   is not a known start, is a tail call only when the target is a
   stack-adjust prologue. The function a `j` belongs to is worked out from
   the starts known so far, so taking any such target as a start would let
   one doubtful start split a real function. Before review round 1 that
   happened: 0x80151BD4, 0x80151CE4 and 0x80151F9C were labels inside one
   function. The prologue case still finds `main` (0x800C4524), which
   `_start` reaches only through its final `j`. It is the one function known
   only through a `j`. The other out-of-function jumps are reported, not
   taken (see "Jumps out of their function" below).
5. **Sizes.** A function ends at the next function start, less any unreached
   words just before it. This keeps the data islands out of functions. A
   function before a stub block ends at the block, and a stub is one slot.
6. **Jump tables are all or nothing.** A table is recorded only if every
   entry is a word address inside the text; otherwise its `jr` is listed as
   unresolved and discovery runs again without it, so `targets[k]` is always
   case k. All 75 tables pass. (The manual tables are checked differently:
   every target must lie inside the function of the `jr`.)

## Unreached ranges

253 ranges, 31,772 bytes (was 262, 32,260 before the manual jump tables).
functions.toml lists them all; they fall into these kinds:

- **Data block inside the text: 0x80110B04 to 0x80114560** (14,940 bytes, 802
  words that do not decode). Code addresses it through lui pairs (11 of
  them).
- **Isolated reserved words between functions** (the census's islands):
  0x800C81B4, 0x800CBB6C, 0x800D6EEC, 0x800DB144, 0x800E7184, 0x800EC53C,
  0x800F1D84. All seven are unreached and lie outside every function. Six
  are single-word ranges; 0x800C81B4 shares its range (0x800C819C to
  0x800C81B8) with an unreached leaf just before it.
- **80 isolated single words** between reached code that decode as valid
  instructions (320 bytes). They are padding of the same kind, for example
  0x8C63F0BC at 0x80100DBC. 2 of them (0x80150DFC and 0x80151A84) are words
  the withdrawal step gave back; the third word it gave back (0x80160624)
  joined the neighbouring leaf range.
- **6 zero-padding runs** (24 bytes).
- **149 ranges of leaf-shaped code** (16,204 bytes): clean decode, ending in
  `jr ra`, no stack frame. For 141 of them no pointer value in the image
  lands anywhere inside the range, so they are either dead code or leaves
  reached in a way this analysis cannot see. The other 8 contain a
  code-built address (see the refused pointers below). The largest are
  0x800F3008 to 0x800F3580 (1,400 bytes) and 0x8012F6C8 to 0x8012FBF0
  (1,320 bytes). Among them, 0x80167FB8 to 0x80167FF4 holds four
  Status-register helpers (`mfc0`/`mtc0` on register 12). These are the only
  COP0 moves in the text, and nothing references them.
- **10 other clean ranges** (232 bytes) with no `jr ra`. (There were 19,
  720 bytes: the other 9, 488 bytes, were the case blocks of the 16 jumps
  below, reached code now.)

2 ranges (16 bytes) end inside a function rather than at a function start:
0x800D87FC to 0x800D8804 and 0x80151BDC to 0x80151BE4. (There were 11; the
other 9 were the case blocks.)

## Manual jump tables (16)

The analysis finds no table within the brief's 12-instruction window for 16
`jr`s. Each has a `[[jump_table]]` entry in `config/game.manual.toml`, with
the targets read from the table in the file (case k is entry k) and a reason
naming the instructions below; `functions.toml` records them with
`table_addr = 0`.

- 0x8012DCA8: a switch whose `sltiu` lies outside the window. The dispatch is
  reached only by the `bne` at 0x8012D97C, whose delay slot is
  `sltiu v0,s0,28`; the `beq` at 0x8012DC94 sends indexes 28 and up to
  0x8012DD50, then `lui at` / `addu` / `lw v0,0x4328(at)` read the table
  0x80174328 (28 entries; the next table, of the `jr` at 0x8012DE14, starts at
  0x80174398). Cases 0x8012DCB0 to 0x8012DD50.
- 0x80150174: a switch with an `sltiu` bound of 5 (0x80150160). The table
  base is held in s5, set to 0x801757A8 at 0x80150108 and 0x8015010C and not
  written again before the epilogue. 5 entries, cases 0x8015017C to
  0x80150200. The switch lies inside the loop that branches back at
  0x80150248.
- 0x80164484, 0x801644EC, 0x80164630, 0x80164698, 0x801647D0, 0x80164830,
  0x80164938, 0x80164998, 0x80164EB4, 0x80164F1C, 0x80165040, 0x801650A8,
  0x80165290, 0x801652F8: `lw v0,0(reg); jr v0` dispatches in a family of
  block-copy routines (0x801643AC, 0x80164558, 0x80164708, 0x80164DD0,
  0x80165124), two per copy loop. Before the loop, each routine computes
  `sltiu tN,idx,7`, then `sll v1,idx,2` and, for each of the loop's two
  dispatches, a base register = `v1` + a table address built by a `lui` /
  `addiu` pair. Inside the loop each dispatch is reached only past
  `beq tN,zero` (index 7 and up skip it), and the base is not written again
  on any path to the `lw` (reaching definitions over the function; the case
  tails write only v0). The tables are 7 words each, separated by a zero
  word: 0x80176AD8 to 0x80176C90, two per tail. Each table's entries are the
  four labels of the dispatch's own 28-byte tail: 0x801644F4, 0x801646A0,
  0x80164838, 0x801649A0, 0x80164F24, 0x801650B0 and 0x80165300.

## Unresolved indirect jumps (0)

With the manual tables, no `jr` on a register other than ra is left
unresolved, so the recompiler emits no `rt_jump` for one. A `jr` added to
this list must be checked not to be the back edge of a loop: `rt_jump` to an
address that is not a function start runs the interpreter as a nested call.

## Jumps out of their function

2 `j` instructions leave their function for an address that is neither a
known start nor a prologue: 0x80151B48 and 0x80151B78, both to 0x80151E90.
0x80151E90 begins `addi sp,sp,204` and then reloads registers from the
stack. It looks like the shared restore path of hand-written save/restore
code around 0x80151AC0 to 0x80152008, which is not GCC output. The
recompiler has to treat it as a jump between functions.

It is a context switch. 0x80151AC0 swaps sp and k1 (three `xor`s at
0x80151AE4), saves s0-s7, fp, ra and FP registers below the stack it switched
to and jumps to 0x80151E90; 0x80151B50 calls 0x80149C58, swaps sp and k1 the
same way and jumps there too. 0x80151E90 lies inside 0x80151BC0, which loops
over a list of saved contexts: it restores a context's registers and returns
into it with `jr ra` at 0x80151E88, and 0x80151E90 is where it carries on when
a context switches back. A manual start at 0x80151E90 does not recompile:
0x80151BC0 then jumps into the new function (`j 0x80151F9C` at 0x80151CF4) and
the new function jumps back (`j 0x80151CE4` at 0x80151F94), neither target a
function start. So 0x80151AC0 and 0x80151B50 are `[[interpreted]]` entries
in the manual file: the recompiler emits each as a call of `interp_run`, and
their `j` runs on in the interpreter. Returning into another context with
`jr ra`, or switching stacks under a C call chain, is a runtime question for
M2 whichever side runs the code.

What the switch is for (read, M1): it is the game's cooperative task
scheduler. `main` calls 0x80151BC0 once (0x800C4740). The task list starts
at the word 0x801A3110 and the current task is the word 0x801A3118; a task
record holds the next task at +0, the saved stack pointer at +8, a sleep
counter (halfword) at +0xC, an argument at +0x10 and the task's function at
+0x24. 0x80151AC0 is the yield: it ORs its argument into the sleep counter,
saves the task's callee-saved registers on the task's own stack and its
stack pointer at +8. The scheduler decrements each task's counter once per
pass and resumes a task whose counter reaches 0. 0x80151AC0 is called from
70 `jal` sites in 32 functions. The two task-creation routines 0x80148FA4
and 0x80149200 plant 0x8014A930 where the restore path reloads `ra`, so a
new task's first resume enters 0x8014A930, which calls the task's function
(+0x24) with the argument (+0x10) and, when it returns, jumps to 0x80151B50,
the exit path. The [M1 report](M1-report.md) ("Risks and decisions for M2")
has what this means for the runtime and what is not known yet.

## Pointers refused by the prologue-or-known-start rule

8 pointer values, from data words or lui pairs, land somewhere inside an
unreached range that holds no undecodable word (was 47: the other 39 were case
labels of the 16 jumps that have manual tables now, 6 in 0x8012DCB0 to
0x8012DD3C, 5 in 0x8015017C to 0x801501F0 and 4 in each of the seven 28-byte
tails). Values inside the data block at 0x80110B04 are data pointers and are
not counted.

- 0x800C42CC: the stub block's end, from `_start`'s copy loop. (The block's
  start, 0x800C408C, is no longer counted: the block is not unreached.)
- 7 probable callbacks: 0x800C6F28, 0x800FE924, 0x800FEDB0, 0x80101CD8,
  0x80102454, 0x801150C8 and 0x801478C0. Code builds each of these addresses
  with a lui pair, and each lands on code with no stack frame. 0x801478C0
  lies right after a `jr ra` delay slot in the middle of its range. They are
  candidates for `manual` starts; the rule as specified refuses them. Three
  more of this kind, 0x800C6030, 0x8014A930 and 0x80151A70, were seen
  running or registered in MAME and are manual starts now
  (`config/game.manual.toml`).

## Census of reached code

Restricted to the 165,760 reached words (all functions, minus the unreached
ranges and the stub block). This is the list of instructions the recompiler
has to handle. The 122 words the manual jump tables added (165,638 before
them) changed ten counts and not the set of 98 mnemonics. The earlier counts
were addiu 26682, lw 16445, lui 15999, addu 14937, sw 14706, sll 12824,
jal 11199, beq 4834, j 4232 and jalr 63.

Unknown words: 0.

Mnemonic counts: addiu 26713, lw 16461, lui 16011, addu 14946, sw 14719,
sll 12831, jal 11200, lwc1 6678, beq 4835, swc1 4481, j 4263, bne 3446,
sh 2755, jr 2139, ori 1802, lhu 1604, mul.s 1589, andi 1513, slti 1497,
add.s 1221, lh 1152, sub.s 1106, sra 928, ldc1 801, mtc1 791, sdc1 697,
or 616, mfc1 551, lbu 513, bc1f 510, c.lt.s 484, subu 479, and 474, slt 459,
cvt.s.w 443, mov.s 389, beql 325, bnel 323, blez 312, sltiu 310, div.s 306,
sb 258, srl 210, c.le.s 209, bc1t 182, sltu 166, c.eq.s 137, bgez 134,
neg.s 128, cvt.d.s 118, trunc.w.s 107, bc1tl 102, cvt.s.d 102, movz 82,
mult 73, xor 72, bc1fl 71, mflo 65, jalr 64, madd.s 64, lb 60, add.d 59,
cvt.d.w 54, bltz 53, mul.d 48, break 39, bgtz 35, movn 33, nor 32, mfhi 30,
xori 30, c.lt.d 24, sllv 23, c.le.d 22, bltzl 21, sub.d 21, cache 20,
mov.d 18, c.eq.d 16, div 15, msub.s 14, div.d 13, bgezl 11, addi 10,
trunc.w.d 8, madd.d 6, nmsub.s 6, movf 5, blezl 4, divu 4, movt 2, bgtzl 1,
ctc1 1, multu 1, neg.d 1, nmadd.s 1, recip.s 1, sqrt.s 1.

The one `ctc1` is in `_start`, where it sets FCSR. (Before the stub block
was declared, one `ldl` was counted: the stub-block artefact at 0x800C42CC.)

MIPS IV instructions: movz 82, movn 33, movf 5, movt 2, madd.s 64, madd.d 6,
msub.s 14, nmsub.s 6, nmadd.s 1, recip.s 1. No pref, prefx, indexed FP
loads/stores, movf.fmt/movt.fmt, movz.fmt/movn.fmt, rsqrt, msub.d or nmadd.d.

FP condition codes other than 0: 4 uses, all in one function: c.lt.s cc1
0x80155B18, bc1f cc1 0x80155B1C, c.lt.s cc2 0x80155B20, bc1fl cc2 0x80155B24.
The movf/movt uses all test cc0.

COP0: cache 20, in the cache-maintenance routines at 0x80155A98, 0x80167D58,
0x80169134, 0x80169280 and 0x80169384. There is no mfc0, mtc0, dmfc0, dmtc0,
eret, wait, tlb* or bc0* in reached code. Every COP0 move, `wait` and `bc0*`
in the whole-image census lies in the data, apart from the four unreferenced
Status helpers above.

Traps: there are 39 `break`s. 34 are the divide-by-zero (`break 7`, word
0x0007000D, 19) and overflow (`break 6`, word 0x0006000D, 15) checks that
GCC emits; 2 of them are in the VBLANK handler 0x800C6030.
4 are the word 0x000101CD, at 0x801447BC, 0x80144814, 0x801448B4 and
0x80151AD0. The last is the stub at 0x801A355C (word 0x0001024D). There is
no syscall and no conditional trap.

The whole-image census flagged `cache`, the traps, `bc0*` and `wait` as
mostly data. Only `cache` (20) and `break` (39) occur in reached code. No
word reached as code is a reserved encoding, so the decoder needs no change.
