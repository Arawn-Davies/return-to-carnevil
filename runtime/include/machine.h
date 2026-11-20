/* machine.h: the runtime's model of the board around the CPU, shared by runtime/core's
 * clock.c (the virtual clock and its events), irq.c (Status and Cause, the handler tables,
 * the exception dispatcher, the interrupt loop and VBLANK) and exc.c (break, FP traps,
 * exceptions other than interrupts, transfers outside RAM, stopping).
 *
 * Everything here follows docs/analysis/loader-services.md section 6, "Interrupts and
 * exceptions as the game sees them": the resident loader's dispatcher is replaced by C that
 * writes the same frame and calls the game's registered handlers the same way.
 *
 * All the state lives in the one machine_t below; machine_reset returns it to power-on
 * values. Time is virtual: nanoseconds as uint64_t, advanced only by check points
 * (recomp.h, RT_CHECK), never read from the host.
 */
#ifndef MACHINE_H
#define MACHINE_H

#include <stdint.h>

#include "recomp.h"

/* --- the clock (clock.c) ---------------------------------------------------------------- */

/* The virtual time one check point stands for. */
#define VCLOCK_NS_PER_CHECK 100u
/* rt_tick reloads slice with the check points until the next event, at least 1 and at most
 * this. */
#define VCLOCK_MAX_SLICE 1000000
#define VCLOCK_MAX_EVENTS 64

/* vclock_now: the virtual time, including the check points of the current slice already
 * used. vclock_at: run fn(arg) from rt_tick once the time reaches t_ns (at the next tick if
 * t_ns has passed); events run in time order, events of equal time in the order they were
 * scheduled, and an event may schedule more (at the current time too: it runs in the same
 * tick). vclock_cancel: forget every scheduled event of fn with arg. */
uint64_t vclock_now(cpu_t *c);
void vclock_at(uint64_t t_ns, void (*fn)(void *), void *arg);
void vclock_cancel(void (*fn)(void *), void *arg);
/* vclock_advance: time passing with no check point, for a device that holds the CPU (the
 * Voodoo holding a store at its FIFO's water marks): accounts the checks used so far, moves
 * the time forward by exactly ns, running every event that falls due on the way in time order
 * at its own time (events those schedule inside the interval too), then reloads the slice.
 * It delivers nothing: an interrupt raised on the way is pending, and the next check point
 * delivers it, as a CPU released from a hold takes it. Not from inside a clock event. */
void vclock_advance(cpu_t *c, uint64_t ns);

/* --- interrupts (irq.c) ----------------------------------------------------------------- */

#define ST_IE 0x00000001u
#define ST_EXL 0x00000002u
#define ST_CU1 0x20000000u
#define ST_POWER_ON 0x30010401u /* docs/analysis/boot-handoff.md: CU1, CU0, DE, IM2, IE */

/* The exception stack and frame of the loader's dispatcher (section 6, "The frame"). */
#define EXC_FRAME 0x80014328u
#define EXC_STACK 0x800C1400u
/* What ra holds when a handler is called: the loader's resume routine (0x80004BE0), where
 * the board's dispatcher goes after the handlers. rt_call returns at the handler's jr ra
 * whatever ra is; the value only marks the call as the runtime's. */
#define EXC_RETURN_RA 0x80004BE0u

/* IOASIC INTCTL (bus word 8): bit 14 is set after every exception, bit 0 is cleared by the
 * default IP3 work. */
#define INTCTL_AFTER_EXCEPTION 0x4000u
/* INTCTL at the game's entry, which machine_reset gives it (measured: loader-services.md,
 * open question 1). */
#define INTCTL_AT_ENTRY 0x4000u

/* irq_raise, irq_clear: the level of CPU interrupt line IP2 to IP6 (Cause bit 8 + ip). A
 * line raised twice before it is served is one pending bit. Delivery happens only in rt_tick,
 * when Status has IE set and EXL clear and a pending line is unmasked (Status IM).
 *
 * A line raised from a clock event is delivered at the next check point by itself. Code
 * that holds a cpu_t and changes what is deliverable outside an event (a loader slot, a bus
 * access: irq_raise, gt_cause_raise, irq_set_gt, a Status write) calls vclock_reload(c)
 * afterwards, so that the line is delivered at the next check point rather than when the
 * current slice (up to VCLOCK_MAX_SLICE checks) runs out; exc_raise does so itself once EXL
 * is clear. */
void irq_raise(int ip);
void irq_clear(int ip);
/* m_status: Status. m_status_set: Status = (Status & ~clear) | set. */
uint32_t m_status(void);
void m_status_set(uint32_t set, uint32_t clear);

