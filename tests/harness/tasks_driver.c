/* tasks_driver.c: runs runtime/core/tasks.c (the host contexts of runtime/core/tasks.h and the
 * game glue rt_host_switch) for tests/test_tasks.py. argv[1] names the scenario; each prints
 * what it saw on stdout and exits 0, unless it ends in a stop (rt_stop, status 3).
 *
 *   ping          three contexts each park PING_PARKS times, then end (context 0 by
 *                 returning from its entry, the others through ctx_end); the scheduler picks
 *                 the next live one with a fixed pseudo-random sequence and checks that the
 *                 context it switched to is the one that ran. Prints "ping switches N
 *                 violations V parks P0 P1 P2 ended E0 E1 E2 roles S T" (S: ctx_is_scheduler
 *                 on this thread, T: the most any context saw it true, so 1 0).
 *   retire        three contexts that count runs before and after each park; each is run
 *                 twice, context 0 is retired, the other two run once more, and the program
 *                 exits with status 0 while they are parked. Prints "retire before B0 B1 B2
 *                 after A0 A1 A2".
 *   fcsr          the glue's FCSR hand-over: task A sets round toward zero and yields; the
 *                 scheduler, then task B (a new thread), compute 1.0f / 3.0f; B sets round
 *                 to nearest and yields; the scheduler, then A (resumed), compute it again.
 *                 Prints "third WHO BITS" for each.
 *   stop          two contexts park, a third calls rt_stop with an at-stop function
 *                 registered: the process stops with status 3 while two threads are parked.
 *   misuse KIND   one misuse of rt_host_switch, which stops (status 3): yield or exit on
 *                 the scheduler's context, resume with EXL set, resume on a task context
 *                 (resume-in-task), resume of a record with no context (no-context), a 65th
 *                 context (full), resume after tasks_reset forgot the record (reset), an
 *                 exit on a task context the glue has no record for (exit-unmapped).
 *   reuse         the game frees a suspended task and reuses its record: record A is run at
 *                 the trampoline and yields, then resumed at the trampoline again while its
 *                 first context is parked, then resumed normally. Prints "run gen G" at each
 *                 body's start, "resumed gen G" when a body comes back from a yield, and
 *                 "live N" (ctx_count) after each resume.
 *   guest MODE    commands on standard input run guest code, in the interpreter (MODE
 *                 interp) or through the recompiled functions a linked generated file
 *                 registers (MODE recomp, where any entry into the interpreter prints
 *                 "unrecompiled ADDR"):
 *                   reset           tasks_reset, machine_reset; RAM and cpu_t zeroed, FCSR 0
 *                   mem ADDR WORD   a RAM word
 *                   r N V           general register N
 *                   run PC          rt_call to PC, then "ran"
 *                   dump ADDR N     "dump W0 .. W(N-1)", N RAM words from ADDR
 *
 * Outside guest mode, recomp_lookup knows one function: a native trampoline at 0x8014A930
 * (the address the glue enters a new task at), which runs the scenario's C task body for the
 * record in the word 0x801A3118. Those bodies yield through rt_host_switch, as the game's
 * yield does.
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "harness.h"
#include "machine.h"
#include "tasks.h"

#define TRAMPOLINE 0x8014A930u
#define CURRENT_TASK 0x801A3118u
#define REC_A 0x80180000u
#define REC_B 0x80180040u
#define HOST_STACK (256u * 1024u)
#define PING_CTX 3
#define PING_PARKS 1000

static uint8_t *ram;
static cpu_t cpu;
static int guest_recomp; /* guest mode: recomp_lookup answers from the registry */
static int guest;

static _Noreturn void die(const char *what, const char *detail)
{
    fflush(stdout);
    fprintf(stderr, "tasks_driver: %s%s%s\n", what, detail ? ": " : "", detail ? detail : "");
    exit(1);
}

/* --- what the core needs from a runtime that it does not provide ------------------------ */

uint32_t bus_read(cpu_t *c, uint32_t addr, int size)
{
    (void)c, (void)size;
    fprintf(stderr, "tasks_driver: bus_read %08" PRIX32 "\n", addr);
    exit(1);
}

void bus_write(cpu_t *c, uint32_t addr, int size, uint32_t value)
{
    (void)c, (void)size, (void)value;
    fprintf(stderr, "tasks_driver: bus_write %08" PRIX32 "\n", addr);
    exit(1);
}

