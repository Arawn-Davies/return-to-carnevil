/* carnevil.c: the program. GAME.EXE, recompiled, run headless on the runtime from the user's
 * own disk image.
 *
 *   carnevil --disk FILE [--overlay FILE] [--nvram FILE] [--seconds N | --frames N]
 *            [--trace FILE] [--quiet]
 *
 * --disk is the raw image of the game's hard disk (the user's carnevil.raw), opened
 * read-only; every write the game makes goes to the overlay (runtime/disk/image.c gives its
 * format) and the CMOS to the NVRAM file, both by default next to the raw image as
 * carnevil.overlay and carnevil.nvram, created when absent. --seconds N and --frames N end
 * the run after N seconds of virtual time (from GAME.EXE's entry) or at the N-th VBLANK
 * interrupt; without either the game runs until it stops. --trace writes the trace
 * (runtime/main/trace.c lists its lines); --quiet sends the game's console writes only to
 * the trace and leaves out the closing line on stderr.
 *
 * Exit status: 0 when the requested time has run, 2 for a usage or file error (one line on
 * stderr naming the file), 3 for a stop of the runtime (rt_stop: "carnevil: stop: ..." names
 * what was not modelled, or the host file that could not be written), 130 when SIGINT,
 * SIGTERM or SIGHUP ended the run. A signal only sets a flag; the run then ends at the next
 * check point through the same path as a run that reached its limit (trace_interrupted): the
 * NVRAM is saved, the overlay flushed and the trace's totals written and the file closed,
 * after an "end interrupted SIGNAME" line and, unless --quiet, "carnevil: interrupted by
 * SIGNAME at N ns of guest time" on stderr. SIGKILL cannot be caught: what survives it is in
 * docs/getting-started.md.
 *
 * Start-up, in this order:
 * 1. The options, then the four resets, each once: machine_reset first (the clock, its event
 *    queue and the at-stop list, which the later steps fill), hw_reset (the devices; the
 *    Voodoo's reset cancels its own clock event, so it comes after the queue is emptied),
 *    loader_reset (the stand-ins: descriptors 0 to 5 open, the measured input ports, no
 *    hooks), tasks_reset (which makes this thread the scheduler's context).
 * 2. The files: the raw image must be a regular file of the supported disk's size; GAME.EXE
 *    is read from its partition 1 through the file system, without the overlay, and its
 *    SHA-256 must be the one the generated code was made from (recomp_exe_sha256, written
 *    into func_table.c from manifest.toml). Only then are the overlay, the NVRAM file and the
 *    trace opened or created, and GAME.EXE is read again through the overlay (what the game's
 *    disk now holds) and checked again.
 * 3. RAM: all zero, then the image placed at its load address as docs/analysis/exe-format.md
 *    says (load_image, the one place that writes RAM directly). Nothing is placed below it:
 *    the stand-ins own their areas (the argument block, the partition-table copy) and write
 *    them when called (boot-handoff.md, "Decisions for the runtime", item 4).
 * 4. The entry state from boot_state.h (config/boot_state.toml): the general registers, hi,
 *    lo, FCSR and Status; slice 0, so the first check point enters rt_tick and the clock
 *    starts from it.
 * 5. The observers (trace.c), the at-stop functions (the CMOS save, registered by cmos_load,
 *    the overlay's flush, the trace), the end of the run as a clock event, and the call of
 *    the entry, which does not return: `_start` jumps to main, which runs the scheduler.
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "../core/tasks.h"
#include "../disk/disk.h"
#include "../hw/hw.h"
#include "../loader/loader.h"
#include "boot_state.h"
#include "machine.h"
#include "recomp.h"
#include "trace.h"

/* The supported disk: 0x0080730C sectors of 512 bytes, the drive size word the loader holds
 * (measured: loader-services.md section 7) and the size of the user's image of it. */
#define DISK_BYTES 4310046720ull
#define GAME_EXE "GAME.EXE"
#define EXE_HEADER 8u
#define NS_PER_SECOND 1000000000ull
/* The largest --seconds (final review M3): every time base of the runtime must hold the end
 * of the run. The narrowest is the Voodoo's, picoseconds since power-on in 64 bits (hw.h,
 * voodoo_null.c: virtual ns x 1000 + VOODOO_ENTRY_PS), which wraps after 18,446,744 s, about
 * 213.5 days; the virtual clock's nanoseconds wrap after about 584 years. 18,000,000 s
 * (about 208 days) leaves the card more than 400,000 s for its own times past the end (a
 * retrace or a command end comes at most a frame or a few milliseconds later). */
#define MAX_SECONDS 18000000ull
#define EXIT_FILES 2

static const char USAGE[] = "usage: carnevil --disk FILE [--overlay FILE] [--nvram FILE] "
                            "[--seconds N | --frames N] [--trace FILE] [--quiet]; exit status "
                            "0 at the limit, 2 for a usage or file error, 3 for a stop, 130 "
                            "when interrupted by a signal";

