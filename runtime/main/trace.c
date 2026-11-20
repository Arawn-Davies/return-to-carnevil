/* trace.c: the observers of a run and its trace file (trace.h).
 *
 * The trace has one line per event, "TIME KIND FIELDS...", TIME the virtual time in ns since
 * GAME.EXE's entry (machine.h's clock), numbers in hexadecimal with 0x unless named decimal:
 *   T console FD N TEXT          a console write (slot 0x0C0 to con:), TEXT its N bytes,
 *                                printable ASCII as is, backslash and every other byte as
 *                                \xNN (the escaping of mame/m2-facts/console.txt)
 *   T open MODE FD NAME          an open (slot 0x088): a1 as the game passed it, v0, the name
 *                                as it was in RAM before the call (escaped the same way)
 *   T loader N SLOT IRQ A0 A1 A2 A3 SP V0
 *                                a loader call, written when it returns: N its number in call
 *                                order (decimal, from 1), IRQ 1 when made inside an
 *                                exception (Status EXL), a0 to a3 and sp at the call, v0 at
 *                                the return; a call that stops is not written (the stop is)
 *   T switch KIND RECORD FN      a host switch (resume, yield or exit): the current task word
 *                                0x801A3118 and the function at its record + 0x24
 *   T task create|exit RECORD FN a change of the game's task list (below)
 *   T interp ADDR                the interpreter entered at ADDR (rt_log_unrecompiled)
 *   T pending N SLOT IRQ A0 A1 A2 A3 SP
 *                                at a stop, each loader call still running, outermost first
 *   T stop STATUS MESSAGE        rt_stop (STATUS decimal)
 *   T end HOW                    the run reached its limit: "seconds N" or "frames N"
 * and at the end of every run, from trace_close (values decimal):
 *   T count vblank N             VBLANK interrupts raised (irq.c, vblank_raise)
 *   T count spin_max N           the largest VBLANK-wait spin count (below)
 *   T count tasks_max N          the most tasks on the game's list at once
 *   T count loader_calls N
 *   T count voodoo_reg REG N     writes per Voodoo register number by the standard map
 *   T count voodoo_alt REG N     ... through the alternate map (both only where N > 0)
 *   T count voodoo_tex N         texture memory writes
 *   T count dropped I N WHY      draws dropped for reason I (hw.h, DROP_*), every reason
 *   T count sndfifo N            sound FIFO writes
 *   T count dcs_data N           words the sound board took as block data
 *   T alive RECORD FN            each task on the game's list at the end
 *
 * The task list (docs/analysis/task-switch.md, "The mechanism"): records linked from the head
 * word 0x801A3110 through +0, the task function at +0x24. It is read at every host switch and
 * at the end; a record that appears is a creation, one that goes is an exit. The game links
 * a new record at the head, so the creations found at one reading are written oldest first;
 * an exit is written before the creations, unless a new record is not one that went (an
 * exiting task creates nothing after its exit, but a record freed by an exit is reused only
 * by a later creation). MAME logs the same events at the creation routine's return and at
 * the exit (mame/m2-facts/tasks.txt), so the two orders agree while no creation and exit of
 * different tasks fall between two switches out of their order.
 *
 * The spin count: the scheduler's wait counts its spins in 0x801A36CC and resumes the first
 * due task right after the wait (task-switch.md, "The wait at the start of a pass"); the
 * word read at each resume is the count of the wait just ended.
 */
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../core/tasks.h"
#include "../disk/disk.h"
#include "../hw/hw.h"
#include "../loader/loader.h"
#include "machine.h"
#include "trace.h"

#define TASK_HEAD 0x801A3110u
#define TASK_CURRENT 0x801A3118u
#define TASK_FN 0x24u
#define SPIN_COUNT 0x801A36CCu
#define TASKS_SEEN 64
#define CALL_DEPTH 64
#define NAME_MAX_BYTES 64

static FILE *out;
static uint8_t *g_ram;
static cpu_t *g_cpu;
static uint64_t frame_limit;

static uint64_t vblanks, spin_max, loader_calls;
static int tasks_max;

/* --- helpers ------------------------------------------------------------------------------ */

/* The time now, without the check vclock_now makes (a broken slice there stops, and this is
 * called from stop functions too). */
static uint64_t now(void)
{
    int64_t used = (int64_t)machine.slice_loaded - (int64_t)g_cpu->slice;
    return machine.now + (used > 0 ? (uint64_t)used * VCLOCK_NS_PER_CHECK : 0);
}

static int ram_word(uint32_t addr, uint32_t *v)
{
    if ((addr & 3u) || !mem_is_ram(addr, 4))
        return 0;
    *v = mem_r32(g_ram, g_cpu, addr);
    return 1;
}