void rt_loader(uint8_t *r, cpu_t *c, uint32_t slot)
{
    (void)r, (void)c;
    fprintf(stderr, "tasks_driver: rt_loader %03" PRIX32 "\n", slot);
    exit(1);
}

/* --- the registries a generated file fills (harness.h) ---------------------------------- */

#define REGISTRY_MAX 64

static uint32_t fn_addr[REGISTRY_MAX];
static recomp_fn fn_ptr[REGISTRY_MAX];
static unsigned fn_count;
static uint32_t switch_pc[REGISTRY_MAX];
static int switch_kind[REGISTRY_MAX];
static unsigned switch_count;

void harness_register(uint32_t addr, recomp_fn fn)
{
    if (fn_count == REGISTRY_MAX)
        die("too many registered functions", NULL);
    fn_addr[fn_count] = addr;
    fn_ptr[fn_count++] = fn;
}

void harness_register_named(const char *name, recomp_fn fn)
{
    (void)name, (void)fn;
}

void harness_register_switch(uint32_t pc, int kind)
{
    if (switch_count == REGISTRY_MAX)
        die("too many host-switch points", NULL);
    switch_pc[switch_count] = pc;
    switch_kind[switch_count++] = kind;
}

int rt_switch_kind(uint32_t pc)
{
    for (unsigned i = 0; i < switch_count; i++)
        if (switch_pc[i] == pc)
            return switch_kind[i];
    return -1;
}

static void native_trampoline(uint8_t *r, cpu_t *c);

recomp_fn recomp_lookup(uint32_t addr)
{
    if (!guest)
        return addr == TRAMPOLINE ? native_trampoline : NULL;
    if (!guest_recomp)
        return NULL;
    for (unsigned i = 0; i < fn_count; i++)
        if (fn_addr[i] == addr)
            return fn_ptr[i];
    return NULL;
}

static void log_unrecompiled(uint32_t addr)
{
    printf("unrecompiled %08" PRIX32 "\n", addr);
}

/* --- helpers ----------------------------------------------------------------------------- */

static uint32_t third(void)
{
    volatile float a = 1.0f, b = 3.0f;
    float q = a / b;
    uint32_t bits;
    memcpy(&bits, &q, 4);
    return bits;
}

static void put_word(uint32_t addr, uint32_t value)
{
    memcpy(ram + (addr & 0x1FFFFFFFu), &value, 4);
}

/* A glue resume of record with ra as the scheduler's restore path would have loaded it. */
static void resume(uint32_t record, uint32_t ra)
{
    put_word(CURRENT_TASK, record);
    cpu.r[31] = ra;
    cpu.pc = 0x80151E88u;
    rt_host_switch(ram, &cpu, RT_SWITCH_RESUME);
}

static void yield(void)
{
    cpu.pc = 0x80151B48u;
    rt_host_switch(ram, &cpu, RT_SWITCH_YIELD);
}

/* --- native task bodies, entered through the trampoline --------------------------------- */

static void (*native_body)(uint32_t record);

static void native_trampoline(uint8_t *r, cpu_t *c)
{
    uint32_t record;
    memcpy(&record, r + (CURRENT_TASK & 0x1FFFFFFFu), 4);
    (void)c;
    native_body(record);
    die("a native task body returned", NULL);
}

static void body_yield_forever(uint32_t record)
{
    (void)record;
    for (;;)
        yield();
}

static void body_fcsr(uint32_t record)
{
    if (record == REC_A) {
        rt_set_fcsr(&cpu, 1); /* RM = 1, toward zero */
        yield();
        printf("third A %08" PRIX32 "\n", third());
    } else {
        printf("third B %08" PRIX32 "\n", third());
        rt_set_fcsr(&cpu, 0); /* RM = 0, nearest */
    }
    body_yield_forever(record);
}

static void body_resume_in_task(uint32_t record)
{
    (void)record;
    resume(REC_B, TRAMPOLINE);
}

static int reuse_gen;

static void body_reuse(uint32_t record)
{
    int me = ++reuse_gen;
    (void)record;
    printf("run gen %d\n", me);
    for (;;) {
        yield();
        printf("resumed gen %d\n", me);
    }
}