static struct {
    const char *disk, *overlay, *nvram, *trace;
    uint64_t seconds, frames;
    int quiet;
} opt;

static uint8_t *ram;
static cpu_t cpu;

/* One line on stderr, exit 2: a usage or file error. */
static _Noreturn void refuse(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

static _Noreturn void refuse(const char *fmt, ...)
{
    va_list ap;
    fflush(stdout);
    fputs("carnevil: ", stderr);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    exit(EXIT_FILES);
}

/* --- options ------------------------------------------------------------------------------ */

static uint64_t count_arg(const char *name, const char *text, uint64_t max)
{
    char *end;
    unsigned long long v;
    errno = 0;
    v = text[0] >= '0' && text[0] <= '9' ? strtoull(text, &end, 10) : 0;
    if (v == 0 || errno || *end != '\0' || v > max)
        refuse("%s %s: not a whole number from 1 to %" PRIu64 " (%s)", name, text, max, USAGE);
    return v;
}

/* path's folder joined with name: the default overlay and NVRAM paths. */
static char *beside(const char *path, const char *name)
{
    const char *slash = strrchr(path, '/');
    size_t dir = slash ? (size_t)(slash - path) + 1 : 0;
    char *p = malloc(dir + strlen(name) + 1);
    if (!p)
        refuse("out of memory");
    memcpy(p, path, dir);
    strcpy(p + dir, name);
    return p;
}

static void parse(int argc, char **argv)
{
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char **value = strcmp(a, "--disk") == 0      ? &opt.disk
                             : strcmp(a, "--overlay") == 0 ? &opt.overlay
                             : strcmp(a, "--nvram") == 0   ? &opt.nvram
                             : strcmp(a, "--trace") == 0   ? &opt.trace
                                                           : NULL;
        if (strcmp(a, "--quiet") == 0) {
            opt.quiet = 1;
            continue;
        }
        if (!value && strcmp(a, "--seconds") != 0 && strcmp(a, "--frames") != 0)
            refuse("%s: not an option (%s)", a, USAGE);
        if (i + 1 == argc)
            refuse("%s needs a value (%s)", a, USAGE);
        if (value) {
            *value = argv[++i];
        } else if (strcmp(a, "--seconds") == 0) {
            opt.seconds = count_arg(a, argv[++i], MAX_SECONDS);
        } else {
            opt.frames = count_arg(a, argv[++i], UINT32_MAX);
        }
    }
    if (!opt.disk)
        refuse("no --disk (%s)", USAGE);
    if (opt.seconds && opt.frames)
        refuse("--seconds and --frames together (%s)", USAGE);
    if (!opt.overlay)
        opt.overlay = beside(opt.disk, "carnevil.overlay");
    if (!opt.nvram)
        opt.nvram = beside(opt.disk, "carnevil.nvram");
}

/* --- the files ------------------------------------------------------------------------------ */

static void check_disk(void)
{
    struct stat st;
    if (stat(opt.disk, &st) != 0)
        refuse("%s: %s", opt.disk, strerror(errno));
    if (!S_ISREG(st.st_mode))
        refuse("%s: not a regular file", opt.disk);
    if ((uint64_t)st.st_size != DISK_BYTES)
        refuse("%s: %" PRIu64 " bytes, not the %llu of the supported disk image", opt.disk,
               (uint64_t)st.st_size, DISK_BYTES);
}

/* The NVRAM file must be the CMOS's size if it exists, and its folder must exist and be
 * writable either way: the file is created there on a first run and replaced through a
 * temporary file and a rename at every save (dev_cmos.c), so a folder the program cannot
 * write would otherwise fail only at the end of the run (final review M1). cmos_load's own
 * refusals are stops, and these are the user's files, so they are checked here, before any
 * file is created. */
static void check_nvram(void)
{
    struct stat st;
    char *dir = beside(opt.nvram, ".");
    if (stat(opt.nvram, &st) == 0) {
        if (!S_ISREG(st.st_mode))
            refuse("%s: not a regular file, so not an NVRAM file", opt.nvram);
        if ((uint64_t)st.st_size != CMOS_SIZE)
            refuse("%s: %" PRIu64 " bytes, not the CMOS's %u", opt.nvram, (uint64_t)st.st_size,
                   CMOS_SIZE);
    } else if (errno != ENOENT) {
        refuse("%s: %s", opt.nvram, strerror(errno));
    }
    if (stat(dir, &st) != 0 || !S_ISDIR(st.st_mode))
        refuse("%s: its folder does not exist", opt.nvram);
    if (access(dir, W_OK | X_OK) != 0)
        refuse("%s: cannot write its folder (%s), where the NVRAM is saved", opt.nvram,
               strerror(errno));
    free(dir);
}

static void hex(const uint8_t d[32], char out[65])
{
    for (int i = 0; i < 32; i++)
        snprintf(out + 2 * i, 3, "%02x", d[i]);
}

/* GAME.EXE from the open image's file system, checked against recomp_exe_sha256; its size in
 * *size. */
