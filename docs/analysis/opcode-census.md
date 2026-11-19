# GAME.EXE opcode census

Produced by `python3 -m carnevil_tools census GAME.EXE` (decoder: `carnevil_tools/mips.py`, IDT R5000, MIPS IV, little-endian). Counts and addresses only.

## Scope

The census covers the whole loaded image, 0x800C4000 to 0x801A4224, which is 229,513 words. It does not separate code from data, so every word is decoded as an instruction. Data words that happen to be valid encodings inflate some counts. The clearest case is `movf` (3,007): a word whose value is 1 decodes as `movf zero,zero,cc0`. The same applies to `cache`, the trap instructions, `bc0*` and `wait`. Task 4 narrows the count to the code section.

0xFFFFFFFF decodes as `sd` (opcode 0x3F), not as an unknown word.

## Result in one line

18,545 of the 229,513 words are unknown. Not present at all: `sdxc1`, `swxc1`, `prefx`, `nmsub.d`, `movf.fmt`, `movt.fmt`, `movn.fmt`, `movz.s`, `recip.d`, `rsqrt.fmt`.

## MIPS IV instructions present (count)

- movf 3007 (mostly data, see above)
- movz 294
- movn 91
- movt 63
- movz.d 1
- pref 9
- lwxc1 6
- ldxc1 1
- madd.s 95
- madd.d 6
- msub.s 15
- msub.d 1
- nmadd.s 4
- nmadd.d 13
- nmsub.s 14
- recip.s 1
- FP condition code other than 0 on c.cond.fmt, bc1* or movf/movt (any form): 134 uses

## COP0 instructions present (count)

- cache 1131
- mfc0 56
- mtc0 50
- dmfc0 13
- dmtc0 42
- wait 42
- bc0f 35
- bc0t 10
- bc0fl 8
- bc0tl 6

`eret` and `tlbr`/`tlbwi`/`tlbwr`/`tlbp` do not occur. The COP0 moves and `wait` counts are low and may be partly data. Task 5 and Task 6 decide which are real.

## Unknown words

18,545 words decode as unknown.

First 20 addresses: 0x800C81B4, 0x800CBB6C, 0x800D6EEC, 0x800DB144, 0x800E7184, 0x800EC53C, 0x800F1D84, 0x80110B04, 0x80110B60, 0x80110B64, 0x80110B8C, 0x80110B9C, 0x80110BAC, 0x80110BB4, 0x80110BB8, 0x80110BE8, 0x80110C08, 0x80110C0C, 0x80110C34, 0x80110C44.

Most of these are expected to be data. The isolated ones low in the image (0x800C81B4 to 0x800F1D84) are the ones to check when Task 4 fixes the code boundary.

## Mnemonic counts (all 229,513 words)

addiu 27146, sll 26042, lui 20019, unknown 18545, lw 16846, addu 15217, sw 15112, jal 11296, lwc1 8170, beq 4926, j 4768, swc1 4566, bne 3591, lb 3270, movf 3007, sh 2849, jr 2730, ori 2041, andi 2035, beql 1883, sra 1880, lhu 1662, mul.s 1633, slti 1558, add.s 1255, ldr 1209, lh 1207, daddiu 1195, sub.s 1141, cache 1131, ll 892, mtc1 849, sltiu 823, ldc1 809, addi 710, sdc1 708, srl 706, bnel 672, or 643, subu 602, mfc1 587, xori 558, ldl 535, lbu 530, and 527, bc1f 520, sllv 508, c.lt.s 504, slt 482, cvt.s.w 454, daddi 445, mov.s 398, swr 347, srav 340, blez 338, div.s 313, syscall 312, movz 294, break 287, sb 285, c.le.s 210, bc1t 190, sltu 179, div 148, bgez 147, c.eq.s 138, add 134, neg.s 131, jalr 127, tge 125, mult 124, scd 122, tltu 121, cvt.d.s 118, trunc.w.s 111, bc1tl 108, mflo 105, teq 105, tlt 105, cvt.s.d 102, tgeu 102, xor 97, madd.s 95, dsra32 94, dsllv 92, movn 91, tne 89, mfhi 84, sd 75, bc1fl 74, bltz 74, srlv 67, lwl 65, movt 63, add.d 59, mfc0 56, cvt.d.w 54, sync 54, dsrl32 52, bgtz 51, mtc0 50, dsll 49, dsrl 49, mul.d 48, bltzl 45, dsll32 44, nor 44, blezl 43, dmtc0 42, wait 42, dsrav 40, dsrlv 38, ddiv 37, bc0f 35, bgtzl 34, sub 33, bgezl 29, ddivu 28, mthi 28, multu 25, c.lt.d 24, dmfc1 24, mtlo 24, c.le.d 22, dsub 22, sub.d 22, divu 21, tgei 21, daddu 20, dadd 19, dmult 19, dsra 19, lwr 19, dmultu 18, mov.d 18, tgeiu 18, bgezall 17, c.eq.d 17, teqi 16, bltzall 15, msub.s 15, tltiu 15, nmsub.s 14, div.d 13, dmfc0 13, nmadd.d 13, sdl 13, swl 12, tlti 12, ld 11, tnei 11, bc0t 10, bgezal 10, bltzal 9, lld 9, pref 9, bc0fl 8, cfc1 8, ctc1 8, sc 8, sdr 8, trunc.w.d 8, dmtc1 7, bc0tl 6, dsubu 6, lwu 6, lwxc1 6, madd.d 6, nmadd.s 4, c.un.d 3, c.ult.d 1, cvt.l.d 1, ldxc1 1, movz.d 1, msub.d 1, neg.d 1, recip.s 1, sqrt.s 1.
