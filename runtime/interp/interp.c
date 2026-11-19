/* interp.c: the reference interpreter.
 *
 * It covers the reached census of docs/analysis/functions.md (98 mnemonics) plus cfc1, and
 * accepts exactly the encodings carnevil_tools/mips.py decodes as those mnemonics (fields
 * the decoder requires to be zero must be zero). Any other word stops the process with its
 * address and the word, never a silent no-op.
 *
 * interp_run fetches from RAM and runs until the function it was entered in returns:
 * - jal / jalr: the link is written and the call target read before the delay slot; after
 *   the slot, rt_call, then execution continues after the slot;
 * - jr ra: after the slot, the run ends;
 * - any other jr, and j: after the slot, if recomp_lookup knows the target it is a tail call
 *   (rt_call, then the run ends), otherwise execution continues at the target;
 * - a branch decides before its slot runs; a branch-likely not taken skips its slot;
 * - break: rt_break(ram, c, pc, code), then the next instruction.
 * A control transfer in a delay slot is an error. Interrupt check points (RT_CHECK, see
 * recomp.h): at entry, and after the delay slot of a taken branch or jump that continues in
 * this run at an address at or below its own; a jump that leaves through rt_call does not
 * check (the callee's entry does).
 */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

#include "recomp.h"

#define RS(w) ((w) >> 21 & 31u)
#define RT(w) ((w) >> 16 & 31u)
#define RD(w) ((w) >> 11 & 31u)
#define SA(w) ((w) >> 6 & 31u)
#define FUNCT(w) ((w) & 63u)
#define SIMM(w) ((((w) & 0xFFFFu) ^ 0x8000u) - 0x8000u) /* sign-extended, as uint32_t */
#define UIMM(w) ((w) & 0xFFFFu)

#define M_RS 0x03E00000u
#define M_RT 0x001F0000u
#define M_RD 0x0000F800u
#define M_SA 0x000007C0u

static _Noreturn void stop(const char *fmt, ...)
{
    va_list ap;
    fflush(stdout);
    fputs("interp: ", stderr);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    exit(EXIT_FAILURE);
}

static _Noreturn void unsupported(uint32_t pc, uint32_t w)
{
    stop("unsupported instruction %08X at %08X", w, pc);
}

/* --- entry log -------------------------------------------------------------------------- */

static uint8_t entered[RAM_SIZE / 4 / 8]; /* one bit per RAM word, kseg0 and kseg1 alike */

void interp_forget_entries(void)
{
    memset(entered, 0, sizeof entered);
}

static void note_entry(uint32_t pc)
{
    if (mem_is_ram(pc, 4)) {
        uint32_t i = (pc & 0x1FFFFFFFu) >> 2;
        if (entered[i >> 3] & 1u << (i & 7))
            return;
        entered[i >> 3] |= (uint8_t)(1u << (i & 7));
    }
    if (rt_log_unrecompiled)
        rt_log_unrecompiled(pc);
}

static uint32_t fetch(uint8_t *ram, uint32_t pc)
{
    uint32_t w;
    if ((pc & 3u) || !mem_is_ram(pc, 4))
        stop("fetch from %08X, which is not aligned RAM", pc);
    memcpy(&w, ram + (pc & 0x1FFFFFFFu), 4);
    return w;
}

/* --- straight-line instructions ----------------------------------------------------------- */

/* The bits a SPECIAL instruction of the census requires to be zero (as mips.py decodes),
 * or ~0 for a funct outside the census (jr and jalr are control transfers, handled apart). */
static uint32_t special_zero(uint32_t funct)
{
    switch (funct) {
    case 0x00: case 0x02: case 0x03: return M_RS;                    /* sll srl sra */
    case 0x01: return M_SA | 2u << 16;                               /* movf movt: nd = 0 */
    case 0x04: case 0x0A: case 0x0B: case 0x21: case 0x23: case 0x24: case 0x25: case 0x26:
    case 0x27: case 0x2A: case 0x2B: return M_SA;
    case 0x0D: return 0;                                             /* break */
    case 0x10: case 0x12: return M_RS | M_RT | M_SA;                 /* mfhi mflo */
    case 0x18: case 0x19: case 0x1A: case 0x1B: return M_RD | M_SA;  /* mult multu div divu */
    default: return ~0u;
    }
}

