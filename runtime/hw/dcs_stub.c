/* dcs_stub.c: the DCS sound board as the game sees it through the IOASIC, without its
 * processor: SOUNDCTL, SOUNDOUT, SOUNDSTAT and SOUNDIN (the `ioasic:` ioctls 0x6906 to 0x690A
 * and 0x6911, loader-services.md section 3), the sound FIFO's fill, INTSTAT, and the IOASIC's
 * interrupt line IP3.
 *
 * docs/analysis/dcs-handshake.md is the reference, with the measured exchange of run A
 * (mame/m2-facts/dcs-trace.txt, "line" below is a line of that file). The board's replies come
 * from a state machine over the words the game sends: a reset through SOUNDCTL, the boot
 * loader's commands (0x001A blocks, 0x002A), the downloaded program's commands (0x55D0 and
 * 0x55D1 blocks, 0x55D4 and the commands without a reply); a block's reply is the 16-bit sum
 * of its data words. A word the state machine does not know where a command is expected, and
 * a request whose answer was never measured, stop.
 *
 * Timing is not the board's: its own processor decides when it takes a word and when a reply
 * appears, and that is not emulated. The rule instead is never slower than the board: a reply
 * appears after the smallest delay the trace shows for its kind (none for a block's sum), bit
 * 0x80 (the board can take a word) is always set, and bit 0x8000 is set throughout the state
 * in which the trace first shows it. dcs-handshake.md, "Known gaps in the responder", gives
 * what that changes.
 */
#include <string.h>

#include "hw.h"
#include "../loader/loader.h"
#include "machine.h"

/* --- the measured constants ------------------------------------------------------------- */

/* SOUNDCTL at the game's entry: the first 0x6907 read, line 8 (measured). */
#define SOUNDCTL_AT_ENTRY 0x0001u

/* Reply delays, from the SOUNDOUT or SOUNDCTL write that completes the request to the time
 * the reply appears, each below the shortest the trace allows (the time of the last poll that
 * did not yet see it, rounded down to a microsecond), so the model is never later (measured):
 * - after a reset, from the SOUNDCTL write that releases it: 2.027 to 11.207 ms (the four
 *   resets: write lines 11, 4,035, 399,705, 400,706; first polls seeing it 4,029, 13,913,
 *   400,700, 404,084); the shortest is 2,027,407 ns (lines 399,705 to 400,699);
 * - after 0x002A: 1.048 and 2.012 ms (writes 60,565 and 450,736; first seen 61,580 and
 *   452,676); the shortest is 1,047,914 ns;
 * - after 0x55D4: 4.294 to 6.203 ms (writes 77,989, 395,543, 469,085; first seen 83,981,
 *   399,699, 474,140); the shortest is 4,293,614 ns.
 * A block's sum was seen at the first poll after its last data word in all 30 blocks (1.19
 * to 6.44 us later): no delay. */
#define DCS_RESET_REPLY_NS 2027000u
#define DCS_BOOTED_REPLY_NS 1047000u
#define DCS_VERSION_REPLY_NS 4293000u

/* The recorded replies with no rule known (dcs-handshake.md, "Which replies are rules and
 * which are recorded values"): the same word at every occurrence in the trace. */
#define REPLY_RESET 0x000Au   /* after a reset and after a boot block: lines 4,030 ... 60,563 */
#define REPLY_BOOTED 0x000Cu  /* after 0x002A: lines 61,581 and 452,677 */
#define REPLY_VERSION 0x0223u /* after 0x55D4: lines 83,982, 399,700, 474,141 */

/* A word of the board whose value was never measured: the one waiting at the game's entry
 * (the first SOUNDSTAT read, line 4, shows bit 0x40, and no SOUNDIN read precedes the reset
 * that drops it), and the reply to 0x00B9 (bit 0x40 at line 2,910,972, never read). */
#define WORD_UNMEASURED 0xFFFFFFFFu

