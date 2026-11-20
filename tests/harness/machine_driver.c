/* machine_driver.c: runs the runtime core's clock, interrupt and exception dispatch
 * (runtime/include/machine.h) from commands on standard input, for tests/test_clock_irq.py.
 * Guest code runs in the interpreter; recomp_lookup knows one function only, PROBE.
 *
 * One command per line. Numbers are hexadecimal, except times in nanoseconds (decimal):
 *   reset                machine_reset; RAM and cpu_t zeroed, slice 0, FCSR 0
 *   mem ADDR WORD        a RAM word
 *   r N V, fpr N V       general register N, FP register half N
 *   hilo HI LO, fcsr V   HI and LO; FCSR as ctc1 writes it
 *   pc V, slice V        c->pc; c->slice (as if something else wrote it)
 *   line LINE FN         irq_set_line
 *   gt BIT FN            irq_set_gt
 *   exc CODE FN          exc_set_handler
 *   fpe CAUSE FN         exc_set_fpe
 *   hook WHICH FN        exc_set_break_hook
 *   vblank NS            a stand-in for the Voodoo's retrace (runtime/hw drives it in the
 *                        runtime): vblank_raise every NS from one NS after the time last
 *                        accounted, replacing an earlier one
 *   event ID NS          vclock_at an event that prints "event ID NOW"
 *   chain ID NS          the same, and the event also schedules event ID + 1 at the time it
 *                        runs
 *   cancel ID            vclock_cancel of event ID
 *   irqraise IP          irq_raise
 *   gtraise BIT          gt_cause_raise
 *   cause V              the pending IP bits (machine.cause_ip) set to V, bypassing irq_raise
 *   intctl V             IOASIC INTCTL set to V
 *   tick                 rt_tick as if the slice ran out (slice 0 first)
 *   reload               vclock_reload
 *   atstop               rt_at_stop of two functions that print "at_stop 1" and "at_stop 2"
 *                        on stderr
 *   stophook             rt_set_stop_hook of a function that prints "stop_hook STATUS MESSAGE"
 *                        on stderr
 *   exit STATUS          rt_exit(STATUS) (decimal)
 *   vbhook               irq_set_vblank_hook of a function that prints "vblank_hook NOW"
 *   run PC               interp_run from PC, then "ran" and the state
 *   break PC CODE        rt_break, then "returned" and the state
 *   raise CODE EPC       exc_raise, then "resume PC" and the state
 *   fptrap BIT           rt_fp_trap(c, BIT), then "returned" and the state
 *   badtarget ADDR       rt_bad_target
 *   checks K             K check points that do not reach 0 (slice -= K), then "now NOW"
 *   advance NS           ticks as if each slice ran out, until vclock_now reaches NS; then
 *                        "advanced NOW TICKS MAXSLICE MINSLICE BACKWARDS" (BACKWARDS: how
 *                        often vclock_now went down across a tick)
 *   dump ADDR N          "dump W0 .. W(N-1)", N RAM words from ADDR
 *   handlers             "handlers F0 .. F15", the exception table
 * The state is the lines "r R0 .. R31", "hilo HI LO", "fpr F0 .. F31", "fcsr X" (as cfc1
 * reads it) and "m STATUS CAUSE_IP INTCTL GT_CAUSE NOW SLICE PC".
 *
 * PROBE (0x80300000) prints "probe A0 A1 A2 SP STATUS NOW" and returns, changing nothing.
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "machine.h"

#define PROBE 0x80300000u

static uint8_t *ram;
static cpu_t cpu;

static _Noreturn void die(const char *what, const char *detail)
{
    fflush(stdout);
    fprintf(stderr, "machine_driver: %s%s%s\n", what, detail ? ": " : "", detail ? detail : "");
    exit(1);
}

/* --- what the core needs from a runtime that it does not provide ------------------------ */

uint32_t bus_read(cpu_t *c, uint32_t addr, int size)
{
    (void)c, (void)size;
    fprintf(stderr, "machine_driver: bus_read %08" PRIX32 "\n", addr);
    exit(1);
}

void bus_write(cpu_t *c, uint32_t addr, int size, uint32_t value)
{
    (void)c, (void)size, (void)value;
    fprintf(stderr, "machine_driver: bus_write %08" PRIX32 "\n", addr);
    exit(1);
}

void rt_loader(uint8_t *r, cpu_t *c, uint32_t slot)
{
    (void)r, (void)c;
    fprintf(stderr, "machine_driver: rt_loader %03" PRIX32 "\n", slot);
    exit(1);
}

void rt_host_switch(uint8_t *r, cpu_t *c, int kind)
{
    (void)r, (void)c;
    fprintf(stderr, "machine_driver: rt_host_switch %d\n", kind);
    exit(1);
}