static void exec_special(uint8_t *ram, cpu_t *c, uint32_t pc, uint32_t w)
{
    uint32_t *r = c->r;
    uint32_t rs = r[RS(w)], rt = r[RT(w)];
    uint32_t *rd = &r[RD(w)];
    if (w & special_zero(FUNCT(w)))
        unsupported(pc, w);
    switch (FUNCT(w)) {
    case 0x00: *rd = rt << SA(w); return;                                    /* sll */
    case 0x01:                                                               /* movf movt */
        if (rc_cc(c, RT(w) >> 2) == (int)(RT(w) & 1u))
            *rd = rs;
        return;
    case 0x02: *rd = rt >> SA(w); return;                                    /* srl */
    case 0x03: *rd = (uint32_t)((int32_t)rt >> SA(w)); return;               /* sra */
    case 0x04: *rd = rt << (rs & 31u); return;                               /* sllv */
    case 0x0A: *rd = rt == 0 ? rs : *rd; return;                             /* movz */
    case 0x0B: *rd = rt != 0 ? rs : *rd; return;                             /* movn */
    case 0x0D: rt_break(ram, c, pc, w >> 6 & 0xFFFFFu); return;              /* break */
    case 0x10: *rd = c->hi; return;                                          /* mfhi */
    case 0x12: *rd = c->lo; return;                                          /* mflo */
    case 0x18: rc_mult(c, rs, rt); return;                                   /* mult */
    case 0x19: rc_multu(c, rs, rt); return;                                  /* multu */
    case 0x1A: rc_div(c, rs, rt); return;                                    /* div */
    case 0x1B: rc_divu(c, rs, rt); return;                                   /* divu */
    case 0x21: *rd = rs + rt; return;                                        /* addu */
    case 0x23: *rd = rs - rt; return;                                        /* subu */
    case 0x24: *rd = rs & rt; return;                                        /* and */
    case 0x25: *rd = rs | rt; return;                                        /* or */
    case 0x26: *rd = rs ^ rt; return;                                        /* xor */
    case 0x27: *rd = ~(rs | rt); return;                                     /* nor */
    case 0x2A: *rd = (int32_t)rs < (int32_t)rt; return;                      /* slt */
    case 0x2B: *rd = rs < rt; return;                                        /* sltu */
    default: unsupported(pc, w);
    }
}

static void exec_fp_s(cpu_t *c, uint32_t pc, uint32_t w)
{
    unsigned ft = RT(w), fs = RD(w), fd = SA(w);
    float a = rc_fs(c, fs), b = rc_fs(c, ft);
    switch (FUNCT(w)) {
    case 0x00: rc_set_fs(c, fd, a + b); return;                        /* add.s */
    case 0x01: rc_set_fs(c, fd, a - b); return;                        /* sub.s */
    case 0x02: rc_set_fs(c, fd, a * b); return;                        /* mul.s */
    case 0x03: rc_set_fs(c, fd, a / b); return;                        /* div.s */
    default: break;
    }
    if (FUNCT(w) >= 0x30) {
        if (fd & 3u)
            unsupported(pc, w);
        switch (FUNCT(w)) {
        case 0x32: rc_c_eq_s(c, fd >> 2, a, b); return;                /* c.eq.s */
        case 0x3C: rc_c_lt_s(c, fd >> 2, a, b); return;                /* c.lt.s */
        case 0x3E: rc_c_le_s(c, fd >> 2, a, b); return;                /* c.le.s */
        default: unsupported(pc, w);
        }
    }
    if (ft)
        unsupported(pc, w);
    switch (FUNCT(w)) {
    case 0x04: rc_set_fs(c, fd, rc_sqrt_s(a)); return;                 /* sqrt.s */
    case 0x06: c->fpr[fd] = c->fpr[fs]; return;                        /* mov.s */
    case 0x07: rc_set_fs(c, fd, -a); return;                           /* neg.s */
    case 0x0D: c->fpr[fd] = rc_trunc_w_s(a); return;                   /* trunc.w.s */
    case 0x15: rc_set_fs(c, fd, 1.0f / a); return;                     /* recip.s */
    case 0x21: rc_set_fd(c, fd, (double)a); return;                    /* cvt.d.s */
    default: unsupported(pc, w);
    }
}