/* The sound FIFO: 512 words. The game's one IP3 handler wrote 512 (hardware-usage.md,
 * "By GAME.EXE's own code"), after which SOUNDSTAT shows 0x30 (full and half full) at every
 * read and before which 0x08 (empty) at every read (measured, the trace's 1,268,828 and
 * 411,004 reads). That 0x10 starts at half the size is inferred from the bit's place between
 * the empty and full bits; no fill between 0 and 512 was observed. */
#define FIFO_WORDS 512u

/* --- the state -------------------------------------------------------------------------- */

/* The board's state (dcs-handshake.md, "The exchange in attract mode"). */
enum board {
    B_ENTRY,   /* as at the game's entry, before its first reset */
    B_HELD,    /* SOUNDCTL bit 0 clear: in reset */
    B_BOOT,    /* after a reset: the boot loader, which takes 0x001A blocks and 0x002A */
    B_PROGRAM, /* after 0x002A: the downloaded program */
};
static const char *const BOARD_NAME[] = {"entry", "held in reset", "boot", "program"};

/* What a command word does once its header or arguments are in. */
enum action {
    A_NONE,       /* nothing the game can see */
    A_BOOT_BLOCK, /* header start, end, space: the data, then their sum and 0x000A */
    A_BANK_BLOCK, /* header start high, start low, end high, end low: the data, their sum */
    A_BOOTED,     /* 0x000C, and the program runs */
    A_VERSION,    /* 0x0223 */
    A_UNREAD,     /* a reply whose value was never measured; once a word waits, as A_UNSEEN */
    A_UNSEEN,     /* whether it replies cannot be seen: taken only while a word waits */
};

struct command {
    uint16_t word;
    uint8_t states; /* the states (1 << enum board) that take it */
    uint8_t words;  /* header or argument words after it */
    uint8_t action;
    uint8_t pinned; /* 1: the arguments must be the measured ones in args */
    uint16_t args[3];
};

#define IN(s) (1u << (s))
/* Every command word of the trace, in the states the trace shows it in (dcs-handshake.md,
 * steps 2 to 15, and the corrections there for 0x00B9), with how often the trace has it
 * (measured). The download commands (0x001A, 0x55D0, 0x55D1) take any addresses and lengths
 * by rule: their reply is computed from the words. The other commands with arguments are
 * pinned to the one pattern the trace shows: what the board does with another was never
 * seen, so it stops. */
static const struct command COMMANDS[] = {
    {0x55AA, IN(B_ENTRY) | IN(B_PROGRAM), 1, A_NONE, 1, {0xC837}}, /* 3: steps 2, 11, 13 */
    {0x001A, IN(B_BOOT), 3, A_BOOT_BLOCK, 0, {0}},                 /* 8: step 5 */
    {0x002A, IN(B_BOOT), 0, A_BOOTED, 0, {0}},                     /* 2: step 6 */
    {0x55D0, IN(B_PROGRAM), 4, A_BANK_BLOCK, 0, {0}},              /* 14: steps 7, 10 */
    {0x55D1, IN(B_PROGRAM), 4, A_BANK_BLOCK, 0, {0}},              /* 8: step 14 */
    {0x55D2, IN(B_PROGRAM), 0, A_NONE, 0, {0}},                    /* 10: steps 10, 14 */
    {0x55D3, IN(B_PROGRAM), 0, A_NONE, 0, {0}},                    /* 13: 8, 10, 11, 14 */
    {0x55D4, IN(B_PROGRAM), 0, A_VERSION, 0, {0}},                 /* 3: step 9 */
    {0x55B2, IN(B_PROGRAM), 1, A_NONE, 1, {0xBB44}},               /* 2: steps 11, 13 */
    {0x55AE, IN(B_PROGRAM), 1, A_NONE, 1, {0x0100}},               /* 2: steps 13, 15 */
    {0x55D7, IN(B_PROGRAM), 3, A_NONE, 1, {0x0000, 0xFF00, 0x0000}}, /* 1: step 14 */
    {0x00B9, IN(B_PROGRAM), 2, A_UNREAD, 1, {0xFF7F, 0x8000}},     /* 1: step 15 (3 in 600 s) */
    /* The 600 s capture (mame/m2-facts/dcs-trace-600.txt; dcs-handshake.md, "After 150 s"):
     * three more commands of 0x00B9's shape and arguments, each sent while the word the first
     * 0x00B9 raised still waits unread, so nothing the game reads shows whether they reply */
    {0x00BB, IN(B_PROGRAM), 2, A_UNSEEN, 1, {0xFF7F, 0x8000}},     /* 2 in 600 s */
    {0x00B7, IN(B_PROGRAM), 2, A_UNSEEN, 1, {0xFF7F, 0x8000}},     /* 2 in 600 s */
    {0x0059, IN(B_PROGRAM), 2, A_UNSEEN, 1, {0xFF7F, 0x8000}},     /* 2 in 600 s */
};
#define N_COMMANDS (sizeof COMMANDS / sizeof COMMANDS[0])

