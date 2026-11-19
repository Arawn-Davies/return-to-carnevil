/* tasks.h: host contexts, one per cooperative task, so that a guest stack switch can switch
 * the host's C stack with it.
 *
 * Recompiled code keeps the game's call chain on the host C stack: when the game's scheduler
 * swaps to a task's guest stack and jumps into it, the host must run that task's C frames,
 * and when the task yields, the scheduler's. A context is a host thread with its own stack;
 * exactly one context runs at any time, and control passes between them only through the
 * calls below, each a complete synchronous hand-over. Nothing here knows the game: the game
 * glue that maps the task switch onto these calls (rt_host_switch) is in tasks.c below its
 * banner.
 *
 * The scheduler's context is the thread that first calls into tasks.c (the program's main
 * thread). A context other than the scheduler's is a task context. Every function stops the
 * program (rt_stop, status RT_STOP_STATUS) when called on the wrong context or when the
 * host cannot create or end a thread; none of them returns an error.
 *
 * Build with -pthread.
 */
#ifndef TASKS_H
#define TASKS_H

#include <stddef.h>

typedef struct task_ctx task_ctx;

/* ctx_spawn, on the scheduler's context: a new context that will run entry(arg) on a host
 * stack of host_stack bytes. It starts parked: entry does not run until the first
 * ctx_switch_to. Returning from entry ends the context, as ctx_end does. */
task_ctx *ctx_spawn(void (*entry)(void *), void *arg, size_t host_stack);

/* ctx_switch_to, on the scheduler's context: runs t (parked) until it parks or ends, then
 * returns. When t has ended, it is released before ctx_switch_to returns, and the caller must
 * not use t again. */
void ctx_switch_to(task_ctx *t);

/* ctx_park, on a task context: hands back to the scheduler; returns when the scheduler
 * switches to this context again. A context retired while parked never returns from it. */
void ctx_park(void);

/* ctx_end, on a task context: ends it and hands back to the scheduler, whose ctx_switch_to
 * then returns. */
_Noreturn void ctx_end(void);

/* ctx_retire, on the scheduler's context: ends t (parked, or never run) without running any
 * more of its entry function, and releases it; t must not be used again. */
void ctx_retire(task_ctx *t);

/* ctx_is_scheduler: 1 on the scheduler's context, 0 on a task context. */
int ctx_is_scheduler(void);

/* tasks_reset, on the scheduler's context: retires every live context and forgets the game
 * glue's map from task records to contexts, so that nothing of a previous run is left (for
 * test harnesses that run independent cases in one process). */
void tasks_reset(void);

#endif