static void exec_fp_d(cpu_t *c, uint32_t pc, uint32_t w)
{
    unsigned ft = RT(w), fs = RD(w), fd = SA(w);
    double a = rc_fd(c, fs), b = rc_fd(c, ft);
    switch (FUNCT(w)) {
    case 0x00: rc_set_fd(c, fd, a + b); return;                        /* add.d */
    case 0x01: rc_set_fd(c, fd, a - b); return;                        /* sub.d */
    case 0x02: rc_set_fd(c, fd, a * b); return;                        /* mul.d */
    case 0x03: rc_set_fd(c, fd, a / b); return;                        /* div.d */
    default: break;
    }
    if (FUNCT(w) >= 0x30) {
        if (fd & 3u)
            unsupported(pc, w);
        switch (FUNCT(w)) {
        case 0x32: rc_c_eq_d(c, fd >> 2, a, b); return;                /* c.eq.d */
        case 0x3C: rc_c_lt_d(c, fd >> 2, a, b); return;                /* c.lt.d */
        case 0x3E: rc_c_le_d(c, fd >> 2, a, b); return;                /* c.le.d */
        default: unsupported(pc, w);
        }
    }
    if (ft)
        unsupported(pc, w);
    switch (FUNCT(w)) {
    case 0x06:                                                         /* mov.d */
        c->fpr[fd & 30u] = c->fpr[fs & 30u];
        c->fpr[(fd & 30u) + 1] = c->fpr[(fs & 30u) + 1];
        return;
    case 0x07: rc_set_fd(c, fd, -a); return;                           /* neg.d */
    case 0x0D: c->fpr[fd] = rc_trunc_w_d(a); return;                   /* trunc.w.d */
    case 0x20: rc_set_fs(c, fd, (float)a); return;                     /* cvt.s.d */
    default: unsupported(pc, w);
    }
}

static void exec_cop1(cpu_t *c, uint32_t pc, uint32_t w)
{
    unsigned fs = RD(w);
    switch (RS(w)) {
    case 0x00: case 0x02: case 0x04: case 0x06:
        /* the moves; cfc1 and ctc1 only of FCSR ($31) */
        if (w & 0x7FFu || ((RS(w) & 2u) && fs != 31))
            break;
        switch (RS(w)) {
        case 0x00: c->r[RT(w)] = c->fpr[fs]; return;                   /* mfc1 */
        case 0x02: c->r[RT(w)] = rt_get_fcsr(c); return;               /* cfc1 */
        case 0x04: c->fpr[fs] = c->r[RT(w)]; return;                   /* mtc1 */
        default: rt_set_fcsr(c, c->r[RT(w)]); return;                  /* ctc1 */
        }
    case 0x10: exec_fp_s(c, pc, w); return;
    case 0x11: exec_fp_d(c, pc, w); return;
    case 0x14:
        if (w & M_RT)
            break;
        switch (FUNCT(w)) {
        case 0x20: rc_set_fs(c, SA(w), (float)(int32_t)c->fpr[fs]); return;  /* cvt.s.w */
        case 0x21: rc_set_fd(c, SA(w), (double)(int32_t)c->fpr[fs]); return; /* cvt.d.w */
        default: break;
        }
        break;
    default: break;
    }
    unsupported(pc, w);
}

static void exec_cop1x(cpu_t *c, uint32_t pc, uint32_t w)
{
    unsigned fr = RS(w), ft = RT(w), fs = RD(w), fd = SA(w);
    switch (FUNCT(w)) {
    case 0x20: rc_set_fs(c, fd, rc_madd_s(rc_fs(c, fr), rc_fs(c, fs), rc_fs(c, ft))); return;
    case 0x21: rc_set_fd(c, fd, rc_madd_d(rc_fd(c, fr), rc_fd(c, fs), rc_fd(c, ft))); return;
    case 0x28: rc_set_fs(c, fd, rc_msub_s(rc_fs(c, fr), rc_fs(c, fs), rc_fs(c, ft))); return;
    case 0x30: rc_set_fs(c, fd, rc_nmadd_s(rc_fs(c, fr), rc_fs(c, fs), rc_fs(c, ft))); return;
    case 0x38: rc_set_fs(c, fd, rc_nmsub_s(rc_fs(c, fr), rc_fs(c, fs), rc_fs(c, ft))); return;
    default: unsupported(pc, w);
    }
}

