/* replay.c: run one recompiled function from a state MAME captured, and compare the result
 * with MAME's.
 *
 * Usage: replay [--interp] CAPTURE_DIR. A capture (tools/mame/funcreplay.lua; the format is in
 * tools/mame/README.md) is a folder holding capture.txt (the function's address, the base
 * RAM image it is relative to, and the registers before and after one call), before.diff (the
 * RAM at the call, against the base) and after.diff (the RAM at the return, against the RAM
 * at the call).
 *
 * The program loads the before-state, calls the function through recomp_lookup (it must be
 * recompiled) with no interrupt delivered (rt_tick only reloads slice), and compares every
 * general register, hi, lo, the 32 FP halves, FCSR with its condition codes, and all 8 MB of
 * RAM with the after-state. It
 * prints each difference (registers by name with both values; RAM as address ranges and byte
 * counts only) and exits 1 on any. An rt_loader, rt_break, rt_host_switch, rt_fp_trap or
 * interpreter entry during the call is printed and is a difference too.
 *
 * The first bus access ends the run instead: the program prints "bus read ADDR size N" or
 * "bus write ADDR size N value V", the state at that moment (common.h, print_state: before
 * the access takes effect) and "ram fnv1a64 H", a 64-bit FNV-1a hash of the 8 MB of RAM then,
 * and exits 3. Whether ADDR is hardware (the function is impure at run time) or a wrong address
 * is for the caller to decide (test_against_mame classifies it with carnevil_tools.board).
 *
 * --interp: the same, but every call, the function's own included, runs in the reference
 * interpreter (this file provides rt_call and rt_jump for that, in place of runtime/core's
 * call.c); interpreter entries are then expected and not reported. For comparing the first
 * bus access of the two modes.
 *
 * Exit 2: a usage or capture error.
 */
#include "common.h"

#define MAX_RANGES 16 /* RAM ranges printed */
#define MERGE 16      /* differing bytes this close are one range */
#define KSEG0 0x80000000u
#define EXIT_BUS 3

static unsigned long events; /* runtime calls a pure function must not make */
static int interp_only;
static const uint8_t *run_ram; /* the RAM of the run, for the hash at the first bus access */

static uint64_t fnv1a64(const uint8_t *p, size_t n)
{
    uint64_t h = 0xCBF29CE484222325ull;
    for (size_t i = 0; i < n; i++)
        h = (h ^ p[i]) * 0x100000001B3ull;
    return h;
}

void (*rt_log_unrecompiled)(uint32_t addr) = 0;

void rt_call(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    recomp_fn fn = interp_only ? NULL : recomp_lookup(addr);
    if (fn)
        fn(ram, c);
    else
        interp_run(ram, c, addr);
}

void rt_jump(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    rt_call(ram, c, addr);
}

static _Noreturn void first_bus_access(const cpu_t *c, const char *dir, uint32_t addr, int size,
                                       const uint32_t *value)
{
    printf("bus %s 0x%08" PRIX32 " size %d", dir, addr, size);
    if (value)
        printf(" value 0x%08" PRIX32, *value);
    printf("\n");
    print_state(stdout, c);
    printf("ram fnv1a64 %016" PRIX64 "\n", fnv1a64(run_ram, RAM_SIZE));
    fflush(stdout);
    exit(EXIT_BUS);
}

uint32_t bus_read(cpu_t *c, uint32_t addr, int size)
{
    first_bus_access(c, "read", addr, size, NULL);
}

void bus_write(cpu_t *c, uint32_t addr, int size, uint32_t value)
{
    first_bus_access(c, "write", addr, size, &value);
}

void rt_loader(uint8_t *ram, cpu_t *c, uint32_t slot)
{
    (void)ram;
    (void)c;
    events++;
    printf("rt_loader slot 0x%03" PRIX32 "\n", slot);
}

void rt_break(uint8_t *ram, cpu_t *c, uint32_t pc, uint32_t code)
{
    (void)ram;
    (void)c;
    events++;
    printf("rt_break pc 0x%08" PRIX32 " code 0x%05" PRIX32 "\n", pc, code);
}

void rt_tick(uint8_t *ram, cpu_t *c)
{
    (void)ram;
    c->slice = STUB_SLICE;
}

void rt_host_switch(uint8_t *ram, cpu_t *c, int kind)
{
    (void)ram;
    events++;
    printf("rt_host_switch kind %d sp 0x%08" PRIX32 "\n", kind, c->r[29]);
}

