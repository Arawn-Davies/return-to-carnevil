/* irq.c: Status and Cause, the handler tables, the exception dispatcher with its frame, the
 * interrupt loop and the loader's default interrupt work, and VBLANK.
 *
 * docs/analysis/loader-services.md section 6 is the reference for every step: the frame
 * ("The frame"), the order of the dispatch and what is restored ("Dispatch"), the loop over
 * the IP bits ("The interrupt loop") and what the loader does after a line's handler ("The
 * loader's default interrupt work"). Section 1 gives the slots that fill the tables (0x1E0,
 * 0x1E8, 0x1F0/0x1F8, 0x200).
 */
#include <stdio.h>

#include "machine.h"

machine_t machine;

void machine_reset(void)
{
    machine = (machine_t){.status = ST_POWER_ON, .intctl = INTCTL_AT_ENTRY};
}

/* The loader's line map 0x800146C0: the line of each IP bit, -1 for none (IP0, IP1, IP7). */
static const int IP_LINE[8] = {-1, -1, 1, 3, 2, 4, 0, -1};

#define IP_BIT(n) (0x100u << (n))

/* --- Status and Cause ------------------------------------------------------------------- */

uint32_t m_status(void)
{
    return machine.status;
}

void m_status_set(uint32_t set, uint32_t clear)
{
    machine.status = (machine.status & ~clear) | set;
}

static void check_ip(const char *who, int ip)
{
    if (ip < 2 || ip > 6)
        rt_stop(RT_STOP_STATUS, "%s: IP%d is not a line a device drives (IP2 to IP6)", who, ip);
}

void irq_raise(int ip)
{
    check_ip("irq_raise", ip);
    machine.cause_ip |= IP_BIT(ip);
}

void irq_clear(int ip)
{
    check_ip("irq_clear", ip);
    machine.cause_ip &= ~IP_BIT(ip);
}

int irq_deliverable(void)
{
    return (machine.status & ST_IE) && !(machine.status & ST_EXL) &&
           (machine.cause_ip & machine.status & 0xFF00u);
}

/* IP2 is the GT64010's line: pending while a cause bit is set and enabled in CPU_MASK. */
static void gt_update(void)
{
    if (machine.gt_cause & machine.gt_mask)
        machine.cause_ip |= IP_BIT(2);
    else
        machine.cause_ip &= ~IP_BIT(2);
}

/* --- the handler tables ----------------------------------------------------------------- */

void irq_set_line(int line, uint32_t fn)
{
    int n = 0;
    if (line < 0 || line > 7)
        return;
    while (n < 8 && IP_LINE[n] != line)
        n++;
    if (n == 8)
        return; /* lines 5 to 7: nothing happens, not even the store */
    /* for line 0 the slot writes VBLANK clear either way (section 1, slot 0x200), which
     * acknowledges a pending VBLANK (inferred, as for the default IP6 work), and sets bit 7
     * of the interrupt enable register 0x17300000 for a handler, clears it for none (read):
     * the board's VBLANK enable */
    if (line == 0) {
        irq_clear(6);
        machine.vblank_enable = fn != 0;
    }
    if (fn)
        machine.status |= IP_BIT(n) | ST_IE;
    else
        machine.status &= ~IP_BIT(n);
    machine.line_fn[line] = fn;
}

void irq_set_gt(int bit, uint32_t fn)
{
    uint32_t b;
    if (bit < 0 || bit > 31)
        return;
    b = 1u << bit;
    if (fn) {
        machine.gt_cause &= ~b;
        machine.gt_mask |= b;
    } else if (bit != 8) { /* bit 8 is the loader's own timer-0 tick: it stays enabled */
        machine.gt_mask &= ~b;
        machine.gt_cause &= ~b;
    }
    machine.gt_fn[bit] = fn;
    gt_update();
}

void gt_cause_raise(int bit)
{
    if (bit < 0 || bit > 31)
        rt_stop(RT_STOP_STATUS, "gt_cause_raise: no GT64010 cause bit %d", bit);
    machine.gt_cause |= 1u << bit;
    gt_update();
}