/* The handler tables, filled as the loader's slots fill them (section 1):
 * - irq_set_line (slot 0x200): line 0 to 7 by the loader's line map, IP6 -> line 0, IP2 ->
 *   1, IP4 -> 2, IP3 -> 3, IP5 -> 4; a line no IP maps to does nothing. A non-zero fn unmasks
 *   the line and sets IE, zero masks it; for line 0 either acknowledges VBLANK, and fn sets
 *   or clears the board's VBLANK enable (interrupt enable 0x17300000 bit 7).
 * - irq_set_gt (slot 0x1E0): GT64010 cause bit 0 to 31. A non-zero fn clears the cause bit
 *   and enables it in CPU_MASK; zero disables and clears it (except bit 8, the loader's
 *   timer tick, which stays enabled). gt_cause_raise sets a cause bit; IP2 follows
 *   (cause & CPU_MASK) != 0.
 * - exc_set_handler (break 0x409): exception code 0 to 15; other codes are ignored.
 * - exc_set_fpe (slot 0x1E8): FCSR cause 0 to 5 (inexact .. unimplemented); others ignored.
 *   It does not touch FCSR: the slot's stand-in sets the enable bit itself.
 * - exc_set_break_hook (slots 0x1F0, 0x1F8): which = 7 (break 7, divide by zero) or 6
 *   (break 6, overflow).
 * fn is a guest address, 0 for none. */
void irq_set_line(int line, uint32_t fn);
void irq_set_gt(int bit, uint32_t fn);
void gt_cause_raise(int bit);
void exc_set_handler(int code, uint32_t fn);
void exc_set_fpe(int cause, uint32_t fn);
void exc_set_break_hook(int which, uint32_t fn);

/* exc_raise: the exception code (0 for an interrupt) at epc, dispatched as the loader does:
 * the frame at EXC_FRAME is written, EXL set, the handlers called through rt_call on
 * EXC_STACK, then every register but k0, Status and FCSR come back from the frame, c->pc
 * becomes the resume pc and the slice is reloaded (vclock_reload). Returns the resume pc
 * (frame + 0x110), which the caller must honour or refuse; the caller never resumes anywhere
 * else. An exception while EXL is set stops (the board reboots). */
uint32_t exc_raise(uint8_t *ram, cpu_t *c, int code, uint32_t epc);

/* exc_hook_call: fn(arg) with ra = ra, as the loader's IDE completion path calls the disk
 * hooks (the completion callback of slot 0x1D8 and the wake hook of slot 0x220) from its
 * interrupt (final review M9, ruling C1): at the exception level, so nothing is delivered
 * inside the hook (measured: disk-hooks.txt, Status 0x30015C03, EXL set, in all 3,186
 * callback and wake calls of facts-A2), and with the registers the dispatcher saves put back
 * afterwards by the dispatcher's own restore (every GPR but k0, HI, LO, Status, the FPRs when
 * CU1 is set, FCSR). Unlike an interrupt it writes no frame (the registers are kept in host
 * memory: the frame's contents on the board would be another code's, which is not modelled)
 * and the hook runs on the caller's stack (on the board, on the exception stack, sp
 * 0x800C1358 in every call measured). INTCTL bit 14 is clear while it runs, as during any
 * handler. A call with EXL already set stops. A line that becomes pending meanwhile is
 * delivered at the next check point after the call. */
void exc_hook_call(uint8_t *ram, cpu_t *c, uint32_t fn, uint32_t arg, uint32_t ra);

/* vblank_raise: the start of the Voodoo's vertical retrace, which is the board's VBLANK
 * source (runtime/hw/voodoo_null.c drives it from the card's video timing): raises IP6 if
 * the board's VBLANK enable is set (loader-services.md section 1, slot 0x200). */
void vblank_raise(void);
/* irq_set_vblank_hook: fn (or NULL) is called at every vblank_raise that raises IP6, after
 * raising it: the count of VBLANK interrupts, for the runtime's trace and its --frames limit.
 * It must not change guest state; machine_reset clears it. */
void irq_set_vblank_hook(void (*fn)(void));

/* --- stopping (exc.c) ------------------------------------------------------------------- */

/* rt_stop: prints "carnevil: stop: " and the message on stderr, runs the functions given to
 * rt_at_stop in the order they were given, and exits with status.
 *
 * The at-stop functions run once, whichever way the run ends (rt_stop, rt_exit). A stop
 * inside one of them (a save that fails) is printed and given to the stop hook like any
 * other, and does not skip the others: the remaining functions still run, and the program
 * then exits with RT_STOP_STATUS whatever status it was ending with. */