void rt_fp_trap(cpu_t *c, uint32_t cause_bit)
{
    (void)c;
    events++;
    printf("rt_fp_trap cause 0x%05" PRIX32 "\n", cause_bit);
}

/* Returning lets the interpreter stop with its own message naming the address. */
void rt_bad_target(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    (void)ram;
    (void)c;
    (void)addr;
}

static void log_unrecompiled(uint32_t addr)
{
    events++;
    printf("interp_run entered at 0x%08" PRIX32 "\n", addr);
}

/* --- the capture ----------------------------------------------------------------------- */

struct state {
    uint32_t r[32], hi, lo, fpr[32], fcsr;
    unsigned have; /* bit per field: r, hi, lo, fpr, fcsr */
};

struct capture {
    uint32_t addr;
    char base[512];
    struct state before, after;
};

static int read_words(char *rest, uint32_t *out, int n)
{
    for (int i = 0; i < n; i++) {
        char *end;
        unsigned long v = strtoul(rest, &end, 16);
        if (end == rest || v > 0xFFFFFFFFul)
            return -1;
        out[i] = (uint32_t)v;
        rest = end;
    }
    while (*rest == ' ' || *rest == '\n' || *rest == '\r')
        rest++;
    return *rest == '\0' ? 0 : -1;
}

static int parse_capture(const char *path, struct capture *cap)
{
    static const struct {
        const char *name;
        int n;
    } fields[] = {{"r", 32}, {"hi", 1}, {"lo", 1}, {"fpr", 32}, {"fcsr", 1}};
    char line[1024];
    int have_addr = 0, lineno = 0;
    FILE *f = fopen(path, "r");
    if (!f) {
        fprintf(stderr, "replay: %s: cannot open\n", path);
        return -1;
    }
    memset(cap, 0, sizeof *cap);
    while (fgets(line, sizeof line, f)) {
        char key[16], field[16];
        int used = 0;
        lineno++;
        if (line[0] == '#' || line[0] == '\n')
            continue;
        if (sscanf(line, "%15s %n", key, &used) != 1)
            continue;
        if (strcmp(key, "addr") == 0) {
            have_addr = read_words(line + used, &cap->addr, 1) == 0;
            if (!have_addr)
                break;
        } else if (strcmp(key, "base") == 0) {
            if (sscanf(line + used, "%511s", cap->base) != 1)
                break;
        } else if (strcmp(key, "before") == 0 || strcmp(key, "after") == 0) {
            struct state *s = key[0] == 'b' ? &cap->before : &cap->after;
            int used2 = 0, k;
            if (sscanf(line + used, "%15s %n", field, &used2) != 1)
                break;
            for (k = 0; k < 5 && strcmp(field, fields[k].name) != 0; k++)
                ;
            uint32_t *dst[5] = {s->r, &s->hi, &s->lo, s->fpr, &s->fcsr};
            if (k == 5 || read_words(line + used + used2, dst[k], fields[k].n) != 0) {
                fprintf(stderr, "replay: %s:%d: bad %s line\n", path, lineno, key);
                fclose(f);
                return -1;
            }
            s->have |= 1u << k;
        } /* other keys (time, hits, ...) are information only */
    }
    fclose(f);
    if (!have_addr || !cap->base[0] || cap->before.have != 31u || cap->after.have != 31u) {
        fprintf(stderr, "replay: %s: needs addr, base and the five before and after lines\n",
                path);
        return -1;
    }
    return 0;
}

/* Apply a diff file (records: u32 offset, u32 length, then length bytes; little-endian). */
static int apply_diff(const char *path, uint8_t *ram)
{
    FILE *f = fopen(path, "rb");
    uint8_t head[8];
    size_t n;
    if (!f) {
        fprintf(stderr, "replay: %s: cannot open\n", path);
        return -1;
    }
    while ((n = fread(head, 1, 8, f)) == 8) {
        uint32_t off, len;
        memcpy(&off, head, 4);
        memcpy(&len, head + 4, 4);
        if (off > RAM_SIZE || len > RAM_SIZE - off || fread(ram + off, 1, len, f) != len) {
            fprintf(stderr, "replay: %s: bad record at offset 0x%08" PRIX32 "\n", path, off);
            fclose(f);
            return -1;
        }
    }
    fclose(f);
    if (n != 0) {
        fprintf(stderr, "replay: %s: truncated\n", path);
        return -1;
    }
    return 0;
}

