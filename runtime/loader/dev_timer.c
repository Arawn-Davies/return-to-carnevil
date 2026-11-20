/* dev_timer.c: the GT64010's timers 1 to 3, `timer:` (descriptor 5 at the hand-off).
 *
 * docs/analysis/loader-services.md section 3 (`timer:`): 24-bit down-counters at 20 ns a
 * count. Start with arg 0 is a one-shot of 0xFFFFFF counts that stays at 0 once run out;
 * start with arg n is timer mode, reloading every n counts. An elapsed read stores (loaded
 * count - current count) x 20, the nanoseconds since the start: saturated at exactly
 * 0x13FFFFEC for a run-out one-shot, wrapping with the period in timer mode. Here the
 * counters run on the virtual clock (vclock_now), all in 64 bits up to the final 32-bit
 * value. Timer 0 is the loader's own 100 ms tick and is not the game's (section 7). Open,
 * close, read and write are the shared return-0 routine (section 2: "only ioctl").
 */
#include "loader.h"
#include "machine.h"

#define NS_PER_COUNT 20u
#define ONE_SHOT_COUNTS 0xFFFFFFu /* also the largest count the 24-bit counter loads */

static struct {
    int running;
    uint32_t period; /* counts; 0 for the one-shot */
    uint64_t start;  /* vclock_now at the start, ns */
} timers[4];

void ld_timer_reset(void)
{
    for (int i = 0; i < 4; i++)
        timers[i].running = 0, timers[i].period = 0, timers[i].start = 0;
}

/* The elapsed value of timer n (section 3, 0x7405 to 0x7407). */
static uint32_t elapsed(int n)
{
    uint64_t counts = (vclock_now(ld.c) - timers[n].start) / NS_PER_COUNT;
    if (timers[n].period == 0)
        counts = counts < ONE_SHOT_COUNTS ? counts : ONE_SHOT_COUNTS;
    else
        counts %= timers[n].period;
    return (uint32_t)(counts * NS_PER_COUNT);
}

/* ioctl: section 3, `timer:` */
static int timer_ioctl(int fd, uint32_t cmd, uint32_t arg)
{
    if (cmd >= 0x7401 && cmd <= 0x7403) { /* start timer 1 to 3 */
        int n = (int)(cmd - 0x7400);
        if (arg > ONE_SHOT_COUNTS)
            ld_stop("ioctl 0x%04X: timer %d started with count 0x%X, more than its 24-bit "
                    "counter holds", (unsigned)cmd, n, (unsigned)arg);
        timers[n].running = 1;
        timers[n].period = arg;
        timers[n].start = vclock_now(ld.c);
        return 0;
    }
    if (cmd >= 0x7405 && cmd <= 0x7407) { /* elapsed of timer 1 to 3; v1 = the value too */
        int n = (int)(cmd - 0x7404);
        uint32_t v;
        if (!timers[n].running)
            ld_stop("ioctl 0x%04X: timer %d is not running (never started, or stopped), and "
                    "what its elapsed read gives then is not documented", (unsigned)cmd, n);
        v = elapsed(n);
        ld_put32(arg, v);
        ld.c->r[3] = v;
        return 0;
    }
    if (cmd >= 0x7409 && cmd <= 0x740B) { /* stop timer 1 to 3 */
        timers[cmd - 0x7408].running = 0;
        return 0;
    }
    ld_bad_ioctl(fd, cmd, arg); /* 0x7400, 0x7404, 0x7408: timer 0, not issued */
}

const struct ldev ld_timer = {"timer:", ld_ret0_open, ld_ret0_close, ld_ret0_rw, ld_ret0_rw,
                              timer_ioctl};