int rt_switch_kind(uint32_t pc)
{
    (void)pc;
    return -1;
}

static void probe(uint8_t *r, cpu_t *c)
{
    (void)r;
    printf("probe %08" PRIX32 " %08" PRIX32 " %08" PRIX32 " %08" PRIX32 " %08" PRIX32
           " %" PRIu64 "\n", c->r[4], c->r[5], c->r[6], c->r[29], m_status(), vclock_now(c));
}

recomp_fn recomp_lookup(uint32_t addr)
{
    return addr == PROBE ? probe : NULL;
}

/* --- events and stop functions ----------------------------------------------------------- */

static void on_event(void *arg)
{
    printf("event %" PRIuPTR " %" PRIu64 "\n", (uintptr_t)arg, vclock_now(&cpu));
}

static void on_chain(void *arg)
{
    on_event(arg);
    vclock_at(vclock_now(&cpu), on_event, (void *)((uintptr_t)arg + 1));
}

static uint64_t retrace_period, retrace_next;

static void on_retrace(void *arg)
{
    (void)arg;
    vblank_raise();
    retrace_next += retrace_period;
    vclock_at(retrace_next, on_retrace, NULL);
}

static void at_stop_1(void)
{
    fputs("at_stop 1\n", stderr);
}

static void at_stop_2(void)
{
    fputs("at_stop 2\n", stderr);
}

static void stop_hook(int status, const char *message)
{
    fprintf(stderr, "stop_hook %d %s\n", status, message);
}

static void vblank_hook(void)
{
    printf("vblank_hook %" PRIu64 "\n", vclock_now(&cpu));
}

/* --- input and output -------------------------------------------------------------------- */

static uint32_t hex(void)
{
    uint32_t v;
    if (scanf("%" SCNx32, &v) != 1)
        die("bad input", "expected a hexadecimal number");
    return v;
}

static uint64_t dec(void)
{
    uint64_t v;
    if (scanf("%" SCNu64, &v) != 1)
        die("bad input", "expected a decimal number");
    return v;
}

static uint32_t reg_index(void)
{
    uint32_t n = hex();
    if (n > 31)
        die("bad input", "register number above 31");
    return n;
}

static void put_words(const char *key, const uint32_t *v, int n)
{
    fputs(key, stdout);
    for (int i = 0; i < n; i++)
        printf(" %08" PRIX32, v[i]);
    putchar('\n');
}

static void print_state(void)
{
    put_words("r", cpu.r, 32);
    printf("hilo %08" PRIX32 " %08" PRIX32 "\n", cpu.hi, cpu.lo);
    put_words("fpr", cpu.fpr, 32);
    printf("fcsr %08" PRIX32 "\n", rt_get_fcsr(&cpu));
    printf("m %08" PRIX32 " %08" PRIX32 " %08" PRIX32 " %08" PRIX32 " %" PRIu64 " %" PRId32
           " %08" PRIX32 "\n", m_status(), machine.cause_ip, machine.intctl, machine.gt_cause,
           vclock_now(&cpu), cpu.slice, cpu.pc);
    fflush(stdout);
}

static void reset(void)
{
    machine_reset();
    memset(ram, 0, RAM_SIZE);
    memset(&cpu, 0, sizeof cpu);
    rt_set_fcsr(&cpu, 0);
}

static void advance(uint64_t until)
{
    uint64_t ticks = 0, backwards = 0;
    int32_t max_slice = 0, min_slice = INT32_MAX;
    while (vclock_now(&cpu) < until) {
        uint64_t before;
        cpu.slice = 0; /* every check point of the slice ran */
        before = vclock_now(&cpu);
        rt_tick(ram, &cpu);
        if (vclock_now(&cpu) < before)
            backwards++;
        if (cpu.slice > max_slice)
            max_slice = cpu.slice;
        if (cpu.slice < min_slice)
            min_slice = cpu.slice;
        ticks++;
    }
    printf("advanced %" PRIu64 " %" PRIu64 " %" PRId32 " %" PRId32 " %" PRIu64 "\n",
           vclock_now(&cpu), ticks, max_slice, min_slice, backwards);
}