static void join(char *out, size_t size, const char *dir, const char *name)
{
    snprintf(out, size, "%s/%s", dir, name);
}

/* --- the comparison ---------------------------------------------------------------------- */

static int diff_word(const char *what, uint32_t want, uint32_t got)
{
    if (want == got)
        return 0;
    printf("%s: MAME %08" PRIX32 ", replay %08" PRIX32 "\n", what, want, got);
    return 1;
}

static int compare(const struct state *want, const cpu_t *c, const uint8_t *want_ram,
                   const uint8_t *ram)
{
    int bad = 0;
    char name[16];
    uint32_t fcsr = rt_get_fcsr(c);
    for (int i = 0; i < 32; i++)
        bad += diff_word(REG_NAMES[i], want->r[i], c->r[i]);
    bad += diff_word("hi", want->hi, c->hi);
    bad += diff_word("lo", want->lo, c->lo);
    for (int i = 0; i < 32; i++) {
        snprintf(name, sizeof name, "f%d", i);
        bad += diff_word(name, want->fpr[i], c->fpr[i]);
    }
    bad += diff_word("fcsr condition codes", want->fcsr & 0xFE800000u, fcsr & 0xFE800000u);
    bad += diff_word("fcsr other bits", want->fcsr & ~0xFE800000u, fcsr & ~0xFE800000u);
    /* RAM: differing ranges, merged across runs of fewer than MERGE equal bytes */
    uint32_t ranges = 0, bytes = 0, i = 0;
    while (i < RAM_SIZE) {
        uint32_t lo = i, hi = i + 1;
        if (want_ram[i] == ram[i]) {
            i++;
            continue;
        }
        bytes++;
        for (i = hi; i < RAM_SIZE && i - hi < MERGE; i++)
            if (want_ram[i] != ram[i]) {
                bytes++;
                hi = i + 1;
            }
        if (++ranges <= MAX_RANGES)
            printf("RAM differs in 0x%08" PRIX32 "-0x%08" PRIX32 "\n", KSEG0 + lo, KSEG0 + hi - 1);
        i = hi;
    }
    if (bytes) {
        printf("RAM: %" PRIu32 " bytes differ in %" PRIu32 " ranges\n", bytes, ranges);
        bad++;
    }
    return bad;
}

int main(int argc, char **argv)
{
    struct capture cap;
    char path[1024];
    uint8_t *ram, *want_ram;
    recomp_fn fn;
    cpu_t c;
    int bad;
    const char *dir;
    if (argc == 3 && strcmp(argv[1], "--interp") == 0)
        interp_only = 1;
    if (argc != 2 + interp_only) {
        fprintf(stderr, "usage: %s [--interp] CAPTURE_DIR\n", argv[0]);
        return 2;
    }
    dir = argv[1 + interp_only];
    join(path, sizeof path, dir, "capture.txt");
    if (parse_capture(path, &cap) != 0)
        return 2;
    join(path, sizeof path, dir, cap.base);
    ram = ram_load("replay", path);
    if (!ram)
        return 2;
    join(path, sizeof path, dir, "before.diff");
    if (apply_diff(path, ram) != 0)
        return 2;
    want_ram = malloc(RAM_SIZE);
    if (!want_ram)
        return 2;
    memcpy(want_ram, ram, RAM_SIZE);
    join(path, sizeof path, dir, "after.diff");
    if (apply_diff(path, want_ram) != 0)
        return 2;
    fn = recomp_lookup(cap.addr);
    if (!fn) {
        fprintf(stderr, "replay: no recompiled function at 0x%08" PRIX32 "\n", cap.addr);
        return 2;
    }
    memset(&c, 0, sizeof c);
    c.slice = STUB_SLICE;
    memcpy(c.r, cap.before.r, sizeof c.r);
    c.r[0] = 0;
    c.hi = cap.before.hi;
    c.lo = cap.before.lo;
    memcpy(c.fpr, cap.before.fpr, sizeof c.fpr);
    rt_set_fcsr(&c, cap.before.fcsr);
    run_ram = ram;
    if (interp_only) {
        interp_run(ram, &c, cap.addr);
    } else {
        rt_log_unrecompiled = log_unrecompiled;
        fn(ram, &c);
    }
    bad = compare(&cap.after, &c, want_ram, ram) + (events != 0);
    printf("%08" PRIX32 ": %s\n", cap.addr, bad ? "differs" : "same as MAME");
    free(ram);
    free(want_ram);
    return bad ? 1 : 0;
}