void exc_set_handler(int code, uint32_t fn)
{
    if (code >= 0 && code <= 15)
        machine.exc_fn[code] = fn;
}

void exc_set_fpe(int cause, uint32_t fn)
{
    if (cause >= 0 && cause <= 5)
        machine.fpe_fn[cause] = fn;
}

void exc_set_break_hook(int which, uint32_t fn)
{
    if (which == 7)
        machine.break7_hook = fn;
    else if (which == 6)
        machine.break6_hook = fn;
    else
        rt_stop(RT_STOP_STATUS, "exc_set_break_hook: no break hook %d (7 or 6)", which);
}

/* --- the frame -------------------------------------------------------------------------- */

/* An 8-byte slot written with sd (or sdc1 of a sign-extended mfc0 value): a 32-bit register
 * appears sign-extended. */
static void frame_sd(uint8_t *ram, uint32_t off, uint32_t v)
{
    frame_w32(ram, off, v);
    frame_w32(ram, off + 4, v & 0x80000000u ? 0xFFFFFFFFu : 0);
}

/* The registers the dispatcher saves and restores, with Status as the save routine sees it
 * (EXL already set). The frame holds them in guest RAM (frame_save, frame_restore); a disk
 * hook's call keeps them in host memory (exc_hook_call), through the same two functions. */
struct exc_regs {
    uint32_t r[32], hi, lo, status, fpr[32], fcsr;
};

/* What the save routine 0x80004808 reads: every GPR (k0's value is not used), HI, LO,
 * Status, the FPRs when Status has CU1, FCSR. */
static void regs_take(const cpu_t *c, struct exc_regs *s)
{
    memcpy(s->r, c->r, sizeof s->r);
    s->hi = c->hi;
    s->lo = c->lo;
    s->status = machine.status;
    memcpy(s->fpr, c->fpr, sizeof s->fpr);
    s->fcsr = rt_get_fcsr(c);
}

/* What the resume routine 0x80004BE0 and its eret put back: HI, LO, Status (eret then clears
 * EXL), the FPRs if the restored Status has CU1, FCSR (which re-applies the host FP
 * environment), every GPR but k0. ERL is not modelled: eret here always clears EXL. */
static void regs_give(cpu_t *c, const struct exc_regs *s)
{
    c->hi = s->hi;
    c->lo = s->lo;
    machine.status = s->status & ~ST_EXL;
    if (s->status & ST_CU1)
        memcpy(c->fpr, s->fpr, sizeof c->fpr);
    rt_set_fcsr(c, s->fcsr);
    for (uint32_t n = 1; n < 32; n++)
        if (n != 26)
            c->r[n] = s->r[n];
    c->r[0] = 0;
}

/* The save routine 0x80004808, with Status already carrying EXL. CP0 registers the runtime
 * does not model (all but Status, Cause and EPC) and FIR are written as 0. */
static void frame_save(uint8_t *ram, const cpu_t *c, uint32_t cause, uint32_t epc)
{
    struct exc_regs s;
    regs_take(c, &s);
    for (uint32_t n = 1; n < 32; n++)
        if (n != 26) /* k0 is not saved: its slot keeps stale data */
            frame_sd(ram, 8 * n, s.r[n]);
    frame_sd(ram, 0x100, s.lo);
    frame_sd(ram, 0x108, s.hi);
    frame_w32(ram, 0x110, epc); /* the resume pc, a 32-bit sw */
    for (uint32_t r = 0; r < 32; r++)
        frame_sd(ram, 0x118 + 8 * r, r == 12 ? s.status : r == 13 ? cause : r == 14 ? epc : 0);
    if (s.status & ST_CU1) {
        /* sdc1 f(2k) with FR clear writes the pair f(2k), f(2k+1); +0x220 + 16k is not
         * written */
        for (uint32_t k = 0; k < 16; k++) {
            frame_w32(ram, 0x218 + 16 * k, s.fpr[2 * k]);
            frame_w32(ram, 0x21C + 16 * k, s.fpr[2 * k + 1]);
        }
    }
    frame_w32(ram, 0x318, 0); /* FIR (cfc1 $0): not modelled */
    frame_w32(ram, 0x31C, s.fcsr);
}