static uint8_t *read_game_exe(uint32_t *size, const char *through)
{
    struct fs_stat st;
    uint8_t digest[32], *data;
    char got[65];
    int h, n;
    if (fs_find(GAME_EXE, &st) != 0)
        refuse("%s: no %s on the disk's partition 1%s", opt.disk, GAME_EXE, through);
    if (st.size < EXE_HEADER || st.size > EXE_HEADER + RAM_SIZE || st.size % 4)
        refuse("%s: %s is %" PRIu32 " bytes, not an EXE that fits RAM%s", opt.disk, GAME_EXE,
               st.size, through);
    data = malloc(st.size);
    if (!data)
        refuse("out of memory");
    h = fs_open(GAME_EXE, 0);
    n = h < 0 ? -1 : fs_read(h, data, st.size);
    if (h >= 0)
        fs_close(h);
    if (n != (int)st.size)
        refuse("%s: cannot read %s from the disk's file system%s", opt.disk, GAME_EXE, through);
    sha256(data, st.size, digest);
    hex(digest, got);
    if (strcmp(got, recomp_exe_sha256) != 0)
        refuse("%s: %s has SHA-256 %s%s; this program was generated from the one with SHA-256 "
               "%s", opt.disk, GAME_EXE, got, through, recomp_exe_sha256);
    *size = st.size;
    return data;
}

/* --- RAM and the entry state ----------------------------------------------------------- */

static uint32_t word_at(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

/* exe-format.md: header word 1 is the load address (and the entry), the image is file bytes
 * 8 on, copied to it. The only code that fills guest RAM directly. */
static void load_image(const uint8_t *exe, uint32_t size)
{
    uint32_t load = word_at(exe + 4), n = size - EXE_HEADER;
    if (load != BOOT_ENTRY)
        refuse("%s: %s loads at 0x%08" PRIX32 ", not at the entry 0x%08X of "
               "config/boot_state.toml", opt.disk, GAME_EXE, load, BOOT_ENTRY);
    if (!mem_is_ram(load, n) || load < 0x80000000u)
        refuse("%s: %s's image 0x%08" PRIX32 " + 0x%" PRIX32 " is not inside kseg0 RAM",
               opt.disk, GAME_EXE, load, n);
    memcpy(ram + (load & 0x1FFFFFFFu), exe + EXE_HEADER, n);
}

static void entry_state(void)
{
    static const uint32_t gpr[32] = BOOT_GPR;
    memset(&cpu, 0, sizeof cpu);
    memcpy(cpu.r, gpr, sizeof cpu.r);
    cpu.r[0] = 0;
    cpu.hi = BOOT_HI;
    cpu.lo = BOOT_LO;
    cpu.pc = BOOT_ENTRY;
    cpu.slice = 0; /* the first check point ticks (machine_reset left slice_loaded at 0) */
    rt_set_fcsr(&cpu, BOOT_FCSR);
    m_status_set(BOOT_STATUS, 0xFFFFFFFFu);
}

static void host_io_failed(const char *message)
{
    rt_stop(RT_STOP_STATUS, "%s", message);
}

static void time_up(void *arg)
{
    char how[48];
    (void)arg;
    snprintf(how, sizeof how, "seconds %" PRIu64, opt.seconds);
    trace_end(how);
}

int main(int argc, char **argv)
{
    char err[1024];
    uint8_t *exe;
    uint32_t size;

    parse(argc, argv);
    machine_reset();
    hw_reset();
    loader_reset();
    tasks_reset();
    /* from here a signal only sets a flag; the first check point after it ends the run */
    rt_catch_signals(trace_interrupted);

    check_disk();
    check_nvram();
    if (img_open(opt.disk, NULL, err, sizeof err) != 0)
        refuse("%s", err);
    free(read_game_exe(&size, ""));
    if (opt.trace && trace_open(opt.trace, err, sizeof err) != 0)
        refuse("%s", err);
    if (img_open(opt.disk, opt.overlay, err, sizeof err) != 0)
        refuse("%s", err);
    exe = read_game_exe(&size, " (through the overlay)");
    cmos_load(opt.nvram);
    rt_at_stop(img_flush);
    /* from here a host I/O error on the image or the overlay stops, naming the file: it is
     * not the game's disk error, so it must not reach the game as the loader's -1 */
    img_set_host_error(host_io_failed);

    ram = calloc(1, RAM_SIZE);
    if (!ram)
        refuse("out of memory for the guest's RAM");
    load_image(exe, size);
    free(exe);
    entry_state();

    loader_set_console_echo(!opt.quiet);
    trace_attach(ram, &cpu, opt.frames, opt.quiet);
    if (opt.seconds)
        vclock_at(opt.seconds * NS_PER_SECOND, time_up, NULL);

    rt_call(ram, &cpu, BOOT_ENTRY);
    rt_stop(RT_STOP_STATUS, "GAME.EXE's entry 0x%08X returned (pc as last stored 0x%08" PRIX32
            "): `_start` ends in a jump to main, whose scheduler never returns", BOOT_ENTRY,
            cpu.pc);
}
