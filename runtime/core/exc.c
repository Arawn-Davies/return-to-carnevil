/* exc.c: break, FP traps, the exceptions other than interrupts, transfers outside RAM, and
 * stopping.
 *
 * docs/analysis/loader-services.md section 6 ("Exceptions other than interrupts", "`break`
 * codes") is the reference. Generated code can only go on after the instruction that raised
 * an exception, so a handler must leave the resume pc (frame + 0x110) at that instruction's
 * pc + 4; any other resume pc stops, naming it, rather than being guessed at.
 */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

#include "machine.h"

/* --- stopping --------------------------------------------------------------------------- */

static void run_at_stop(void)
{
    if (!machine.stopping) { /* a stop from a stop function does not run them again */
        machine.stopping = 1;
        for (int i = 0; i < machine.n_at_stop; i++)
            machine.at_stop[i]();
    }
}

_Noreturn void rt_stop(int status, const char *fmt, ...)
{
    char message[RT_STOP_MESSAGE_MAX];
    va_list ap;
    fflush(stdout);
    va_start(ap, fmt);
    vsnprintf(message, sizeof message, fmt, ap);
    va_end(ap);
    fprintf(stderr, "carnevil: stop: %s\n", message);
    fflush(stderr);
    if (machine.stop_hook && !machine.stopping) {
        void (*hook)(int, const char *) = machine.stop_hook;
        machine.stop_hook = NULL; /* a stop inside the hook does not call it again */
        hook(status, message);
    }
    run_at_stop();
    exit(status);
}

_Noreturn void rt_exit(int status)
{
    fflush(stdout);
    run_at_stop();
    exit(status);
}

void rt_set_stop_hook(void (*fn)(int status, const char *message))
{
    machine.stop_hook = fn;
}

void rt_at_stop(void (*fn)(void))
{
    if (machine.n_at_stop == RT_AT_STOP_MAX)
        rt_stop(RT_STOP_STATUS, "rt_at_stop: more than %d functions", RT_AT_STOP_MAX);
    machine.at_stop[machine.n_at_stop++] = fn;
}

/* --- exceptions other than interrupts --------------------------------------------------- */

/* What an FP trap's stop messages say about the pc they name (see rt_fp_trap). */
#define FP_PC_NOTE " (pc as last stored, exact only in a --trace-pc build)"

/* After exc_raise for what (a description) at epc: returns when the resume pc is epc + 4. */
static void resume_next(const char *what, uint32_t epc, const char *pc_note, uint32_t resume)
{
    if (resume == epc + 4)
        return;
    if (resume == epc)
        rt_stop(RT_STOP_STATUS, "%s at pc 0x%08X%s: the handler left the resume pc there, so "
                "it would run again for ever", what, (unsigned)epc, pc_note);
    rt_stop(RT_STOP_STATUS, "%s at pc 0x%08X%s: the handler moved the resume pc to 0x%08X; "
            "the runtime resumes only at the next instruction", what, (unsigned)epc, pc_note,
            (unsigned)resume);
}

void exc_other(uint8_t *ram, cpu_t *c, int code, uint32_t cause, uint32_t epc)
{
    uint32_t fn = code <= 15 ? machine.exc_fn[code] : 0;
    if (fn && exc_call(ram, c, fn) == 0) /* (Cause, frame), already in a0 and a1 */
        return;
    /* no handler, or it returned non-zero: the loader's default for the code (0x80014760) */
    if (code == 9) {
        uint32_t hook = machine.break_code == 0x1C00u   ? machine.break7_hook
                        : machine.break_code == 0x1800u ? machine.break6_hook
                                                        : 0;
        if (!hook)
            rt_stop(RT_STOP_STATUS, "exception code 9 (break 0x%X) at pc 0x%08X: no handler "
                    "and no break hook", (unsigned)machine.break_code, (unsigned)epc);
        c->r[4] = cause;
        c->r[5] = EXC_FRAME;
        exc_call(ram, c, hook); /* hook(Cause, frame), return ignored */
        return;
    }
    if (code == 15) {
        /* 0x80005CD8: FCSR cause bits 12 (inexact) to 17 (unimplemented), ascending, each
         * to its FPU handler with a0 = frame (return ignored), then cleared in the saved
         * FCSR. Without a handler the board prints FPCSR and goes to its debug entry. */
        uint32_t pending = frame_r32(ram, 0x31C);
        for (int b = 12; b <= 17; b++) {
            if (!(pending & 1u << b))
                continue;
            if (!machine.fpe_fn[b - 12])
                rt_stop(RT_STOP_STATUS, "floating-point exception: FCSR cause bit 0x%X at pc "
                        "0x%08X" FP_PC_NOTE " has no FPU handler", 1u << b, (unsigned)epc);
            c->r[4] = EXC_FRAME;
            exc_call(ram, c, machine.fpe_fn[b - 12]);
            frame_w32(ram, 0x31C, frame_r32(ram, 0x31C) & ~(1u << b));
        }
        return;
    }
    rt_stop(RT_STOP_STATUS, "exception code %d at pc 0x%08X: %s", code, (unsigned)epc,
            fn ? "its handler returned non-zero and the loader has no default for it"
               : "no handler");
}

