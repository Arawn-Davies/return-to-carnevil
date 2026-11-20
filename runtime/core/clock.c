/* clock.c: the virtual clock, its event queue, and rt_tick.
 *
 * Time advances by check points only (recomp.h, RT_CHECK): rt_tick counts the checks used
 * since the runtime last set slice, VCLOCK_NS_PER_CHECK ns each, runs the events that are
 * due, delivers a pending interrupt (irq.c), and reloads slice with the checks until the next
 * event. Nothing here reads the host clock, so a run is the same every time.
 */
#include "machine.h"

/* The checks used since the runtime last set slice. slice only goes down between (RT_CHECK
 * decrements it, and nothing else writes it), so a slice above what was loaded is a broken
 * contract, not something to guess about. */
static uint64_t checks_used(const cpu_t *c)
{
    int64_t used = (int64_t)machine.slice_loaded - (int64_t)c->slice;
    if (used < 0)
        rt_stop(RT_STOP_STATUS, "slice is %d, above the %d the runtime loaded: something other "
                "than RT_CHECK wrote it", (int)c->slice, (int)machine.slice_loaded);
    return (uint64_t)used;
}

uint64_t vclock_now(cpu_t *c)
{
    return machine.now + checks_used(c) * VCLOCK_NS_PER_CHECK;
}

void vclock_account(cpu_t *c)
{
    machine.now += checks_used(c) * VCLOCK_NS_PER_CHECK;
    machine.slice_loaded = c->slice;
}

void vclock_reload(cpu_t *c)
{
    int32_t n = VCLOCK_MAX_SLICE;
    vclock_account(c);
    if (irq_deliverable()) {
        n = 1;
    } else if (machine.n_events > 0) {
        /* the checks until the next event, rounded up; 64-bit until clamped to the slice */
        uint64_t t = machine.events[0].t, left = 1;
        if (t > machine.now)
            left = (t - machine.now - 1) / VCLOCK_NS_PER_CHECK + 1;
        if (left < (uint64_t)n)
            n = (int32_t)left;
    }
    c->slice = n;
    machine.slice_loaded = n;
}

void vclock_at(uint64_t t_ns, void (*fn)(void *), void *arg)
{
    int i;
    if (machine.n_events == VCLOCK_MAX_EVENTS)
        rt_stop(RT_STOP_STATUS, "vclock_at: more than %d events pending", VCLOCK_MAX_EVENTS);
    /* after every event due at or before t_ns: equal times keep their scheduling order */
    for (i = machine.n_events; i > 0 && machine.events[i - 1].t > t_ns; i--)
        machine.events[i] = machine.events[i - 1];
    machine.events[i] = (struct vclock_event){t_ns, machine.event_seq++, fn, arg};
    machine.n_events++;
}

void vclock_cancel(void (*fn)(void *), void *arg)
{
    int j = 0;
    for (int i = 0; i < machine.n_events; i++)
        if (machine.events[i].fn != fn || machine.events[i].arg != arg)
            machine.events[j++] = machine.events[i];
    machine.n_events = j;
}

/* vclock_advance (machine.h): the clock steps to each due event's time and runs it, so an
 * event sees its own time and one it schedules inside the interval runs too. Nothing here
 * delivers: only rt_tick does, at a check point. */
void vclock_advance(cpu_t *c, uint64_t ns)
{
    uint64_t end;
    if (machine.advancing)
        rt_stop(RT_STOP_STATUS, "vclock_advance called from inside a clock event");
    vclock_account(c);
    end = machine.now + ns;
    machine.advancing = 1;
    while (machine.n_events > 0 && machine.events[0].t <= end) {
        if (machine.events[0].t > machine.now)
            machine.now = machine.events[0].t;
        vclock_run_due();
    }
    machine.advancing = 0;
    machine.now = end;
    vclock_reload(c);
}

void vclock_run_due(void)
{
    while (machine.n_events > 0 && machine.events[0].t <= machine.now) {
        struct vclock_event e = machine.events[0];
        machine.n_events--;
        for (int i = 0; i < machine.n_events; i++)
            machine.events[i] = machine.events[i + 1];
        e.fn(e.arg);
    }
}

/* rt_tick (recomp.h). While a handler runs (EXL set) it still advances time and runs
 * events, so interrupts become pending, but delivers nothing; vclock_reload then makes the
 * first check after the dispatcher returns deliver them. One delivery per tick, so the
 * interrupted code runs at least one check between two. */
void rt_tick(uint8_t *ram, cpu_t *c)
{
    machine.ram = ram;
    vclock_account(c);
    vclock_run_due();
    if (irq_deliverable()) {
        uint32_t epc = c->pc, resume = exc_raise(ram, c, 0, epc);
        if (resume != epc)
            rt_stop(RT_STOP_STATUS, "an interrupt handler moved the resume pc from 0x%08X to "
                    "0x%08X (pc as last stored): the runtime resumes only where it was "
                    "interrupted", (unsigned)epc, (unsigned)resume);
        vclock_account(c);
        vclock_run_due();
    }
    vclock_reload(c);
}
