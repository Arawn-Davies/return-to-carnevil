/* tasks.c: host contexts for the game's cooperative tasks (tasks.h), and below its banner the
 * game glue rt_host_switch (recomp.h) that runs the task switch on them.
 *
 * A context is a POSIX thread with its own host stack. The hand-over protocol:
 *
 * - One mutex, baton_lock, guards `baton`, the context allowed to run, and every context's
 *   state and retire flag. Each context has its own condition variable, `wake`.
 * - A context runs only while baton points at it. It waits for the baton with
 *   `while (baton != me) pthread_cond_wait(&me->wake, &baton_lock)`, so a spurious wake-up
 *   (or a signal meant for an earlier hand-over) finds the predicate false and waits again:
 *   it cannot let a second context run.
 * - Only the holder changes baton, and it does so under the mutex, setting baton to the
 *   target and signalling the target's wake, then (unless it is ending) waiting for the baton
 *   itself in the same critical section. The target cannot leave its wait until the giver
 *   has released the mutex inside pthread_cond_wait, so the two never run outside the mutex
 *   at the same time. Because the predicate is set under the mutex before the signal, a
 *   target that has not yet started waiting (a thread just created) finds baton == itself
 *   when it first takes the mutex: no wake-up is lost between "park" and "switch to".
 * - Only the scheduler hands the baton to a task, and only a parked one: the scheduler runs
 *   only while it holds the baton, and then every task context is blocked in its wait, so a
 *   context cannot be switched to while it is running. A task hands the baton back only to
 *   the scheduler (ctx_park, ctx_end, or when it finds itself retired).
 * - A thread does run outside the baton in two places, neither touching any shared data: a
 *   new thread from its creation up to its first wait (it takes the mutex and waits), and an
 *   ending or retired thread from releasing the mutex to the end of pthread_exit, which the
 *   scheduler's pthread_join waits for before it releases the context.
 *
 * Every hand-over is a full synchronous transfer through that mutex, so all other runtime
 * data, and the guest's cpu_t and RAM, are only ever touched by one thread at a time, with a
 * mutex release and acquire between consecutive threads: nothing else needs a lock.
 *
 * Nothing is registered at exit and nothing here has a destructor, so a stop (rt_stop calls
 * exit, possibly from a task's thread) exits promptly with parked threads still blocked.
 */
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "machine.h"
#include "tasks.h"

enum ctx_state {
    CTX_PARKED,  /* blocked waiting for the baton, or created and not yet run */
    CTX_RUNNING, /* handed the baton by the scheduler, not yet parked or ended */
    CTX_ENDED    /* handed the baton back for the last time; its thread is exiting */
};

struct task_ctx {
    pthread_t thread;
    pthread_cond_t wake;   /* signalled when the baton is handed to this context */
    enum ctx_state state;  /* under baton_lock */
    int retire;            /* under baton_lock: on waking, end without running more */
    void (*entry)(void *);
    void *arg;
    task_ctx *prev, *next; /* the live list, the scheduler's alone */
};

static pthread_mutex_t baton_lock = PTHREAD_MUTEX_INITIALIZER;
static task_ctx scheduler = {.wake = PTHREAD_COND_INITIALIZER, .state = CTX_RUNNING};
static task_ctx *baton;               /* under baton_lock */
static _Thread_local task_ctx *self;  /* this thread's context; NULL before it is one */
static int started;
static task_ctx *live;                /* every context spawned and not yet released */

static void check(int err, const char *what)
{
    if (err)
        rt_stop(RT_STOP_STATUS, "tasks: %s failed: %s", what, strerror(err));
}

static void lock(void)
{
    check(pthread_mutex_lock(&baton_lock), "pthread_mutex_lock");
}

static void unlock(void)
{
    check(pthread_mutex_unlock(&baton_lock), "pthread_mutex_unlock");
}

/* The first thread to call into this file becomes the scheduler's context, holding the
 * baton. Task threads exist only after that, so they never start it. */
static void start(void)
{
    if (started)
        return;
    started = 1;
    self = &scheduler;
    baton = &scheduler;
}

static void need_scheduler(const char *what)
{
    start();
    if (self != &scheduler)
        rt_stop(RT_STOP_STATUS, "tasks: %s on a task context; only the scheduler's may", what);
}

static void need_task(const char *what)
{
    start();
    if (self == &scheduler || self == NULL)
        rt_stop(RT_STOP_STATUS, "tasks: %s on the scheduler's context; only a task's may",
                what);
}