/* The words the board has sent and the game has not acknowledged, oldest first. At most two
 * are ever outstanding (a boot block's sum and its 0x000A); a third is a request the trace
 * does not show, so it stops. */
#define QUEUE_MAX 2

static struct {
    enum board board;
    uint32_t soundctl;
    uint32_t latch; /* SOUNDIN: the last word the board showed */
    struct {
        uint32_t word;
        uint64_t at; /* when it appears, virtual ns */
    } queue[QUEUE_MAX];
    int queued;
    /* the command being received (NULL between commands), its header or argument words so
     * far, the data words still to come and their sum */
    const struct command *cmd;
    uint32_t header[4];
    uint32_t got;
    uint64_t data_left; /* a guest-given length: only ever counted down */
    uint16_t sum;
    uint32_t fifo;      /* sound FIFO words written, never drained (see dcs_fifo_write) */
    uint16_t unseen;    /* the A_UNSEEN command after which the queue's depth is not known */
    uint64_t data_words;
    uint32_t raises;
} d;

/* --- the reply queue -------------------------------------------------------------------- */

static void appear_event(void *arg);

/* The clock event that re-derives IP3 when the oldest word appears (so an INTCTL that enables
 * INTSTAT bit 0x40 sees it on time): one at most, for the oldest word. */
static void schedule(uint64_t now)
{
    vclock_cancel(appear_event, NULL);
    if (d.queued > 0 && d.queue[0].at > now)
        vclock_at(d.queue[0].at, appear_event, NULL);
}

static void appear_event(void *arg)
{
    (void)arg;
    dcs_irq_update(NULL);
}

static void reply(cpu_t *c, uint32_t word, uint64_t delay)
{
    uint64_t now = vclock_now(c);
    if (d.queued == QUEUE_MAX)
        ld_stop("sound board: a reply 0x%04X with %d words unacknowledged (the trace never has "
                "more than %d)", (unsigned)word, d.queued, QUEUE_MAX);
    d.queue[d.queued].word = word;
    d.queue[d.queued].at = now + delay;
    /* a later word never appears before an earlier one */
    if (d.queued > 0 && d.queue[d.queued].at < d.queue[d.queued - 1].at)
        d.queue[d.queued].at = d.queue[d.queued - 1].at;
    d.queued++;
    schedule(now);
}

/* Bit 0x40: the oldest unacknowledged word has appeared (dcs-handshake.md, "The status bits"). */
static int waiting(uint64_t now)
{
    return d.queued > 0 && d.queue[0].at <= now;
}

static void drop_queue(uint64_t now)
{
    d.queued = 0;
    schedule(now);
}

/* --- status ----------------------------------------------------------------------------- */

/* SOUNDSTAT's FIFO bits (the trace: 0x08 before the handler's writes, 0x30 after). */
static uint32_t fifo_bits(void)
{
    uint32_t bits = 0;
    if (d.fifo == 0)
        bits |= 0x08u;
    if (d.fifo >= FIFO_WORDS / 2)
        bits |= 0x10u;
    if (d.fifo >= FIFO_WORDS)
        bits |= 0x20u;
    return bits;
}

