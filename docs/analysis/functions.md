# GAME.EXE function discovery

Produced by `python3 -m carnevil_tools functions GAME.EXE -o config/game.functions.toml`
(`carnevil_tools/functions.py`, names from `carnevil_tools/naming.py`). The
command prints every number below. `config/game.functions.toml` holds the
result: text bounds, functions (address, size, name, sources), jump tables
(the `jr`, the table address, the targets) and the unreached ranges. Counts and
addresses only.

Entry: 0x800C4000 (the load address).

## Result in one paragraph

The text runs from 0x800C4000 to 0x8016DC4C (695,372 bytes). There are 2,091
functions and 75 jump tables (1,488 entries, the largest has 121). One of the
functions sits in the data, past text_end (0x801A355C, below). 165,748 words are
reached as code and none of them decodes as unknown. Outside the boot-ROM stub
block, no function runs straight into the next one. 263 ranges (32,396 bytes,
4.66% of the text) are not reached; 15 KB of that is one data block inside the
text. 16 indirect jumps could not be resolved; their targets (about 1 KB of
code) are among the unreached ranges, listed below.

## Functions by source

A function counts once under each of its sources:

- entry: 1
- call: 1,241 (`jal`, plus tail calls by `j`, see departure 4)
- pointer: 390
- prologue: 1,085
- manual: 0

The combinations that occur: call 941, call+prologue 291, call+pointer 1,
call+pointer+prologue 8, pointer 63, pointer+prologue 318, prologue 468,
entry 1.

The lui/addiu (and lui/ori) pass over reached code found 47 function starts
that no other pass had found when it ran. These are callbacks whose address is
built in code: the `pointer` source covers both this pass and the data scan.
The pass runs before the prologue scan in each round, so some of the 47 have a
prologue and would also have been found by it.

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

## The boot-ROM stub block at 0x800C408C to 0x800C42CC

In the file these 576 bytes are all zero. 29 call targets (0x800C40A4 to
0x800C42AC) lie inside the block. `_start` fills it at boot by copying from
0xA0000000, which is physical address 0 and was left there by the boot ROM.
The descent reads the zeros as `nop`s and follows them to the next `jr ra`,
which is how 0x800C42CC (the bytes "spri", decoded as `ldl`) comes to count
as reached code. The sizes of these 29 functions in functions.toml come from
the zeros in the file and mean nothing. The recompiler must not translate
this block from the file: its contents come from the boot ROM (Task 5).

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
   the withdrawal step below was added.) With this rule it is 2,091
   functions and 4.66%, still with no unknown word in reached code.

   Because the passes run in rounds, a later pass can prove a prologue start
   wrong. After each round, a start whose only source is `prologue` is
   withdrawn in two cases:
   - reached code runs straight into it. One case: 0x800D1588 is inside
     0x800D1574, whose stack adjustment is 13 instructions in, past the
     8-instruction limit. The `jal` to 0x800D1574 was found only after the
     scan.
   - its hoisted run of plain instructions runs straight into another start
     that a later pass found. Its words become unreached again.

   24 starts are withdrawn: 0x800D1588, and 23 single words before a
   function, which the hoist took in before a pointer or call found the real
   start (for example 0x800F38C4 before 0x800F38C8). A withdrawn start is
   never proposed again.
3. **Jump-table base.** GCC builds the table address as
   `lui at,%hi; addu at,at,idx; lw r,%lo(at)`, with the low half in the `lw`
   offset. The brief's `lui`/`addiu` form is also accepted. No entry of the
   75 tables falls outside the function that holds its `jr`.
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
   words just before it. This keeps the data islands out of functions.

## Unreached ranges

263 ranges, 32,396 bytes. functions.toml lists them all; they fall into these
kinds:

- **Data block inside the text: 0x80110B04 to 0x80114560** (14,940 bytes, 802
  words that do not decode). Code addresses it through lui pairs (11 of
  them).