/* rt_break (recomp.h): the codes of the loader's break table 0x80004684 that the game uses.
 * A break is an exception, so one inside a handler stops as any other does. */
void rt_break(uint8_t *ram, cpu_t *c, uint32_t pc, uint32_t code)
{
    machine.ram = ram;
    if (machine.status & ST_EXL)
        rt_stop(RT_STOP_STATUS, "exception inside a handler: code 9 (break 0x%X) at pc 0x%08X "
                "(the board prints \"Exception while in exeption handler\" and reboots)",
                (unsigned)code, (unsigned)pc);
    switch (code) {
    case 0x409:
        /* install a0's handler a1 for codes 0 to 15; registers are left as they were */
        if (c->r[4] <= 15)
            exc_set_handler((int)c->r[4], c->r[5]);
        return;
    case 0x1C00:
    case 0x1800: {
        char what[32];
        uint32_t resume;
        if (!(code == 0x1C00u ? machine.break7_hook : machine.break6_hook))
            rt_stop(RT_STOP_STATUS, "break 0x%X at pc 0x%08X with no break hook: the board "
                    "enters its debug monitor", (unsigned)code, (unsigned)pc);
        machine.break_code = code;
        resume = exc_raise(ram, c, 9, pc);
        machine.break_code = 0;
        snprintf(what, sizeof what, "break 0x%X", (unsigned)code);
        resume_next(what, pc, "", resume);
        return;
    }
    case 0x407:
        rt_stop(RT_STOP_STATUS, "break 0x407 (assert) at pc 0x%08X: the board enters its debug "
                "monitor and does not come back", (unsigned)pc);
    case 0x402:
        rt_stop(RT_STOP_STATUS, "break 0x402 (exit) at pc 0x%08X: the board enters its debug "
                "monitor", (unsigned)pc);
    default:
        rt_stop(RT_STOP_STATUS, "break 0x%X at pc 0x%08X: not a break code the runtime models",
                (unsigned)code, (unsigned)pc);
    }
}

/* rt_fp_trap (recomp.h): FP exception, code 15, at c->pc. c->pc is exact only in the
 * interpreter and in generated code built with --trace-pc; elsewhere it is the pc last
 * stored, which the messages say. A handler that skips the instruction lets the division go
 * on with its IEEE result, where the board would leave the destination unwritten; the game's
 * catch-all never returns, so that difference is not reached. */
void rt_fp_trap(cpu_t *c, uint32_t cause_bit)
{
    char what[64];
    uint32_t epc = c->pc, resume;
    if (!machine.ram)
        rt_stop(RT_STOP_STATUS, "floating-point exception (FCSR cause 0x%X) at pc 0x%08X"
                FP_PC_NOTE " before the runtime was given RAM", (unsigned)cause_bit,
                (unsigned)epc);
    c->fcsr |= cause_bit;
    resume = exc_raise(machine.ram, c, 15, epc);
    snprintf(what, sizeof what, "floating-point exception (FCSR cause 0x%X)", (unsigned)cause_bit);
    resume_next(what, epc, FP_PC_NOTE, resume);
}

/* rt_bad_target (recomp.h): the game's crash handler ends at the reset vector, which on the
 * board reboots. */
void rt_bad_target(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    (void)ram;
    if (addr >= 0x80000000u && addr < 0xC0000000u && (addr & 0x1FFFFFFFu) == 0x1FC00000u)
        rt_stop(RT_STOP_STATUS, "game jumped to the reset vector 0x%08X (pc as last stored "
                "0x%08X)", (unsigned)addr, (unsigned)c->pc);
    rt_stop(RT_STOP_STATUS, "transfer to 0x%08X, which is not RAM (pc as last stored 0x%08X)",
            (unsigned)addr, (unsigned)c->pc);
}