_Noreturn void rt_stop(int status, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
void rt_at_stop(void (*fn)(void));
/* rt_set_stop_hook: fn (or NULL) is called by rt_stop with its status and its message (as
 * printed, without the prefix, at most RT_STOP_MESSAGE_MAX - 1 bytes), after printing it and
 * before the at-stop functions (for a stop inside an at-stop function: before the next
 * one); for the runtime's trace. A stop inside the hook itself does not call it again.
 * machine_reset clears it.
 * rt_exit: the end of a run that did not stop: runs the at-stop functions as rt_stop does,
 * prints nothing, and exits with status. */
void rt_set_stop_hook(void (*fn)(int status, const char *message));
_Noreturn void rt_exit(int status);
#define RT_STOP_MESSAGE_MAX 2048
#define RT_STOP_STATUS 3 /* every stop of the clock, interrupt and exception code */
#define RT_AT_STOP_MAX 16

/* Signals (final review I1). rt_catch_signals: from now on SIGINT, SIGTERM and SIGHUP only
 * set a flag (a volatile sig_atomic_t); the handler does nothing else. rt_poll_signal: when
 * the flag is set, calls end(signal), which must end the run (through rt_exit, so that the
 * at-stop functions run once, on the thread that polls); if end returns, or none was given,
 * rt_exit(RT_INTERRUPT_STATUS). It is called where the program is certain to pass regularly
 * with no guest instruction in progress, or with one that is complete as far as the guest
 * can see: at the start of rt_tick, and in every host loop that can run long (the Voodoo's
 * hold of a CPU store). */
#define RT_INTERRUPT_STATUS 130
void rt_catch_signals(void (*end)(int sig));
void rt_poll_signal(void);

/* --- the state -------------------------------------------------------------------------- */

struct vclock_event {
    uint64_t t;   /* when it is due, ns */
    uint64_t seq; /* scheduling order, for events of equal t */
    void (*fn)(void *);
    void *arg;
};

typedef struct machine {
    uint8_t *ram;           /* the guest RAM, as rt_tick or rt_break last saw it */
    /* the clock */
    uint64_t now;           /* virtual time at the last accounting, ns */
    int32_t slice_loaded;   /* slice as the runtime last left it: checks since are unaccounted */
    struct vclock_event events[VCLOCK_MAX_EVENTS]; /* sorted by (t, seq) */
    int n_events;
    uint64_t event_seq;
    int vblank_enable;      /* interrupt enable 0x17300000 bit 7: VBLANK reaches IP6 */
    int advancing;          /* inside vclock_advance's events */
    /* CP0 and the board's interrupt state */
    uint32_t status;
    uint32_t cause_ip;      /* the pending IP bits, as Cause holds them (bits 8 to 15) */
    uint32_t intctl;        /* IOASIC INTCTL */
    uint32_t gt_cause, gt_mask; /* GT64010 INTR_CAUSE and CPU_MASK */
    /* the handler tables (guest addresses, 0 for none) */
    uint32_t line_fn[8];
    uint32_t gt_fn[32];
    uint32_t exc_fn[16];
    uint32_t fpe_fn[6];
    uint32_t break7_hook, break6_hook;
    uint32_t break_code;    /* the break code being dispatched as code 9, 0 otherwise */
    /* stopping */
    void (*at_stop[RT_AT_STOP_MAX])(void);
    int n_at_stop;
    int stopping;
    void (*stop_hook)(int status, const char *message);
    /* observers (machine_reset clears them) */
    void (*vblank_hook)(void);
} machine_t;

extern machine_t machine;

/* machine_reset: every piece of state above to power-on values (Status ST_POWER_ON, Cause
 * 0, time 0, no handlers, no events, INTCTL INTCTL_AT_ENTRY). */
void machine_reset(void);

/* --- between runtime/core's files ------------------------------------------------------- */

/* vclock_account: adds the checks used since the runtime last set slice to the time.
 * vclock_reload: accounts, then sets slice to 1 when an interrupt is deliverable (so the next
 * check point delivers it), otherwise to the checks until the next event. vclock_run_due:
 * runs the events whose time has come. */
void vclock_account(cpu_t *c);
void vclock_reload(cpu_t *c);
void vclock_run_due(void);
/* irq_deliverable: Status has IE set, EXL clear, and a pending line is unmasked. */
int irq_deliverable(void);
/* exc_call: calls the guest handler fn through rt_call as the loader's dispatcher does, with
 * sp = EXC_STACK and ra = EXC_RETURN_RA (the arguments are the caller's to set); returns its
 * v0. */
uint32_t exc_call(uint8_t *ram, cpu_t *c, uint32_t fn);
/* exc_other: the dispatch of an exception code other than 0 at epc, with the frame written
 * and EXL set; cause is the Cause value. */
void exc_other(uint8_t *ram, cpu_t *c, int code, uint32_t cause, uint32_t epc);

/* A 32-bit word of the frame at byte offset off. */
static inline uint32_t frame_r32(const uint8_t *ram, uint32_t off)
{
    uint32_t v;
    memcpy(&v, ram + (EXC_FRAME & 0x1FFFFFFFu) + off, 4);
    return v;
}

static inline void frame_w32(uint8_t *ram, uint32_t off, uint32_t v)
{
    memcpy(ram + (EXC_FRAME & 0x1FFFFFFFu) + off, &v, 4);
}

#endif