/* SOUNDSTAT (dcs-handshake.md, "The status bits", and "Known gaps in the responder"): 0x80
 * always (never slower than the board, which took every word well inside the 25 ms timeout);
 * 0x40 while a word waits; 0x8000 throughout the program state, where the trace first shows it
 * (line 61,279, after the 0x002A write of line 60,565; never set before, cleared by every
 * reset); the FIFO bits. Bits 0x0100 and 0x0200 are not modelled (read as 0): they change when
 * the board posts certain replies, at the board's times, and no game routine tests them
 * (loader-services.md section 5: the masks 0x40, 0x80 and 0x8080). */
static uint32_t soundstat(uint64_t now)
{
    uint32_t v = 0x80u | fifo_bits();
    if (waiting(now))
        v |= 0x40u;
    if (d.board == B_PROGRAM)
        v |= 0x8000u;
    return v;
}

uint32_t dcs_intstat(cpu_t *c)
{
    /* INTSTAT (dcs-handshake.md, "The IOASIC interrupt"): 0x2000 and 0x0001 in every measured
     * value (entry 0x20C9, lines 785,710 and 785,711: 0x2089, 0x2081); 0x0080 and 0x0040 as
     * SOUNDSTAT's (board ready, word waiting: 0x40 set at the entry with a word waiting, clear
     * at the IP3 with none); 0x0008 while the sound FIFO is empty (cleared by the handler's
     * FIFO writes between its two reads). */
    uint64_t now = c ? vclock_now(c) : machine.now;
    uint32_t v = 0x2001u | 0x80u;
    if (waiting(now))
        v |= 0x40u;
    if (d.fifo == 0)
        v |= 0x08u;
    return v;
}

void dcs_irq_update(cpu_t *c)
{
    /* The level (dcs-handshake.md, "The IOASIC interrupt", inferred from the one IP3): INTCTL
     * bit 0 set and an INTSTAT bit among bits 1 to 15 set whose INTCTL bit is set too. INTCTL
     * 0x4008 with INTSTAT 0x2089 gave none; 0x4009 gave it at once (line 785,707). The
     * loader's default IP3 work clears INTCTL bit 0, which makes the level false (irq.c). */
    uint32_t ctl = machine.intctl & 0xFFFFu;
    int level = (ctl & 1u) && (dcs_intstat(c) & ctl & 0xFFFEu);
    int pending = (machine.cause_ip & 0x800u) != 0;
    if (level && !pending) {
        irq_raise(3);
        d.raises++;
        if (c)
            vclock_reload(c);
    } else if (!level && pending) {
        irq_clear(3);
    }
}

/* --- the command state machine ---------------------------------------------------------- */

