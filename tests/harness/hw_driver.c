/* hw_driver.c: runs the bus (runtime/core/bus.c), the devices behind it (runtime/hw/hw.h)
 * and the loader stand-ins (runtime/loader/loader.h) from commands on standard input, for
 * tests/test_hw.py, with the runtime core (clock, interrupts, exceptions), the interpreter
 * and the disk library (runtime/disk/disk.h).
 *
 * One command per line. Numbers are hexadecimal, except times in nanoseconds and counts
 * (decimal):
 *   reset                machine_reset, loader_reset, hw_reset; RAM and cpu_t zeroed, FCSR 0
 *   machinereset         machine_reset alone, cpu_t's slice zeroed (a misuse, for its test)
 *   img RAW OVERLAY      img_open (OVERLAY - for none); "img 0", or "img -1 MESSAGE"
 *   mem ADDR WORD        a RAM word
 *   fill ADDR N BYTE     N RAM bytes from ADDR set to BYTE
 *   r N V                general register N
 *   pc V                 c->pc
 *   call SLOT A0 A1 A2 A3   a0 to a3 set, rt_loader(SLOT); then "ret V0 V1"
 *   rd SIZE ADDR         "rd V": mem_r8, mem_r16 or mem_r32 (SIZE 1, 2 or 4) at ADDR
 *   wr SIZE ADDR V       mem_w8, mem_w16 or mem_w32 at ADDR
 *   dump ADDR N          "dump W0 .. W(N-1)", N RAM words from ADDR
 *   peek ADDR N          "peek HEX", N RAM bytes from ADDR as hexadecimal
 *   regs                 "regs R0 .. R31 HI LO"
 *   advance NS           ticks as if each slice ran out until vclock_now reaches NS, then
 *                        "now NOW"
 *   checks K             K check points that do not reach 0 (slice -= K), then "now NOW"
 *   at NS                the virtual clock set to NS (decimal, not earlier than now), with
 *                        no events run: for replaying a capture at its recorded times; test
 *                        only, the runtime has no such call
 *   vsync                "vsync NEXT LEN PERIOD" (picoseconds since power-on, decimal):
 *                        voodoo_vsync, or "vsync -" before the video timing is programmed
 *   dropped              "dropped N0 .. N(DROP_COUNT-1)": vfb_dropped per reason
 *   state                "m STATUS CAUSE_IP GT_CAUSE GT_MASK NOW"
 *   delay N BYTES        "delay NS": dma0_delay_ns(N, BYTES), N and BYTES decimal
 *   counts REG           "counts STD ALT TEX FIFO": voodoo_reg_writes(REG),
 *                        voodoo_alt_writes(REG), voodoo_tex_writes(), sndfifo_writes()
 *   dcs                  "dcs QUEUED DATA RAISES CAUSE_IP": dcs_queued(), dcs_data_words(),
 *                        dcs_ip3_raises() (decimal) and the pending IP bits
 *   dcsreplay PATH ENTRY FIFO   replays a DCS trace (mame/m2-facts/dcs-trace.txt's format)
 *                        through ioctl slot 0x0C8 on fd 4 at its times less ENTRY (ns), with
 *                        FIFO sound-FIFO word writes through the bus (0xB3000000) right after
 *                        the IP3 handler's first INTSTAT read; prints the summary lines its
 *                        comment lists
 * A stop (rt_stop) ends the run with status 3 and its message on stderr.
 *
 * recomp_lookup knows one function, PROBE (0x80300000), which prints "probe A0 SP RA NOW"
 * and returns, and stores 0xDEAD0000 + its call count in a0 to a3, t0 to t9, at, v1, hi
 * and lo (scratch registers a callee may change), so a test sees whether the caller kept
 * them.
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "disk.h"
#include "hw.h"
#include "loader.h"
#include "machine.h"

#define PROBE 0x80300000u

static uint8_t *ram;
static cpu_t cpu;
static uint32_t probe_calls;

static _Noreturn void die(const char *what, const char *detail)
{
    fflush(stdout);
    fprintf(stderr, "hw_driver: %s%s%s\n", what, detail ? ": " : "", detail ? detail : "");
    exit(1);
}

/* --- what the runtime needs that it does not provide ------------------------------------- */

void rt_host_switch(uint8_t *r, cpu_t *c, int kind)
{
    (void)r, (void)c;
    fprintf(stderr, "hw_driver: rt_host_switch %d\n", kind);
    exit(1);
}

int rt_switch_kind(uint32_t pc)
{
    (void)pc;
    return -1;
}