static void exit_unmapped_entry(void *arg)
{
    (void)arg;
    cpu.pc = 0x80151B78u;
    rt_host_switch(ram, &cpu, RT_SWITCH_EXIT);
}

/* --- scenarios on the generic contexts --------------------------------------------------- */

static int ping_last = -1, ping_sched_seen;
static long ping_parks[PING_CTX];
static int ping_ended[PING_CTX];

static void ping_entry(void *arg)
{
    int id = (int)(intptr_t)arg;
    for (int i = 0; i < PING_PARKS; i++) {
        ping_last = id;
        if (ctx_is_scheduler())
            ping_sched_seen = 1;
        ctx_park();
        ping_parks[id]++;
    }
    ping_last = id;
    ping_ended[id] = 1;
    if (id != 0)
        ctx_end();
}

static void ping(void)
{
    task_ctx *t[PING_CTX];
    long switches = 0, violations = 0;
    uint32_t lcg = 12345u;
    int live = PING_CTX;
    for (int i = 0; i < PING_CTX; i++)
        t[i] = ctx_spawn(ping_entry, (void *)(intptr_t)i, HOST_STACK);
    while (live) {
        int pick;
        lcg = lcg * 1103515245u + 12345u;
        pick = (int)(lcg >> 16) % PING_CTX;
        while (!t[pick])
            pick = (pick + 1) % PING_CTX;
        ping_last = -1;
        ctx_switch_to(t[pick]);
        switches++;
        if (ping_last != pick)
            violations++;
        if (ping_ended[pick]) {
            t[pick] = NULL;
            live--;
        }
    }
    printf("ping switches %ld violations %ld parks %ld %ld %ld ended %d %d %d roles %d %d\n",
           switches, violations, ping_parks[0], ping_parks[1], ping_parks[2], ping_ended[0],
           ping_ended[1], ping_ended[2], ctx_is_scheduler(), ping_sched_seen);
}

static long retire_before[3], retire_after[3];

static void retire_entry(void *arg)
{
    int id = (int)(intptr_t)arg;
    for (;;) {
        retire_before[id]++;
        ctx_park();
        retire_after[id]++;
    }
}

static void retire(void)
{
    task_ctx *t[3];
    for (int i = 0; i < 3; i++)
        t[i] = ctx_spawn(retire_entry, (void *)(intptr_t)i, HOST_STACK);
    for (int round = 0; round < 2; round++)
        for (int i = 0; i < 3; i++)
            ctx_switch_to(t[i]);
    ctx_retire(t[0]);
    ctx_switch_to(t[1]);
    ctx_switch_to(t[2]);
    printf("retire before %ld %ld %ld after %ld %ld %ld\n", retire_before[0],
           retire_before[1], retire_before[2], retire_after[0], retire_after[1],
           retire_after[2]);
}

static void stop_at_stop(void)
{
    fputs("at_stop ran\n", stderr);
}

static void stop_entry(void *arg)
{
    if ((intptr_t)arg == 2) {
        rt_at_stop(stop_at_stop);
        rt_stop(RT_STOP_STATUS, "stop from a task context");
    }
    for (;;)
        ctx_park();
}

static void stop(void)
{
    for (int i = 0; i < 3; i++)
        ctx_switch_to(ctx_spawn(stop_entry, (void *)(intptr_t)i, HOST_STACK));
    die("rt_stop returned to the scheduler", NULL);
}

/* --- scenarios on the glue --------------------------------------------------------------- */

static void fcsr(void)
{
    native_body = body_fcsr;
    resume(REC_A, TRAMPOLINE);
    printf("third scheduler %08" PRIX32 "\n", third());
    resume(REC_B, TRAMPOLINE);
    printf("third scheduler %08" PRIX32 "\n", third());
    resume(REC_A, 0x80100000u);
    printf("third scheduler %08" PRIX32 "\n", third());
    tasks_reset();
}