/* With baton_lock held: the baton to t. */
static void hand_to(task_ctx *t)
{
    baton = t;
    check(pthread_cond_signal(&t->wake), "pthread_cond_signal");
}

/* With baton_lock held: returns, still holding it, once the baton is me's. */
static void wait_baton(task_ctx *me)
{
    while (baton != me)
        check(pthread_cond_wait(&me->wake, &baton_lock), "pthread_cond_wait");
}

/* With baton_lock held, on a context just woken with its retire flag set: the baton back to
 * the scheduler (waiting in ctx_retire) and the end of the thread, from inside the wait, so
 * nothing more of its entry function runs. */
static _Noreturn void end_retired(void)
{
    hand_to(&scheduler);
    unlock();
    pthread_exit(NULL);
}

/* On the scheduler's context, t's thread having handed back the baton for the last time:
 * joins it (which releases its host stack) and frees t. */
static void release(task_ctx *t)
{
    check(pthread_join(t->thread, NULL), "pthread_join");
    check(pthread_cond_destroy(&t->wake), "pthread_cond_destroy");
    if (t->prev)
        t->prev->next = t->next;
    else
        live = t->next;
    if (t->next)
        t->next->prev = t->prev;
    free(t);
}

static void *ctx_main(void *arg)
{
    task_ctx *t = arg;
    self = t;
    lock();
    wait_baton(t);
    if (t->retire)
        end_retired();
    unlock();
    t->entry(t->arg);
    ctx_end();
}

task_ctx *ctx_spawn(void (*entry)(void *), void *arg, size_t host_stack)
{
    pthread_attr_t attr;
    task_ctx *t;
    need_scheduler("ctx_spawn");
    t = calloc(1, sizeof *t);
    if (!t)
        rt_stop(RT_STOP_STATUS, "tasks: ctx_spawn: out of memory");
    t->entry = entry;
    t->arg = arg;
    t->state = CTX_PARKED;
    check(pthread_cond_init(&t->wake, NULL), "pthread_cond_init");
    check(pthread_attr_init(&attr), "pthread_attr_init");
    check(pthread_attr_setstacksize(&attr, host_stack), "pthread_attr_setstacksize");
    check(pthread_create(&t->thread, &attr, ctx_main, t), "pthread_create");
    check(pthread_attr_destroy(&attr), "pthread_attr_destroy");
    t->next = live;
    if (live)
        live->prev = t;
    live = t;
    return t;
}

void ctx_switch_to(task_ctx *t)
{
    enum ctx_state state;
    need_scheduler("ctx_switch_to");
    lock();
    if (t->state != CTX_PARKED) {
        unlock();
        rt_stop(RT_STOP_STATUS, "tasks: ctx_switch_to a context that is not parked");
    }
    t->state = CTX_RUNNING;
    hand_to(t);
    wait_baton(&scheduler);
    state = t->state;
    unlock();
    if (state == CTX_ENDED)
        release(t);
}

void ctx_park(void)
{
    need_task("ctx_park");
    lock();
    self->state = CTX_PARKED;
    hand_to(&scheduler);
    wait_baton(self);
    if (self->retire)
        end_retired();
    unlock();
}

_Noreturn void ctx_end(void)
{
    need_task("ctx_end");
    lock();
    self->state = CTX_ENDED;
    hand_to(&scheduler);
    unlock();
    pthread_exit(NULL);
}

void ctx_retire(task_ctx *t)
{
    need_scheduler("ctx_retire");
    lock();
    if (t->state != CTX_PARKED) {
        unlock();
        rt_stop(RT_STOP_STATUS, "tasks: ctx_retire of a context that is not parked");
    }
    t->retire = 1;
    hand_to(t);
    wait_baton(&scheduler);
    unlock();
    release(t);
}

int ctx_is_scheduler(void)
{
    start();
    return self == &scheduler;
}

/* ===================================================================================== */
/* The game glue: GAME.EXE's task switch on the contexts above.                         */
/* ===================================================================================== */

/* docs/analysis/task-switch.md, "The mechanism (read)": the word holding the current task's
 * record (set by the scheduler's walk before it resumes a task), and the trampoline every
 * task's first resume enters (the creation routines plant it where the restore path loads
 * ra; it calls the task's function and then jumps to the exit, never returning). */
#define GAME_CURRENT_TASK 0x801A3118u
#define GAME_TRAMPOLINE 0x8014A930u

#define TASK_HOST_STACK (1u << 20)
#define TASKS_MAX 64 /* MAME saw at most 6 tasks alive at once */