static void probe(uint8_t *r, cpu_t *c)
{
    static const int scratch[] = {1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25};
    uint32_t mark = 0xDEAD0000u + ++probe_calls;
    (void)r;
    printf("probe %08" PRIX32 " %08" PRIX32 " %08" PRIX32 " %" PRIu64 "\n", c->r[4], c->r[29],
           c->r[31], vclock_now(c));
    for (size_t i = 0; i < sizeof scratch / sizeof scratch[0]; i++)
        c->r[scratch[i]] = mark;
    c->hi = c->lo = mark;
}

recomp_fn recomp_lookup(uint32_t addr)
{
    return addr == PROBE ? probe : NULL;
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

static void word(char *buf, size_t n)
{
    char fmt[16];
    snprintf(fmt, sizeof fmt, "%%%zus", n - 1);
    if (scanf(fmt, buf) != 1)
        die("bad input", "expected a word");
}

static uint8_t *ram_at(uint32_t addr, uint32_t n)
{
    if (!mem_is_ram(addr, n ? n : 1))
        die("not a RAM range", NULL);
    return ram + (addr & 0x1FFFFFFFu);
}

static void reset(void)
{
    machine_reset();
    loader_reset();
    hw_reset();
    memset(ram, 0, RAM_SIZE);
    memset(&cpu, 0, sizeof cpu);
    rt_set_fcsr(&cpu, 0);
    probe_calls = 0;
}

static void advance(uint64_t until)
{
    while (vclock_now(&cpu) < until) {
        cpu.slice = 0; /* every check point of the slice ran */
        rt_tick(ram, &cpu);
    }
    printf("now %" PRIu64 "\n", vclock_now(&cpu));
}

/* --- the DCS trace replay ---------------------------------------------------------------- */

#define REPLAY_BUF 0x80202000u

static uint32_t replay_ioctl(uint32_t cmd, uint32_t arg)
{
    uint32_t w;
    cpu.r[4] = 4;
    cpu.r[5] = cmd;
    cpu.r[6] = arg;
    rt_loader(ram, &cpu, 0x0C8);
    if (cpu.r[2] != 0)
        die("dcsreplay: an ioctl returned non-zero", NULL);
    memcpy(&w, ram + (REPLAY_BUF & 0x1FFFFFFFu), 4);
    return w;
}

/* The summary, one line each:
 *   dcsread KIND MATCHED TOTAL FIRST_BAD_LINE GOT WANT   per result kind 0x6907, 0x690A,
 *                        0x690E, 0x6910 (FIRST_BAD_LINE 0 and GOT WANT 0 when all match)
 *   dcsstate MATCHED TOTAL FIRST_BAD_LINE   SOUNDSTAT reads whose state bits (0x38) match
 *   dcsbit BIT LATE EARLY FIRST_LATE_LINE   per timing bit of SOUNDSTAT: LATE reads recorded
 *                        set and modelled clear, EARLY modelled set and recorded clear
 *   dcsother DIFF        SOUNDSTAT reads differing outside 0x38, 0x40, 0x80 and 0x8000
 *   dcsorphan N          SOUNDSTAT reads with 0x40 modelled set and nothing queued
 *   dcsip3 LINE PREV     each ip3 event and the line of the event fed before it
 *   dcsraise LINE        each raise of IP3 by the model, at the line whose event caused it
 *   dcsfifo LINE         where the FIFO writes went in (after that line's INTSTAT read)
 *   dcsdata N            dcs_data_words() at the end */
static void dcs_replay(const char *path, uint64_t entry, uint32_t fifo)
{
    static const uint32_t kinds[4] = {0x6907, 0x690A, 0x690E, 0x6910};
    static const uint32_t bits[3] = {0x40, 0x80, 0x8000};
    uint64_t matched[4] = {0}, total[4] = {0}, bad_line[4] = {0}, got[4] = {0}, want[4] = {0};
    uint64_t st_ok = 0, st_total = 0, st_bad = 0, other = 0, orphan = 0;
    uint64_t late[3] = {0}, early[3] = {0}, late_line[3] = {0};
    uint64_t line = 0, prev_fed = 0;
    uint32_t raises = dcs_ip3_raises();
    int in_handler = 0;
    static char buf[4096];
    FILE *f = fopen(path, "r");
    if (!f)
        die("dcsreplay: cannot open", path);
    while (fgets(buf, sizeof buf, f)) {
        uint64_t t;
        char kind[16];
        uint32_t v;
        if (!strchr(buf, '\n') && !feof(f))
            die("dcsreplay: a line longer than the buffer", NULL);
        line++;
        if (buf[0] == '#')
            continue;
        if (sscanf(buf, "%" SCNu64 " %15s %" SCNx32, &t, kind, &v) != 3)
            die("dcsreplay: bad line", buf);
        if (t < entry)
            die("dcsreplay: an event before the entry", buf);
        vclock_account(&cpu);
        if (t - entry < machine.now)
            die("dcsreplay: an event before the virtual clock", buf);
        machine.now = t - entry;
        if (strcmp(kind, "ip3") == 0) {
            printf("dcsip3 %" PRIu64 " %" PRIu64 "\n", line, prev_fed);
            continue;
        }
        if (strcmp(kind, "h3") == 0) {
            in_handler = 1;
            continue;
        }
        if (strcmp(kind, "eeee") == 0)
            die("dcsreplay: an eeee event (none in run A)", buf);
        uint32_t cmd = (uint32_t)strtoul(kind, NULL, 16);
        switch (cmd) {
        case 0x6906: case 0x6908: case 0x6911: case 0x690F:
            replay_ioctl(cmd, v);
            break;
        case 0x6909: {
            uint32_t m = replay_ioctl(cmd, REPLAY_BUF) & 0xFFFFu;
            st_total++;
            if ((m & 0x38u) == (v & 0x38u))
                st_ok++;
            else if (!st_bad)
                st_bad = line;
            for (int i = 0; i < 3; i++) {
                if ((v & bits[i]) && !(m & bits[i]) && !late[i]++)
                    late_line[i] = line;
                if (!(v & bits[i]) && (m & bits[i]))
                    early[i]++;
            }
            if ((m ^ v) & ~0x80F8u)
                other++;
            if ((m & 0x40u) && dcs_queued() == 0)
                orphan++;
            break;
        }
        default: {
            int i = 0;
            while (i < 4 && kinds[i] != cmd)
                i++;
            if (i == 4)
                die("dcsreplay: an unknown event kind", buf);
            uint32_t m = replay_ioctl(cmd, REPLAY_BUF) & 0xFFFFu;
            total[i]++;
            if (m == v) {
                matched[i]++;
            } else if (!bad_line[i]) {
                bad_line[i] = line;
                got[i] = m;
                want[i] = v;
            }
            if (cmd == 0x690E && in_handler) {
                /* the handler's FIFO writes come after its first INTSTAT read */
                for (uint32_t n = 0; n < fifo; n++)
                    mem_w32(ram, &cpu, 0xB3000000u, 0);
                printf("dcsfifo %" PRIu64 "\n", line);
                in_handler = 0;
            }
        }
        }
        prev_fed = line;
        for (; raises < dcs_ip3_raises(); raises++)
            printf("dcsraise %" PRIu64 "\n", line);
    }
    if (ferror(f))
        die("dcsreplay: read error", path);
    fclose(f);
    for (int i = 0; i < 4; i++)
        printf("dcsread %04" PRIX32 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %04" PRIX64
               " %04" PRIX64 "\n", kinds[i], matched[i], total[i], bad_line[i], got[i], want[i]);
    printf("dcsstate %" PRIu64 " %" PRIu64 " %" PRIu64 "\n", st_ok, st_total, st_bad);
    for (int i = 0; i < 3; i++)
        printf("dcsbit %04" PRIX32 " %" PRIu64 " %" PRIu64 " %" PRIu64 "\n", bits[i], late[i],
               early[i], late_line[i]);
    printf("dcsother %" PRIu64 "\ndcsorphan %" PRIu64 "\ndcsdata %" PRIu64 "\n", other, orphan,
           dcs_data_words());
}

int main(void)
{
    static char key[16];
    ram = malloc(RAM_SIZE);
    if (!ram)
        die("out of memory", NULL);
    reset();
    while (scanf("%15s", key) == 1) {
        if (strcmp(key, "machinereset") == 0) {
            machine_reset();
            cpu.slice = 0;
        } else if (strcmp(key, "reset") == 0) {
            reset();
        } else if (strcmp(key, "img") == 0) {
            static char raw[1024], ovl[1024], err[256];
            word(raw, sizeof raw);
            word(ovl, sizeof ovl);
            if (img_open(raw, strcmp(ovl, "-") ? ovl : NULL, err, sizeof err) == 0)
                puts("img 0");
            else
                printf("img -1 %s\n", err);
        } else if (strcmp(key, "mem") == 0) {
            uint32_t addr = hex(), w = hex();
            memcpy(ram_at(addr, 4), &w, 4);
        } else if (strcmp(key, "fill") == 0) {
            uint32_t addr = hex(), n = hex(), b = hex();
            memset(ram_at(addr, n), (int)b, n);
        } else if (strcmp(key, "r") == 0) {
            uint32_t n = hex();
            if (n > 31)
                die("bad input", "register number above 31");
            cpu.r[n] = hex();
        } else if (strcmp(key, "pc") == 0) {
            cpu.pc = hex();
        } else if (strcmp(key, "call") == 0) {
            uint32_t slot = hex();
            for (int i = 4; i < 8; i++)
                cpu.r[i] = hex();
            rt_loader(ram, &cpu, slot);
            printf("ret %08" PRIX32 " %08" PRIX32 "\n", cpu.r[2], cpu.r[3]);
        } else if (strcmp(key, "rd") == 0) {
            uint32_t size = hex(), addr = hex(), v;
            if (size == 1)
                v = mem_r8(ram, &cpu, addr);
            else if (size == 2)
                v = mem_r16(ram, &cpu, addr);
            else if (size == 4)
                v = mem_r32(ram, &cpu, addr);
            else
                die("bad input", "rd: size not 1, 2 or 4");
            printf("rd %08" PRIX32 "\n", v);
        } else if (strcmp(key, "wr") == 0) {
            uint32_t size = hex(), addr = hex(), v = hex();
            if (size == 1)
                mem_w8(ram, &cpu, addr, v);
            else if (size == 2)
                mem_w16(ram, &cpu, addr, v);
            else if (size == 4)
                mem_w32(ram, &cpu, addr, v);
            else
                die("bad input", "wr: size not 1, 2 or 4");
        } else if (strcmp(key, "dump") == 0) {
            uint32_t addr = hex(), n = hex();
            fputs("dump", stdout);
            for (uint32_t i = 0; i < n; i++) {
                uint32_t w;
                memcpy(&w, ram_at(addr + 4 * i, 4), 4);
                printf(" %08" PRIX32, w);
            }
            putchar('\n');
        } else if (strcmp(key, "peek") == 0) {
            uint32_t addr = hex(), n = hex();
            const uint8_t *p = ram_at(addr, n);
            fputs("peek ", stdout);
            for (uint32_t i = 0; i < n; i++)
                printf("%02X", p[i]);
            putchar('\n');
        } else if (strcmp(key, "regs") == 0) {
            fputs("regs", stdout);
            for (int i = 0; i < 32; i++)
                printf(" %08" PRIX32, cpu.r[i]);
            printf(" %08" PRIX32 " %08" PRIX32 "\n", cpu.hi, cpu.lo);
        } else if (strcmp(key, "advance") == 0) {
            advance(dec());
        } else if (strcmp(key, "checks") == 0) {
            cpu.slice -= (int32_t)hex();
            printf("now %" PRIu64 "\n", vclock_now(&cpu));
        } else if (strcmp(key, "at") == 0) {
            /* test only, for replaying a capture at its times: the virtual clock set to NS */
            uint64_t t = dec();
            vclock_account(&cpu);
            if (t < machine.now)
                die("bad input", "at: a time before the virtual clock");
            machine.now = t;
        } else if (strcmp(key, "vsync") == 0) {
            uint64_t next, len, period;
            if (voodoo_vsync(&next, &len, &period))
                printf("vsync %" PRIu64 " %" PRIu64 " %" PRIu64 "\n", next, len, period);
            else
                puts("vsync -");
        } else if (strcmp(key, "dropped") == 0) {
            fputs("dropped", stdout);
            for (int i = 0; i < DROP_COUNT; i++)
                printf(" %" PRIu32, vfb_dropped(i));
            putchar('\n');
        } else if (strcmp(key, "state") == 0) {
            printf("m %08" PRIX32 " %08" PRIX32 " %08" PRIX32 " %08" PRIX32 " %" PRIu64 "\n",
                   m_status(), machine.cause_ip, machine.gt_cause, machine.gt_mask,
                   vclock_now(&cpu));
        } else if (strcmp(key, "delay") == 0) {
            uint64_t n = dec(), bytes = dec();
            if (n > UINT32_MAX)
                die("bad input", "delay: record count above 2^32");
            printf("delay %" PRIu64 "\n", dma0_delay_ns((uint32_t)n, bytes));
        } else if (strcmp(key, "counts") == 0) {
            uint32_t reg = hex();
            printf("counts %" PRIu32 " %" PRIu32 " %" PRIu64 " %" PRIu64 "\n",
                   voodoo_reg_writes(reg), voodoo_alt_writes(reg), voodoo_tex_writes(),
                   sndfifo_writes());
        } else if (strcmp(key, "dcs") == 0) {
            printf("dcs %d %" PRIu64 " %" PRIu32 " %08" PRIX32 "\n", dcs_queued(),
                   dcs_data_words(), dcs_ip3_raises(), machine.cause_ip);
        } else if (strcmp(key, "dcsreplay") == 0) {
            static char path[1024];
            uint64_t entry;
            word(path, sizeof path);
            entry = dec();
            dcs_replay(path, entry, (uint32_t)dec());
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