static void misuse(const char *kind)
{
    native_body = body_yield_forever;
    put_word(CURRENT_TASK, REC_A);
    if (strcmp(kind, "yield") == 0) {
        yield();
    } else if (strcmp(kind, "exit") == 0) {
        cpu.pc = 0x80151B78u;
        rt_host_switch(ram, &cpu, RT_SWITCH_EXIT);
    } else if (strcmp(kind, "exl") == 0) {
        m_status_set(ST_EXL, 0);
        resume(REC_A, TRAMPOLINE);
    } else if (strcmp(kind, "resume-in-task") == 0) {
        native_body = body_resume_in_task;
        resume(REC_A, TRAMPOLINE);
    } else if (strcmp(kind, "no-context") == 0) {
        resume(REC_A, 0x80100000u);
    } else if (strcmp(kind, "full") == 0) {
        for (uint32_t i = 0; i < 65; i++) {
            printf("spawned %" PRIu32 "\n", i);
            fflush(stdout);
            resume(REC_A + 0x40u * i, TRAMPOLINE);
        }
    } else if (strcmp(kind, "reset") == 0) {
        resume(REC_A, TRAMPOLINE);
        tasks_reset();
        resume(REC_A, 0x80100000u);
    } else if (strcmp(kind, "exit-unmapped") == 0) {
        ctx_switch_to(ctx_spawn(exit_unmapped_entry, NULL, HOST_STACK));
    } else {
        die("unknown misuse", kind);
    }
    puts("returned");
}

static void reuse(void)
{
    native_body = body_reuse;
    resume(REC_A, TRAMPOLINE);
    printf("live %d\n", ctx_count());
    resume(REC_A, TRAMPOLINE);
    printf("live %d\n", ctx_count());
    resume(REC_A, 0x80100000u);
    printf("live %d\n", ctx_count());
    tasks_reset();
}

/* --- guest mode -------------------------------------------------------------------------- */

static uint32_t hex(void)
{
    uint32_t v;
    if (scanf("%" SCNx32, &v) != 1)
        die("bad input", "expected a hexadecimal number");
    return v;
}

static void reset(void)
{
    tasks_reset();
    machine_reset();
    memset(ram, 0, RAM_SIZE);
    memset(&cpu, 0, sizeof cpu);
    rt_set_fcsr(&cpu, 0);
}

static void run_guest(void)
{
    static char key[16];
    while (scanf("%15s", key) == 1) {
        if (strcmp(key, "reset") == 0) {
            reset();
        } else if (strcmp(key, "mem") == 0) {
            uint32_t addr = hex(), word = hex();
            if ((addr & 3u) || !mem_is_ram(addr, 4))
                die("mem: not an aligned RAM address", NULL);
            put_word(addr, word);
        } else if (strcmp(key, "r") == 0) {
            uint32_t n = hex();
            if (n > 31)
                die("bad input", "register number above 31");
            cpu.r[n] = hex();
        } else if (strcmp(key, "run") == 0) {
            rt_call(ram, &cpu, hex());
            puts("ran");
        } else if (strcmp(key, "dump") == 0) {
            uint32_t addr = hex(), n = hex();
            fputs("dump", stdout);
            for (uint32_t i = 0; i < n; i++)
                printf(" %08" PRIX32, mem_r32(ram, &cpu, addr + 4 * i));
            putchar('\n');
        } else {
            die("bad input: unknown command", key);
        }
        fflush(stdout);
    }
    if (!feof(stdin))
        die("bad input", "unreadable");
}

int main(int argc, char **argv)
{
    if (argc < 2)
        die("usage", "tasks_driver SCENARIO [ARG]");
    ram = calloc(1, RAM_SIZE);
    if (!ram)
        die("out of memory", NULL);
    machine_reset();
    rt_set_fcsr(&cpu, 0);
    if (strcmp(argv[1], "ping") == 0) {
        ping();
    } else if (strcmp(argv[1], "retire") == 0) {
        retire();
    } else if (strcmp(argv[1], "fcsr") == 0) {
        fcsr();
    } else if (strcmp(argv[1], "stop") == 0) {
        stop();
    } else if (strcmp(argv[1], "misuse") == 0 && argc == 3) {
        misuse(argv[2]);
    } else if (strcmp(argv[1], "reuse") == 0) {
        reuse();
    } else if (strcmp(argv[1], "guest") == 0 && argc == 3) {
        guest = 1;
        guest_recomp = strcmp(argv[2], "recomp") == 0;
        if (guest_recomp)
            rt_log_unrecompiled = log_unrecompiled;
        else if (strcmp(argv[2], "interp") != 0)
            die("unknown guest mode", argv[2]);
        run_guest();
        tasks_reset();
    } else {
        die("unknown scenario", argv[1]);
    }
    fflush(stdout);
    return 0; /* in retire, with two contexts still parked */
}