int main(void)
{
    static char key[16];
    ram = malloc(RAM_SIZE);
    if (!ram)
        die("out of memory", NULL);
    reset();
    while (scanf("%15s", key) == 1) {
        if (strcmp(key, "reset") == 0) {
            reset();
        } else if (strcmp(key, "mem") == 0) {
            uint32_t addr = hex(), word = hex();
            if ((addr & 3u) || !mem_is_ram(addr, 4))
                die("mem: not an aligned RAM address", NULL);
            memcpy(ram + (addr & 0x1FFFFFFFu), &word, 4);
        } else if (strcmp(key, "r") == 0) {
            uint32_t n = reg_index();
            cpu.r[n] = hex();
        } else if (strcmp(key, "fpr") == 0) {
            uint32_t n = reg_index();
            cpu.fpr[n] = hex();
        } else if (strcmp(key, "hilo") == 0) {
            cpu.hi = hex();
            cpu.lo = hex();
        } else if (strcmp(key, "fcsr") == 0) {
            rt_set_fcsr(&cpu, hex());
        } else if (strcmp(key, "pc") == 0) {
            cpu.pc = hex();
        } else if (strcmp(key, "slice") == 0) {
            cpu.slice = (int32_t)hex();
        } else if (strcmp(key, "line") == 0) {
            uint32_t line = hex();
            irq_set_line((int)line, hex());
        } else if (strcmp(key, "gt") == 0) {
            uint32_t bit = hex();
            irq_set_gt((int)bit, hex());
        } else if (strcmp(key, "exc") == 0) {
            uint32_t code = hex();
            exc_set_handler((int)code, hex());
        } else if (strcmp(key, "fpe") == 0) {
            uint32_t cause = hex();
            exc_set_fpe((int)cause, hex());
        } else if (strcmp(key, "hook") == 0) {
            uint32_t which = hex();
            exc_set_break_hook((int)which, hex());
        } else if (strcmp(key, "vblank") == 0) {
            retrace_period = dec();
            if (!retrace_period)
                die("bad input", "vblank: period 0");
            vclock_cancel(on_retrace, NULL);
            retrace_next = machine.now + retrace_period;
            vclock_at(retrace_next, on_retrace, NULL);
        } else if (strcmp(key, "event") == 0 || strcmp(key, "chain") == 0) {
            uint32_t id = hex();
            vclock_at(dec(), key[0] == 'e' ? on_event : on_chain, (void *)(uintptr_t)id);
        } else if (strcmp(key, "cancel") == 0) {
            vclock_cancel(on_event, (void *)(uintptr_t)hex());
        } else if (strcmp(key, "irqraise") == 0) {
            irq_raise((int)hex());
        } else if (strcmp(key, "gtraise") == 0) {
            gt_cause_raise((int)hex());
        } else if (strcmp(key, "cause") == 0) {
            machine.cause_ip = hex();
        } else if (strcmp(key, "intctl") == 0) {
            machine.intctl = hex();
        } else if (strcmp(key, "tick") == 0) {
            cpu.slice = 0;
            rt_tick(ram, &cpu);
        } else if (strcmp(key, "reload") == 0) {
            vclock_reload(&cpu);
        } else if (strcmp(key, "atstop") == 0) {
            rt_at_stop(at_stop_1);
            rt_at_stop(at_stop_2);
        } else if (strcmp(key, "stophook") == 0) {
            rt_set_stop_hook(stop_hook);
        } else if (strcmp(key, "exit") == 0) {
            int status = (int)dec();
            fflush(stdout);
            rt_exit(status);
        } else if (strcmp(key, "vbhook") == 0) {
            irq_set_vblank_hook(vblank_hook);
        } else if (strcmp(key, "run") == 0) {
            interp_run(ram, &cpu, hex());
            puts("ran");
            print_state();
        } else if (strcmp(key, "break") == 0) {
            uint32_t pc = hex();
            rt_break(ram, &cpu, pc, hex());
            puts("returned");
            print_state();
        } else if (strcmp(key, "raise") == 0) {
            uint32_t code = hex(), resume = exc_raise(ram, &cpu, (int)code, hex());
            printf("resume %08" PRIX32 "\n", resume);
            print_state();
        } else if (strcmp(key, "fptrap") == 0) {
            rt_fp_trap(&cpu, hex());
            puts("returned");
            print_state();
        } else if (strcmp(key, "badtarget") == 0) {
            rt_bad_target(ram, &cpu, hex());
            puts("returned");
        } else if (strcmp(key, "checks") == 0) {
            cpu.slice -= (int32_t)hex();
            printf("now %" PRIu64 "\n", vclock_now(&cpu));
        } else if (strcmp(key, "advance") == 0) {
            advance(dec());
        } else if (strcmp(key, "dump") == 0) {
            uint32_t addr = hex(), n = hex();
            fputs("dump", stdout);
            for (uint32_t i = 0; i < n; i++)
                printf(" %08" PRIX32, mem_r32(ram, &cpu, addr + 4 * i));
            putchar('\n');
        } else if (strcmp(key, "handlers") == 0) {
            put_words("handlers", machine.exc_fn, 16);
        } else {
            die("bad input: unknown command", key);
        }
        fflush(stdout);
    }
    if (!feof(stdin))
        die("bad input", "unreadable");
    free(ram);
    return 0;
}