/* The registers from the frame, put back as the resume routine does; returns the resume pc. */
static uint32_t frame_restore(uint8_t *ram, cpu_t *c)
{
    struct exc_regs s;
    for (uint32_t n = 0; n < 32; n++)
        s.r[n] = frame_r32(ram, 8 * n);
    s.hi = frame_r32(ram, 0x108);
    s.lo = frame_r32(ram, 0x100);
    s.status = frame_r32(ram, 0x178);
    for (uint32_t k = 0; k < 16; k++) {
        s.fpr[2 * k] = frame_r32(ram, 0x218 + 16 * k);
        s.fpr[2 * k + 1] = frame_r32(ram, 0x21C + 16 * k);
    }
    s.fcsr = frame_r32(ram, 0x31C);
    regs_give(c, &s);
    return frame_r32(ram, 0x110);
}

/* --- the dispatcher --------------------------------------------------------------------- */

uint32_t exc_call(uint8_t *ram, cpu_t *c, uint32_t fn)
{
    c->r[29] = EXC_STACK;
    c->r[31] = EXC_RETURN_RA;
    rt_call(ram, c, fn);
    return c->r[2];
}

void exc_hook_call(uint8_t *ram, cpu_t *c, uint32_t fn, uint32_t arg, uint32_t ra)
{
    struct exc_regs s;
    if (machine.status & ST_EXL)
        rt_stop(RT_STOP_STATUS, "disk hook 0x%08X called with Status EXL already set: on the "
                "board it runs from the IDE completion interrupt, which is not taken inside "
                "a handler, and what it does then is not modelled", (unsigned)fn);
    machine.ram = ram;
    regs_take(c, &s);
    machine.status |= ST_EXL;
    machine.intctl &= ~INTCTL_AFTER_EXCEPTION;
    vclock_reload(c); /* a fresh slice, as for a handler (exc_raise) */
    c->r[4] = arg;
    c->r[31] = ra;
    rt_call(ram, c, fn);
    regs_give(c, &s);
    machine.intctl |= INTCTL_AFTER_EXCEPTION;
    /* EXL is clear again: a line that became pending in the hook (a VBLANK that came due) is
     * delivered at the next check point, after the slot has returned */
    vclock_reload(c);
}

/* The loader's default work 0x80005368 for IP bit n, after its line handler returned
 * non-zero or when there is none. */
static void default_work(uint8_t *ram, cpu_t *c, int n)
{
    switch (n) {
    case 6:
        /* VBLANK: writes VBLANK clear, acknowledging it. The loader's VBLANK count, LED and
         * watchdog writes are not modelled: the game never reads them. */
        irq_clear(6);
        return;
    case 3:
        /* IOASIC: clears INTCTL bit 0. The IOASIC model's IP3 level (runtime/hw/dcs_stub.c,
         * dcs_irq_update) needs INTCTL bit 0, so with it clear the line follows the model
         * low. It rises again when the level becomes true: the game setting bit 0 with an
         * enabled source pending, or an enabled source becoming pending while bit 0 is set. */
        machine.intctl &= ~1u;
        irq_clear(3);
        return;
    case 5:
        fputs("carnevil: Hilink/NSS/Widget Interrupt - must be handled\n", stderr);
        return;
    case 2: {
        /* GT64010: every pending and enabled cause bit, ascending, cleared first; then its
         * handler with a0 = 1, return ignored. Bit 8 is also the loader's 100 ms tick and
         * disk-timeout check, which are not modelled yet. */
        uint32_t pending = machine.gt_cause & machine.gt_mask;
        for (int b = 0; b < 32; b++) {
            if (!(pending & 1u << b))
                continue;
            machine.gt_cause &= ~(1u << b);
            if (machine.gt_fn[b]) {
                c->r[4] = 1;
                exc_call(ram, c, machine.gt_fn[b]);
            } else if (b < 8 || b > 11) {
                fprintf(stderr, "carnevil: GT INT: cause bit %d has no handler\n", b);
            }
        }
        gt_update();
        return;
    }
    default: /* 4 */
        rt_stop(RT_STOP_STATUS, "IP4 (IDE) default interrupt work, the loader's disk "
                "completion service 0x80012158, is not modelled yet (interrupted pc 0x%08X)",
                (unsigned)frame_r32(ram, 0x110));
    }
}