/* Runs a straight-line instruction and returns 1, or returns 0 for a control transfer
 * (left to the caller). Stops on anything else. */
static int exec(uint8_t *ram, cpu_t *c, uint32_t pc, uint32_t w)
{
    uint32_t *r = c->r;
    uint32_t rs = r[RS(w)];
    uint32_t ea = rs + SIMM(w);
    uint32_t *rt = &r[RT(w)];
    unsigned ft = RT(w);
    switch (w >> 26) {
    case 0x00:
        if (FUNCT(w) == 0x08 || FUNCT(w) == 0x09)
            return 0;
        exec_special(ram, c, pc, w);
        break;
    case 0x01: case 0x02: case 0x03: case 0x04: case 0x05: case 0x06: case 0x07:
    case 0x14: case 0x15: case 0x16: case 0x17:
        return 0;
    case 0x08: *rt = rs + SIMM(w); break;  /* addi: MAME takes no overflow trap */
    case 0x09: *rt = rs + SIMM(w); break;                               /* addiu */
    case 0x0A: *rt = (int32_t)rs < (int32_t)SIMM(w); break;             /* slti */
    case 0x0B: *rt = rs < SIMM(w); break;                               /* sltiu */
    case 0x0C: *rt = rs & UIMM(w); break;                               /* andi */
    case 0x0D: *rt = rs | UIMM(w); break;                               /* ori */
    case 0x0E: *rt = rs ^ UIMM(w); break;                               /* xori */
    case 0x0F: *rt = UIMM(w) << 16; break;                              /* lui */
    case 0x11:
        if (RS(w) == 8)
            return 0;                                                   /* bc1* */
        exec_cop1(c, pc, w);
        break;
    case 0x13: exec_cop1x(c, pc, w); break;
    case 0x20: *rt = (uint32_t)(int32_t)(int8_t)mem_r8(ram, c, ea); break;    /* lb */
    case 0x21: *rt = (uint32_t)(int32_t)(int16_t)mem_r16(ram, c, ea); break;  /* lh */
    case 0x23: *rt = mem_r32(ram, c, ea); break;                        /* lw */
    case 0x24: *rt = mem_r8(ram, c, ea); break;                         /* lbu */
    case 0x25: *rt = mem_r16(ram, c, ea); break;                        /* lhu */
    case 0x28: mem_w8(ram, c, ea, *rt); break;                          /* sb */
    case 0x29: mem_w16(ram, c, ea, *rt); break;                         /* sh */
    case 0x2B: mem_w32(ram, c, ea, *rt); break;                         /* sw */
    case 0x2F: break;                                   /* cache: no caches are modelled */
    case 0x31: c->fpr[ft] = mem_r32(ram, c, ea); break;                 /* lwc1 */
    case 0x35:                                                          /* ldc1 */
        c->fpr[ft & 30u] = mem_r32(ram, c, ea);
        c->fpr[(ft & 30u) + 1] = mem_r32(ram, c, ea + 4);
        break;
    case 0x39: mem_w32(ram, c, ea, c->fpr[ft]); break;                  /* swc1 */
    case 0x3D:                                                          /* sdc1 */
        mem_w32(ram, c, ea, c->fpr[ft & 30u]);
        mem_w32(ram, c, ea + 4, c->fpr[(ft & 30u) + 1]);
        break;
    default: unsupported(pc, w);
    }
    r[0] = 0;
    return 1;
}

/* --- control transfers ------------------------------------------------------------------ */

enum xfer { X_BRANCH, X_CALL, X_RETURN, X_JUMP };