/* A command's header or arguments are in: start its data or act. */
static void command_done(cpu_t *c)
{
    const struct command *k = d.cmd;
    uint32_t *h = d.header;
    switch (k->action) {
    case A_BOOT_BLOCK: /* start, end, space: (end - start + 1) words, twice that for space 0
                        * (dcs-handshake.md, step 5) */
        if (h[1] < h[0])
            ld_stop("sound board: 0x001A block with end 0x%04X before start 0x%04X",
                    (unsigned)h[1], (unsigned)h[0]);
        if (h[2] > 1)
            ld_stop("sound board: 0x001A block with space %u (the trace has 0 and 1)",
                    (unsigned)h[2]);
        d.data_left = (uint64_t)(h[1] - h[0] + 1) * (h[2] ? 1 : 2);
        d.sum = 0;
        return;
    case A_BANK_BLOCK: { /* 32-bit start and end: (end - start + 1) words (step 7) */
        uint32_t start = h[0] << 16 | h[1], end = h[2] << 16 | h[3];
        if (end < start)
            ld_stop("sound board: 0x%04X block with end 0x%08X before start 0x%08X",
                    (unsigned)k->word, (unsigned)end, (unsigned)start);
        d.data_left = (uint64_t)(end - start) + 1;
        d.sum = 0;
        return;
    }
    case A_BOOTED: /* 0x000C, a recorded constant, and the downloaded program runs */
        d.board = B_PROGRAM;
        reply(c, REPLY_BOOTED, DCS_BOOTED_REPLY_NS);
        break;
    case A_VERSION: /* 0x0223, a recorded constant (plausibly the firmware version) */
        reply(c, REPLY_VERSION, DCS_VERSION_REPLY_NS);
        break;
    case A_UNREAD: /* bit 0x40 came up after 0x00B9's last word; its value was never read and
                    * its time is unknown (somewhere in 22.6 to 132.5 s): at once. Its later
                    * sends (251.7 and 480.9 s in the 600 s capture) find that word still
                    * waiting, so they are A_UNSEEN */
        if (waiting(vclock_now(c)))
            d.unseen = k->word;
        else
            reply(c, WORD_UNMEASURED, 0);
        break;
    case A_UNSEEN: /* the board may or may not queue a reply: while a word already waits, every
                    * status bit the game reads is the same either way (0x40 is set by the
                    * waiting word, 0x80 is the board's readiness); only the queue's depth is
                    * unknown, and the acknowledgement that would show it stops (dcs_ioctl) */
        if (!waiting(vclock_now(c)))
            ld_stop("sound board: 0x%04X with no word waiting: whether it replies, and what, "
                    "was never seen (in MAME's 600 s it always came while 0x00B9's word "
                    "waited)", (unsigned)k->word);
        d.unseen = k->word;
        break;
    default:
        break;
    }
    d.cmd = NULL;
}

/* The last data word of a block is in: its reply (dcs-handshake.md, "Which replies are rules"). */
static void block_done(cpu_t *c)
{
    reply(c, d.sum, 0);
    if (d.cmd->action == A_BOOT_BLOCK)
        reply(c, REPLY_RESET, 0);
    d.cmd = NULL;
}

static void soundout(cpu_t *c, uint32_t w)
{
    if (d.board == B_HELD)
        ld_stop("sound board: SOUNDOUT 0x%04X while the board is held in reset (SOUNDCTL "
                "0x%04X)", (unsigned)w, (unsigned)d.soundctl);
    if (d.cmd && d.got == d.cmd->words) { /* inside a block: data, consumed by its length */
        d.sum = (uint16_t)(d.sum + w);
        d.data_words++;
        if (--d.data_left == 0)
            block_done(c);
        return;
    }
    if (d.cmd) { /* a header or argument word */
        if (d.cmd->pinned && w != d.cmd->args[d.got])
            ld_stop("sound board: 0x%04X argument %u is 0x%04X, not the measured 0x%04X (%s "
                    "state): what the board does with it was never seen",
                    (unsigned)d.cmd->word, (unsigned)d.got + 1, (unsigned)w,
                    (unsigned)d.cmd->args[d.got], BOARD_NAME[d.board]);
        d.header[d.got++] = w;
        if (d.got == d.cmd->words)
            command_done(c);
        return;
    }
    for (size_t i = 0; i < N_COMMANDS; i++) {
        if (COMMANDS[i].word == w && (COMMANDS[i].states & IN(d.board))) {
            d.cmd = &COMMANDS[i];
            d.got = 0;
            if (d.cmd->words == 0)
                command_done(c);
            return;
        }
    }
    ld_stop("sound board: SOUNDOUT 0x%04X is not a command the board takes in the %s state",
            (unsigned)w, BOARD_NAME[d.board]);
}

/* SOUNDCTL bit 0 clear holds the board in reset and drops what it was doing; set again, the
 * board's boot loader starts and sends 0x000A (dcs-handshake.md, step 3; the 0x0001 and
 * 0x000F writes of step 13 leave bit 0 set and reset nothing). Other bits are stored only,
 * and only the values the game wrote are taken (measured: 0x0000 4 times, 0x0001 5 times,
 * 0x000F once). */