/* The bytes as console.txt escapes them. */
static void put_escaped(const uint8_t *bytes, uint32_t n)
{
    for (uint32_t i = 0; i < n; i++) {
        uint8_t b = bytes[i];
        if (b >= 0x20 && b <= 0x7E && b != '\\')
            fputc(b, out);
        else
            fprintf(out, "\\x%02X", b);
    }
}

static void line_head(const char *kind)
{
    fprintf(out, "%" PRIu64 " %s", now(), kind);
}

/* --- the console and the loader --------------------------------------------------------- */

static void on_console(int fd, const uint8_t *bytes, uint32_t n)
{
    if (!out)
        return;
    line_head("console");
    fprintf(out, " 0x%X 0x%" PRIX32 " ", (unsigned)fd, n);
    put_escaped(bytes, n);
    fputc('\n', out);
}

static struct call {
    uint64_t n;
    uint32_t slot, irq, a[4], sp;
    uint8_t name[NAME_MAX_BYTES];
    uint32_t name_len;
} calls[CALL_DEPTH];
static int depth;

static void on_call(cpu_t *c, uint32_t slot, int after)
{
    struct call *k;
    if (!after) {
        loader_calls++;
        if (depth < CALL_DEPTH) {
            k = &calls[depth];
            *k = (struct call){.n = loader_calls, .slot = slot,
                               .irq = (m_status() & ST_EXL) != 0, .sp = c->r[29]};
            memcpy(k->a, &c->r[4], sizeof k->a);
            if (slot == 0x088) /* the name before the stand-in upper-cases it */
                for (uint32_t i = 0; i < NAME_MAX_BYTES && mem_is_ram(c->r[4] + i, 1); i++) {
                    uint8_t b = (uint8_t)mem_r8(g_ram, c, c->r[4] + i);
                    if (!b)
                        break;
                    k->name[k->name_len++] = b;
                }
        }
        depth++;
        return;
    }
    depth--;
    if (!out || depth >= CALL_DEPTH)
        return; /* deeper than kept: not written (the game nests at most twice) */
    k = &calls[depth];
    line_head("loader");
    fprintf(out, " %" PRIu64 " 0x%03" PRIX32 " %" PRIu32 " 0x%08" PRIX32 " 0x%08" PRIX32
            " 0x%08" PRIX32 " 0x%08" PRIX32 " 0x%08" PRIX32 " 0x%08" PRIX32 "\n", k->n, k->slot,
            k->irq, k->a[0], k->a[1], k->a[2], k->a[3], k->sp, c->r[2]);
    if (slot == 0x088) {
        line_head("open");
        fprintf(out, " 0x%08" PRIX32 " 0x%08" PRIX32 " ", k->a[1], c->r[2]);
        put_escaped(k->name, k->name_len);
        fputc('\n', out);
    }
}

/* --- the tasks ---------------------------------------------------------------------------- */

static struct task {
    uint32_t record, fn;
} seen[TASKS_SEEN];
static int n_seen;

static int read_list(struct task *list)
{
    uint32_t rec;
    int n = 0;
    if (!ram_word(TASK_HEAD, &rec))
        return 0;
    while (rec && n < TASKS_SEEN) {
        uint32_t fn = 0, next;
        ram_word(rec + TASK_FN, &fn);
        list[n++] = (struct task){rec, fn};
        if (!ram_word(rec, &next))
            break;
        rec = next;
    }
    return n;
}

static int find(const struct task *list, int n, struct task t, int by_record)
{
    for (int i = 0; i < n; i++)
        if (list[i].record == t.record && (by_record || list[i].fn == t.fn))
            return i;
    return -1;
}

static void task_line(const char *event, struct task t)
{
    if (!out)
        return;
    line_head("task");
    fprintf(out, " %s 0x%08" PRIX32 " 0x%08" PRIX32 "\n", event, t.record, t.fn);
}

static void scan_tasks(void)
{
    struct task list[TASKS_SEEN];
    int n = read_list(list), gone[TASKS_SEEN] = {0};
    if (n > tasks_max)
        tasks_max = n;
    for (int i = 0; i < n_seen; i++)
        gone[i] = find(list, n, seen[i], 0) < 0;
    /* exits whose record a creation now reuses, then the creations oldest first, then the
     * other exits */
    for (int i = 0; i < n_seen; i++)
        if (gone[i] && find(list, n, seen[i], 1) >= 0) {
            task_line("exit", seen[i]);
            gone[i] = 0;
        }
    for (int i = n - 1; i >= 0; i--)
        if (find(seen, n_seen, list[i], 0) < 0)
            task_line("create", list[i]);
    for (int i = 0; i < n_seen; i++)
        if (gone[i])
            task_line("exit", seen[i]);
    memcpy(seen, list, sizeof list[0] * (size_t)n);
    n_seen = n;
}