- **Isolated reserved words between functions** (the census's islands):
  0x800C81B4, 0x800CBB6C, 0x800D6EEC, 0x800DB144, 0x800E7184, 0x800EC53C,
  0x800F1D84. All seven are unreached and lie outside every function. Six
  are single-word ranges; 0x800C81B4 shares its range (0x800C819C to
  0x800C81B8) with an unreached leaf just before it.
- **78 isolated single words** between reached code that decode as valid
  instructions (312 bytes). They are padding of the same kind, for example
  0x8C63F0BC at 0x80100DBC. 21 of them are words the withdrawal step gave
  back; the other 2 words it gave back (0x80151A84 and 0x80160624) joined
  the neighbouring leaf ranges.
- **7 zero-padding runs** (48 bytes).
- **149 ranges of leaf-shaped code** (16,216 bytes): clean decode, ending in
  `jr ra`, no stack frame. For 141 of them no pointer value in the image
  lands anywhere inside the range, so they are either dead code or leaves
  reached in a way this analysis cannot see. The other 8 contain a code-built
  address (see the refused pointers below). The largest are 0x800F3008 to
  0x800F3580 (1,400 bytes) and 0x8012F6C8 to 0x8012FBF0 (1,320 bytes). Among them, 0x80167FB8 to 0x80167FF4 holds four
  Status-register helpers (`mfc0`/`mtc0` on register 12). These are the only
  COP0 moves in the text, and nothing references them.
- **21 other clean ranges** (828 bytes) with no `jr ra`. 488 bytes of that
  are the case blocks of the unresolved indirect jumps below.

11 ranges (504 bytes) end inside a function rather than at a function start.
9 are the case blocks of the unresolved jumps; the other two are
0x800D87FC to 0x800D8804 and 0x80151BDC to 0x80151BE4.

## Unresolved indirect jumps (16)

None of these has a table within the brief's 12-instruction window. The next
milestone needs their targets:

- 0x8012DCA8: a switch whose `sltiu` lies outside the window (the dispatch is
  reached by a branch). The table is 0x80174328 and its cases are 0x8012DCB0
  to 0x8012DD50.
- 0x80150174: a switch with an `sltiu` bound of 5. The table base is held in
  s5 from earlier in the function. The table is 0x801757A8 (5 entries), the
  cases 0x8015017C to 0x80150200.
- 0x80164484, 0x801644EC, 0x80164630, 0x80164698, 0x801647D0, 0x80164830,
  0x80164938, 0x80164998, 0x80164EB4, 0x80164F1C, 0x80165040, 0x801650A8,
  0x80165290, 0x801652F8: `lw v0,0(reg); jr v0` dispatches in a family of
  block-copy routines. The routines go through tables of code labels (for
  example 0x80176AD8) to the 28-byte tails at 0x801644F4, 0x801646A0,
  0x80164838, 0x801649A0, 0x80164F24, 0x801650B0 and 0x80165300.

## Jumps out of their function

2 `j` instructions leave their function for an address that is neither a
known start nor a prologue: 0x80151B48 and 0x80151B78, both to 0x80151E90.
0x80151E90 begins `addi sp,sp,204` and then reloads registers from the
stack. It looks like the shared restore path of hand-written save/restore
code around 0x80151AC0 to 0x80152008, which is not GCC output. The
recompiler has to treat it as a jump between functions.

## Pointers refused by the prologue-or-known-start rule

50 pointer values, from data words or lui pairs, land somewhere inside an
unreached range that holds no undecodable word. Values inside the data block
at 0x80110B04 are data pointers and are not counted.

- 0x800C408C: the stub block's start, from `_start`'s copy loop.
- 39 case labels of the unresolved jumps:
  - 6 in 0x8012DCB0 to 0x8012DD3C;
  - 5 in 0x8015017C to 0x801501F0;
  - 4 in each of the seven 28-byte tails at 0x801644F4, 0x801646A0,
    0x80164838, 0x801649A0, 0x80164F24, 0x801650B0 and 0x80165300.
- 10 probable callbacks: 0x800C6030, 0x800C6F28, 0x800FE924, 0x800FEDB0,
  0x80101CD8, 0x80102454, 0x801150C8, 0x801478C0, 0x8014A930, 0x80151A70.
  Code builds each of these addresses with a lui pair, and each lands on code
  with no stack frame. 0x801478C0 lies right after a `jr ra` delay slot in the
  middle of its range; 0x8014A930 lies one word in. They are candidates for
  `manual` starts; the rule as specified refuses them.

## Census of reached code

Restricted to the 165,748 reached words (all functions, minus the unreached
ranges). This is the list of instructions the recompiler has to handle.

Unknown words: 0.

Mnemonic counts: addiu 26677, lw 16439, lui 15995, addu 14936, sw 14705,
sll 12959, jal 11199, lwc1 6678, beq 4834, swc1 4481, j 4231, bne 3443,
sh 2755, jr 2139, ori 1802, lhu 1604, mul.s 1589, andi 1513, slti 1497,
add.s 1221, lh 1152, sub.s 1106, sra 928, ldc1 801, mtc1 791, sdc1 697,
or 616, mfc1 551, lbu 513, bc1f 510, c.lt.s 484, subu 479, and 474, slt 459,
cvt.s.w 443, mov.s 389, beql 325, bnel 323, blez 312, sltiu 310, div.s 306,
sb 258, srl 210, c.le.s 209, bc1t 182, sltu 166, c.eq.s 137, bgez 134,
neg.s 128, cvt.d.s 118, trunc.w.s 107, bc1tl 102, cvt.s.d 102, movz 82,
mult 73, xor 72, bc1fl 71, mflo 65, madd.s 64, jalr 62, lb 60, add.d 59,
cvt.d.w 54, bltz 53, mul.d 48, break 37, bgtz 35, movn 33, nor 32, xori 30,
mfhi 29, c.lt.d 24, sllv 23, c.le.d 22, bltzl 21, sub.d 21, cache 20,
mov.d 18, c.eq.d 16, div 14, msub.s 14, div.d 13, bgezl 11, addi 10,
trunc.w.d 8, madd.d 6, nmsub.s 6, movf 5, blezl 4, divu 4, movt 2, bgtzl 1,
ctc1 1, ldl 1, multu 1, neg.d 1, nmadd.s 1, recip.s 1, sqrt.s 1.

The one `ldl` is the stub-block artefact at 0x800C42CC. The one `ctc1` is in
`_start`, where it sets FCSR.

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

Traps: there are 37 `break`s. 32 are the divide-by-zero (`break 7`, word
0x0007000D) and overflow (`break 6`, word 0x0006000D) checks that GCC emits.
4 are the word 0x000101CD, at 0x801447BC, 0x80144814, 0x801448B4 and
0x80151AD0. The last is the stub at 0x801A355C (word 0x0001024D). There is
no syscall and no conditional trap.

The whole-image census flagged `cache`, the traps, `bc0*` and `wait` as
mostly data. Only `cache` (20) and `break` (37) occur in reached code. No
word reached as code is a reserved encoding, so the decoder needs no change.