/* The live contexts by guest record. Changed only by the context holding the baton. */
static struct {
    uint32_t record;
    task_ctx *ctx;
} task_map[TASKS_MAX];
static int n_tasks;
static uint8_t *glue_ram; /* the one guest state, for a new context's entry */
static cpu_t *glue_cpu;

static const char *const kind_names[] = {"resume", "yield", "exit"};

static _Noreturn void refuse(const cpu_t *c, int kind, uint32_t record, const char *fmt, ...)
    __attribute__((format(printf, 4, 5)));

static _Noreturn void refuse(const cpu_t *c, int kind, uint32_t record, const char *fmt, ...)
{
    char why[160];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(why, sizeof why, fmt, ap);
    va_end(ap);
    rt_stop(RT_STOP_STATUS, "rt_host_switch: %s %s (record 0x%08X, pc as last stored 0x%08X, "
            "exact only in a --trace-pc build)", kind_names[kind], why, (unsigned)record,
            (unsigned)c->pc);
}

static int map_find(uint32_t record, const task_ctx *t)
{
    for (int i = 0; i < n_tasks; i++)
        if (t ? task_map[i].ctx == t : task_map[i].record == record)
            return i;
    return -1;
}

static void map_drop(int i)
{
    task_map[i] = task_map[--n_tasks];
}

/* A new context's entry: the task's first run, from the trampoline. This thread's host FP
 * environment is not the guest's until it is applied. */
static void task_entry(void *arg)
{
    uint32_t record = (uint32_t)(uintptr_t)arg;
    rt_set_fcsr(glue_cpu, rt_get_fcsr(glue_cpu));
    rt_call(glue_ram, glue_cpu, GAME_TRAMPOLINE);
    rt_stop(RT_STOP_STATUS, "rt_host_switch: the task of record 0x%08X returned from the "
            "trampoline 0x%08X, which ends in a jump to the exit (pc as last stored 0x%08X, "
            "exact only in a --trace-pc build)", (unsigned)record, GAME_TRAMPOLINE,
            (unsigned)glue_cpu->pc);
}

void rt_host_switch(uint8_t *ram, cpu_t *c, int kind)
{
    uint32_t record = mem_r32(ram, c, GAME_CURRENT_TASK);
    int i;
    if (kind < RT_SWITCH_RESUME || kind > RT_SWITCH_EXIT)
        rt_stop(RT_STOP_STATUS, "rt_host_switch: no switch kind %d (pc as last stored 0x%08X, "
                "exact only in a --trace-pc build)", kind, (unsigned)c->pc);
    /* MAME: no yield, exit or resume ever runs inside an exception */
    if (m_status() & ST_EXL)
        refuse(c, kind, record, "inside an exception or interrupt handler (Status EXL set)");
    if (kind == RT_SWITCH_RESUME) {
        if (!ctx_is_scheduler())
            refuse(c, kind, record, "on a task's context");
        i = map_find(record, NULL);
        if (c->r[31] == GAME_TRAMPOLINE) {
            /* a first run; a context still held for the record is a task the game freed
             * while it was suspended, whose record it has reused */
            if (i >= 0) {
                ctx_retire(task_map[i].ctx);
                map_drop(i);
            }
            if (n_tasks == TASKS_MAX)
                refuse(c, kind, record, "needs a %dth task context, more than the %d the "
                       "runtime keeps", TASKS_MAX + 1, TASKS_MAX);
            glue_ram = ram;
            glue_cpu = c;
            i = n_tasks++;
            task_map[i].record = record;
            task_map[i].ctx = ctx_spawn(task_entry, (void *)(uintptr_t)record, TASK_HOST_STACK);
        } else if (i < 0) {
            refuse(c, kind, record, "of a record with no context, and ra 0x%08X is not the "
                   "trampoline 0x%08X", (unsigned)c->r[31], GAME_TRAMPOLINE);
        }
        ctx_switch_to(task_map[i].ctx); /* returns when the task yields or exits */
        rt_set_fcsr(c, rt_get_fcsr(c));
        return;
    }
    if (ctx_is_scheduler())
        refuse(c, kind, record, "on the scheduler's context");
    if (kind == RT_SWITCH_YIELD) {
        ctx_park(); /* returns when the scheduler resumes this task */
        rt_set_fcsr(c, rt_get_fcsr(c));
        return;
    }
    /* the exit: the context is this one, whatever record the current task word now holds
     * (the game's free makes the exiting task's predecessor current) */
    i = map_find(0, self);
    if (i >= 0)
        map_drop(i);
    ctx_end();
}

void tasks_reset(void)
{
    need_scheduler("tasks_reset");
    while (live)
        ctx_retire(live);
    n_tasks = 0;
}