static void soundctl(cpu_t *c, uint32_t v)
{
    uint64_t now = vclock_now(c);
    uint32_t old = d.soundctl;
    if (v != 0x0000u && v != 0x0001u && v != 0x000Fu)
        ld_stop("sound board: SOUNDCTL write 0x%04X (the game wrote 0x0000, 0x0001 and 0x000F "
                "only)", (unsigned)v);
    d.soundctl = v;
    if ((old & 1u) && !(v & 1u)) {
        d.board = B_HELD;
        d.cmd = NULL;
        d.unseen = 0; /* the reset empties the queue, whatever it held */
        drop_queue(now);
    } else if (!(old & 1u) && (v & 1u)) {
        d.board = B_BOOT;
        reply(c, REPLY_RESET, DCS_RESET_REPLY_NS);
    }
}

/* --- the interface ---------------------------------------------------------------------- */

void dcs_reset(void)
{
    vclock_cancel(appear_event, NULL);
    memset(&d, 0, sizeof d); /* no command under way, the FIFO empty, the counters 0 */
    d.board = B_ENTRY;
    d.soundctl = SOUNDCTL_AT_ENTRY;
    d.latch = WORD_UNMEASURED;
    d.queue[0].word = WORD_UNMEASURED; /* the word waiting at the entry (line 4, measured) */
    d.queue[0].at = 0;
    d.queued = 1;
}

void dcs_fifo_write(cpu_t *c)
{
    /* Nothing drains the FIFO: the trace has one IP3 in 150 s, so the board never emptied it
     * again in attract mode (inferred); a word to a full FIFO was never measured. */
    if (d.fifo == FIFO_WORDS)
        hw_stop(c, SNDFIFO_PHYS, 4, 1, 0, "the sound FIFO is full (%u words) and the model "
                "does not drain it", FIFO_WORDS);
    d.fifo++;
    dcs_irq_update(c);
}

int dcs_ioctl(cpu_t *c, uint32_t cmd, uint32_t arg)
{
    uint64_t now = vclock_now(c);
    switch (cmd) {
    case 0x6906: /* SOUNDCTL write */
        soundctl(c, arg & 0xFFFFu);
        break;
    case 0x6907: /* SOUNDCTL read */
        ld_put32(arg, d.soundctl);
        return 0;
    case 0x6908: /* SOUNDOUT write */
        soundout(c, arg & 0xFFFFu);
        break;
    case 0x6909: /* SOUNDSTAT read */
        ld_put32(arg, soundstat(now));
        return 0;
    case 0x690A: { /* SOUNDIN read: the waiting word, or the last one again (the "Rules") */
        uint32_t w = waiting(now) ? d.queue[0].word : d.latch;
        if (w == WORD_UNMEASURED)
            ld_stop("sound board: SOUNDIN 0x690A read of a word whose value was not measured "
                    "(%s)", waiting(now) ? "the one waiting" : "none has been sent");
        d.latch = w;
        ld_put32(arg, w);
        return 0;
    }
    case 0x6911: /* SOUNDIN write 0: the acknowledgement, which takes the waiting word away */
        if ((arg & 0xFFFFu) != 0)
            ld_stop("sound board: SOUNDIN write 0x%04X (the game writes 0 only)",
                    (unsigned)(arg & 0xFFFFu));
        if (d.unseen)
            ld_stop("sound board: SOUNDIN acknowledged after 0x%04X, whose reply (if any) was "
                    "never seen: whether another word then waits is not known",
                    (unsigned)d.unseen);
        if (waiting(now)) {
            d.latch = d.queue[0].word; /* SOUNDIN keeps it */
            for (int i = 1; i < d.queued; i++)
                d.queue[i - 1] = d.queue[i];
            d.queued--;
            schedule(now);
        }
        break;
    default:
        ld_stop("sound board: ioctl 0x%04X is not a sound-board command", (unsigned)cmd);
    }
    dcs_irq_update(c);
    return 0;
}

int dcs_queued(void)
{
    return d.queued;
}

uint64_t dcs_data_words(void)
{
    return d.data_words;
}

uint32_t dcs_ip3_raises(void)
{
    return d.raises;
}