static void on_switch(uint8_t *ram, cpu_t *c, int kind)
{
    static const char *const names[] = {"resume", "yield", "exit"};
    uint32_t record = 0, fn = 0, spin;
    (void)ram, (void)c;
    scan_tasks();
    if (kind == RT_SWITCH_RESUME && ram_word(SPIN_COUNT, &spin) && spin > spin_max)
        spin_max = spin;
    if (!out)
        return;
    if (ram_word(TASK_CURRENT, &record))
        ram_word(record + TASK_FN, &fn);
    line_head("switch");
    fprintf(out, " %s 0x%08" PRIX32 " 0x%08" PRIX32 "\n",
            kind >= 0 && kind <= 2 ? names[kind] : "?", record, fn);
}

/* --- the rest ------------------------------------------------------------------------------ */

static void on_interp(uint32_t addr)
{
    if (!out)
        return;
    line_head("interp");
    fprintf(out, " 0x%08" PRIX32 "\n", addr);
}

static void on_stop(int status, const char *message)
{
    if (!out)
        return;
    for (int i = 0; i < depth && i < CALL_DEPTH; i++) {
        const struct call *k = &calls[i];
        line_head("pending");
        fprintf(out, " %" PRIu64 " 0x%03" PRIX32 " %" PRIu32 " 0x%08" PRIX32 " 0x%08" PRIX32
                " 0x%08" PRIX32 " 0x%08" PRIX32 " 0x%08" PRIX32 "\n", k->n, k->slot, k->irq,
                k->a[0], k->a[1], k->a[2], k->a[3], k->sp);
    }
    line_head("stop");
    fprintf(out, " %d %s\n", status, message);
}

static void on_vblank(void)
{
    char how[48];
    vblanks++;
    if (frame_limit && vblanks == frame_limit) {
        snprintf(how, sizeof how, "frames %" PRIu64, frame_limit);
        trace_end(how);
    }
}

static int end_quiet;

_Noreturn void trace_end(const char *how)
{
    if (out) {
        line_head("end");
        fprintf(out, " %s\n", how);
    }
    if (!end_quiet) {
        uint8_t d[32];
        sha256(g_ram, RAM_SIZE, d);
        fprintf(stderr, "carnevil: ran %" PRIu64 " ns of guest time; RAM SHA-256 ", now());
        for (int i = 0; i < 32; i++)
            fprintf(stderr, "%02x", d[i]);
        fputc('\n', stderr);
    }
    rt_exit(0);
}

void trace_close(void)
{
    if (!out)
        return;
    scan_tasks();
    line_head("count");
    fprintf(out, " vblank %" PRIu64 "\n", vblanks);
    line_head("count");
    fprintf(out, " spin_max %" PRIu64 "\n", spin_max);
    line_head("count");
    fprintf(out, " tasks_max %d\n", tasks_max);
    line_head("count");
    fprintf(out, " loader_calls %" PRIu64 "\n", loader_calls);
    for (uint32_t r = 0; r < NREGS_VOODOO; r++) {
        if (voodoo_reg_writes(r)) {
            line_head("count");
            fprintf(out, " voodoo_reg 0x%02" PRIX32 " %" PRIu32 "\n", r, voodoo_reg_writes(r));
        }
        if (voodoo_alt_writes(r)) {
            line_head("count");
            fprintf(out, " voodoo_alt 0x%02" PRIX32 " %" PRIu32 "\n", r, voodoo_alt_writes(r));
        }
    }
    line_head("count");
    fprintf(out, " voodoo_tex %" PRIu64 "\n", voodoo_tex_writes());
    for (int i = 0; i < DROP_COUNT; i++) {
        line_head("count");
        fprintf(out, " dropped %d %" PRIu32 " %s\n", i, vfb_dropped(i), vfb_drop_name(i));
    }
    line_head("count");
    fprintf(out, " sndfifo %" PRIu64 "\n", sndfifo_writes());
    line_head("count");
    fprintf(out, " dcs_data %" PRIu64 "\n", dcs_data_words());
    for (int i = n_seen - 1; i >= 0; i--) {
        line_head("alive");
        fprintf(out, " 0x%08" PRIX32 " 0x%08" PRIX32 "\n", seen[i].record, seen[i].fn);
    }
    fclose(out);
    out = NULL;
}

int trace_open(const char *path, char *err, size_t errlen)
{
    out = fopen(path, "w");
    if (!out) {
        snprintf(err, errlen, "cannot write the trace %s: %s", path, strerror(errno));
        return -1;
    }
    setvbuf(out, NULL, _IOFBF, 1u << 20);
    return 0;
}

void trace_attach(uint8_t *ram, cpu_t *c, uint64_t frames, int quiet)
{
    g_ram = ram;
    g_cpu = c;
    frame_limit = frames;
    end_quiet = quiet;
    loader_set_console_hook(on_console);
    loader_set_call_hook(on_call);
    tasks_set_switch_hook(on_switch);
    rt_log_unrecompiled = on_interp;
    rt_set_stop_hook(on_stop);
    irq_set_vblank_hook(on_vblank);
    rt_at_stop(trace_close);
}