/* The interrupt loop 0x80005100: every IP bit of the Cause read at entry, ascending, masked
 * or not; the frame's Cause loses each bit once it is served. */
static void irq_loop(uint8_t *ram, cpu_t *c, uint32_t cause)
{
    for (int n = 0; n < 8; n++) {
        uint32_t bit = IP_BIT(n), fn, ret = 1;
        if (!(cause & bit))
            continue;
        if (IP_LINE[n] < 0) /* no source drives IP0, IP1 or IP7 here */
            rt_stop(RT_STOP_STATUS, "interrupt on IP%d, which has no line: the board enters "
                    "its debug entry (interrupted pc 0x%08X)", n,
                    (unsigned)frame_r32(ram, 0x110));
        fn = machine.line_fn[IP_LINE[n]];
        if (fn) {
            c->r[4] = bit; /* a1 is not set: the frame on the first call, then left over */
            ret = exc_call(ram, c, fn);
        }
        if (ret)
            default_work(ram, c, n);
        frame_w32(ram, 0x180, frame_r32(ram, 0x180) & ~bit);
    }
}

uint32_t exc_raise(uint8_t *ram, cpu_t *c, int code, uint32_t epc)
{
    uint32_t cause, resume;
    if (code < 0 || code > 31)
        rt_stop(RT_STOP_STATUS, "exc_raise: no exception code %d (pc 0x%08X)", code,
                (unsigned)epc);
    if (machine.status & ST_EXL)
        rt_stop(RT_STOP_STATUS, "exception inside a handler: code %d at pc 0x%08X (the board "
                "prints \"Exception while in exeption handler\" and reboots)", code,
                (unsigned)epc);
    machine.ram = ram;
    cause = machine.cause_ip | (uint32_t)code << 2;
    machine.status |= ST_EXL;
    frame_save(ram, c, cause, epc);
    machine.intctl &= ~INTCTL_AFTER_EXCEPTION;
    /* The handlers start with a fresh slice. A handler that runs longer than a slice
     * re-enters rt_tick; EXL is what makes that safe: it advances time and runs events but
     * delivers nothing. */
    vclock_reload(c);
    c->r[4] = cause;
    c->r[5] = EXC_FRAME;
    if (code == 0)
        irq_loop(ram, c, cause);
    else
        exc_other(ram, c, code, cause, epc);
    resume = frame_restore(ram, c);
    c->pc = resume; /* so a later trap or interrupt does not record a handler's pc */
    machine.intctl |= INTCTL_AFTER_EXCEPTION;
    /* EXL is clear again: a line that became pending while the handlers ran (a VBLANK that
     * came due, a line a slot raised) is delivered at the next check point */
    vclock_reload(c);
    return resume;
}

/* --- VBLANK ----------------------------------------------------------------------------- */

/* The board's VBLANK is the Voodoo's vertical retrace (MAME's seattle.cpp: the card's vblank
 * callback is the board's VBLANK interrupt); its times are the card's (runtime/hw). */
void vblank_raise(void)
{
    if (!machine.vblank_enable)
        return;
    irq_raise(6);
    if (machine.vblank_hook)
        machine.vblank_hook();
}

void irq_set_vblank_hook(void (*fn)(void))
{
    machine.vblank_hook = fn;
}