/* Decodes the control transfer at pc, before its delay slot: decides a branch, reads a jump
 * target, writes a link. Sets *taken and *likely for a branch. */
static enum xfer control(cpu_t *c, uint32_t pc, uint32_t w, uint32_t *target, int *taken,
                         int *likely)
{
    uint32_t rs = c->r[RS(w)], rt = c->r[RT(w)];
    int32_t s = (int32_t)rs;
    uint32_t branch = pc + 4 + (SIMM(w) << 2);
    *taken = 0;
    *likely = 0;
    *target = branch;
    switch (w >> 26) {
    case 0x00:
        if (FUNCT(w) == 0x08) {                                          /* jr */
            if (w & (M_RT | M_RD | M_SA))
                break;
            *target = rs;
            return RS(w) == 31 ? X_RETURN : X_JUMP;
        }
        if (w & (M_RT | M_SA))                                           /* jalr */
            break;
        *target = rs;
        c->r[RD(w)] = pc + 8;
        c->r[0] = 0;
        return X_CALL;
    case 0x01:
        switch (RT(w)) {
        case 0x00: *taken = s < 0; return X_BRANCH;                      /* bltz */
        case 0x01: *taken = s >= 0; return X_BRANCH;                     /* bgez */
        case 0x02: *likely = 1; *taken = s < 0; return X_BRANCH;         /* bltzl */
        case 0x03: *likely = 1; *taken = s >= 0; return X_BRANCH;        /* bgezl */
        default: break;
        }
        break;
    case 0x02:                                                           /* j */
        *target = ((pc + 4) & 0xF0000000u) | (w & 0x03FFFFFFu) << 2;
        return X_JUMP;
    case 0x03:                                                           /* jal */
        *target = ((pc + 4) & 0xF0000000u) | (w & 0x03FFFFFFu) << 2;
        c->r[31] = pc + 8;
        return X_CALL;
    case 0x14: *likely = 1; /* fall through */
    case 0x04: *taken = rs == rt; return X_BRANCH;                       /* beq, beql */
    case 0x15: *likely = 1; /* fall through */
    case 0x05: *taken = rs != rt; return X_BRANCH;                       /* bne, bnel */
    case 0x16: *likely = 1; /* fall through */
    case 0x06: *taken = s <= 0; return X_BRANCH;                         /* blez, blezl */
    case 0x17: *likely = 1; /* fall through */
    case 0x07: *taken = s > 0; return X_BRANCH;                          /* bgtz, bgtzl */
    case 0x11:                                       /* bc1f, bc1t, bc1fl, bc1tl */
        *likely = (RT(w) & 2u) != 0;
        *taken = rc_cc(c, RT(w) >> 2) == (int)(RT(w) & 1u);
        return X_BRANCH;
    default: break;
    }
    unsupported(pc, w);
}

static void delay_slot(uint8_t *ram, cpu_t *c, uint32_t pc)
{
    uint32_t w = fetch(ram, pc);
    c->pc = pc;
    if (!exec(ram, c, pc, w))
        stop("control transfer %08X in the delay slot at %08X", w, pc);
}

void interp_run(uint8_t *ram, cpu_t *c, uint32_t pc)
{
    note_entry(pc);
    RT_CHECK(ram, c);
    for (;;) {
        uint32_t w = fetch(ram, pc), target;
        int taken, likely;
        c->pc = pc;
        if (exec(ram, c, pc, w)) {
            pc += 4;
            continue;
        }
        enum xfer x = control(c, pc, w, &target, &taken, &likely);
        if (x == X_BRANCH && likely && !taken) {
            pc += 8;
            continue;
        }
        delay_slot(ram, c, pc + 4);
        switch (x) {
        case X_BRANCH:
            if (taken && target <= pc)
                RT_CHECK(ram, c);
            pc = taken ? target : pc + 8;
            break;
        case X_CALL:
            rt_call(ram, c, target);
            pc += 8;
            break;
        case X_RETURN:
            return;
        case X_JUMP:
            if (recomp_lookup(target)) {
                rt_call(ram, c, target);
                return;
            }
            if (target <= pc)
                RT_CHECK(ram, c);
            pc = target;
            break;
        }
    }
}
