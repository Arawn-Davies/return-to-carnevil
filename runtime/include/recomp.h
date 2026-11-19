/* recomp.h: the contract between generated code, the reference interpreter and a runtime.
 *
 * The CPU is an R5000 run with 32-bit general registers and FR = 0: 32 FP register halves of
 * 32 bits; a single is one half, a double in even register n is half n (low word) and half
 * n + 1 (high word). The board has 8 MB of RAM, seen at 0x80000000 (kseg0), 0xA0000000
 * (kseg1) and, through the loader's TLB entries, 0x00040000 up in kuseg (mem_is_ram); every
 * other address is the bus.
 *
 * Results that are MAME's own behaviour rather than an established R5000 fact (the oracle
 * the port is checked against, see vectors.MAME_SPECIFIC) are marked "MAME:" below.
 *
 * Build every file including this one with -std=c11 -O2 -ffp-contract=off -frounding-math
 * -fno-strict-aliasing -fwrapv, never -ffast-math.
 */
#ifndef RECOMP_H
#define RECOMP_H

#include <math.h>
#include <stdint.h>
#include <string.h>

#if !defined(__BYTE_ORDER__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "recomp.h: RAM is accessed in host byte order, which must be little-endian"
#endif

typedef struct cpu {
    uint32_t r[32];
    uint32_t hi, lo;
    uint32_t fpr[32];
    uint32_t fcsr;  /* FCSR without its condition bits (FCSR bits 23 and 25..31 are 0 here) */
    uint8_t fcc;    /* bit n is FP condition code n */
    uint32_t pc;    /* kept by the interpreter, and by generated code built with --trace-pc */
    int32_t slice;  /* check points left until RT_CHECK calls rt_tick (below) */
    void *rt;       /* the runtime's own data */
} cpu_t;

typedef void (*recomp_fn)(uint8_t *ram, cpu_t *c);

#define RAM_SIZE 0x800000u

/* --- provided by a runtime ------------------------------------------------------------ */

/* bus_read, bus_write: a load or store whose address mem_is_ram rejects (hardware, or kuseg
 * outside the mapped RAM). size is 1, 2 or 4 (lb/lbu/sb, lh/lhu/sh, the word accesses; ldc1
 * and sdc1 are two word accesses). The caller masks a read's result to the size, so a
 * runtime may return a wider value; a write's value arrives masked to the size. The address
 * is not checked for alignment (see "memory" below). The generated code passes no guest PC:
 * c->pc names the instruction that made the access only in the interpreter and in generated
 * code emitted with --trace-pc (recompile --trace-pc stores it before each load, store and
 * call). */
uint32_t bus_read(cpu_t *c, uint32_t addr, int size);
void bus_write(cpu_t *c, uint32_t addr, int size, uint32_t value);

/* rt_loader: a call or jump into the stub block, where the resident loader's services are
 * (config/boot_state.toml, [[external_call]]). slot is the byte offset of the slot from the
 * stub block's start (0x800C408C in the supported revision), so 0x000 to 0x238 in steps of
 * 8. At entry the transfer is done as far as the board would have done it: for a jal, ra is
 * already the return address (the jal's address + 8); the delay slot has run; the arguments
 * are in a0-a3 and on the guest stack (o32). On return the runtime has set what the service
 * returns (v0, v1) and whatever else the service writes (RAM, registers it clobbers). The
 * caller then goes on after a jal, and returns to its own caller after a j or from a stub
 * thunk (a function whose body is this call). rt_loader may re-enter game code through
 * rt_call (a registered callback or handler). It is not an interrupt check point: a service
 * that waits (for DMA, a frame) must advance the clock and deliver interrupts itself (what
 * rt_tick does). */
void rt_loader(uint8_t *ram, cpu_t *c, uint32_t slot);

/* rt_break: a break instruction at pc; code is the 20-bit field of the break word, bits 6 to
 * 25. Returning resumes at the next instruction (pc + 4), so a runtime must not return from
 * a break the game treats as fatal (0x407, an assert). */
void rt_break(uint8_t *ram, cpu_t *c, uint32_t pc, uint32_t code);

/* rt_tick: called by RT_CHECK (below) when slice reaches 0, with the guest state in *c. Time
 * is counted in check points: the runtime accounts the checks used since it last set slice,
 * advances its virtual clock by them, runs the events that are due and delivers the
 * interrupts that are pending, then reloads slice (at least 1). It must leave all guest state
 * (r, hi, lo, fpr, fcsr and fcc, and RAM apart from what the handlers write) and the host FP
 * environment as an exception return would, apart from what the handlers' ABI changes. It
 * may call rt_call: the game's handlers are recompiled functions with RT_CHECK at their
 * entry, so slice must be reloaded (or the source masked) before a handler runs, or rt_tick
 * re-enters itself. Handlers run on whatever host context (stack, fiber, thread) is current
 * at the check point. slice belongs to the thread running the guest; nothing else writes it. */
void rt_tick(uint8_t *ram, cpu_t *c);

/* rt_host_switch: a transfer of the game's task switch that leaves guest control flow for the
 * runtime (config/game.manual.toml, [[host_switch]]). At entry the delay slot has run; kind is
 * RT_SWITCH_YIELD (the `j` that ends the yield), RT_SWITCH_EXIT (the `j` that ends a finished
 * task's exit) or RT_SWITCH_RESUME (the scheduler's `jr ra` into a task). On return the caller
 * goes on as the kind says: after a resume, at the instruction after its delay slot (the
 * scheduler's restore path, reached when the task yields); after a yield, by returning from
 * the C function (to whatever host frame resumed the task); after an exit, the same, though
 * the runtime never returns from an exit. rt_switch_kind (below) gives the interpreter the
 * same points. */
enum { RT_SWITCH_RESUME, RT_SWITCH_YIELD, RT_SWITCH_EXIT };
void rt_host_switch(uint8_t *ram, cpu_t *c, int kind);

/* rt_fp_trap: the FP exception the board raises, with cause_bit its FCSR cause bit (1 << 15,
 * divide by zero, is the only one raised: by rc_div_s and rc_div_d below). The runtime does
 * not return from it: on the board the game's catch-all handler runs and ends at the reset
 * vector. A test or stub runtime that does return gets the IEEE result of the operation. */
void rt_fp_trap(cpu_t *c, uint32_t cause_bit);

/* rt_bad_target: the interpreter is to fetch from addr, which is not RAM: a call or jump
 * through a null or corrupt pointer, or to the boot ROM (the game's crash handler ends with
 * a jump to the reset vector 0xBFC00000). c->pc is the pc last stored. A runtime need not
 * return; if it does, the interpreter stops the process with a message naming the address. */
void rt_bad_target(uint8_t *ram, cpu_t *c, uint32_t addr);

/* rt_switch_kind: the RT_SWITCH_* kind of the host-switch point at pc, or -1 when pc is none.
 * The generated func_table.c provides it for the game; a test harness may provide its own. The
 * interpreter asks it at every `j` and `jr`. */
int rt_switch_kind(uint32_t pc);

/* recomp_lookup: the recompiled function at addr, or NULL when there is none. The generated
 * func_table.c provides it for the game; a test harness may provide its own. rt_call and the
 * interpreter use it to choose between recompiled code and interp_run. */
recomp_fn recomp_lookup(uint32_t addr);

/* --- provided by runtime/core --------------------------------------------------------- */

/* rt_call: a call to addr (jalr, a jal to an address that is no known function start, the
 * interpreter's calls and tail jumps, a runtime's callback): the recompiled function at addr
 * if recomp_lookup has one, otherwise interp_run from addr. A call's link register is
 * already written. It returns when the callee returns (its jr ra). */
void rt_call(uint8_t *ram, cpu_t *c, uint32_t addr);
/* rt_jump: the same, for a jump that leaves its function (a jr that is not on ra and has no
 * resolved table, or a table's default); the caller returns straight after it. Each such
 * jump nests one host frame. */
void rt_jump(uint8_t *ram, cpu_t *c, uint32_t addr);

/* rt_set_fcsr: FCSR as ctc1 $31 writes it (condition bits in place); also sets the host
 * rounding mode from RM, and host flush-to-zero from FS only when runtime/core/fpu.c is
 * built with RT_HONOUR_FS=1 (by default FS is kept but ignored, as MAME ignores it).
 * rt_get_fcsr: FCSR as cfc1 $31 reads it.
 *
 * The host FP environment: rt_set_fcsr is the only thing that sets it, and it belongs to the
 * process (per thread), not to cpu_t, so a runtime's own float code runs under the guest's
 * rounding and flush settings. A runtime that loads a guest state, or switches host context
 * or thread, must re-apply it: rt_set_fcsr(c, rt_get_fcsr(c)). */
void rt_set_fcsr(cpu_t *c, uint32_t value);
uint32_t rt_get_fcsr(const cpu_t *c);

/* rt_log_unrecompiled: when not NULL, called once per distinct address the interpreter is
 * entered at (a RAM address is reported once until interp_forget_entries; any other address
 * each time, and its fetch then stops the process). */
extern void (*rt_log_unrecompiled)(uint32_t addr);

/* Interrupt check points, the same in recompiled code and the interpreter: at the entry of
 * every function (and of interp_run) except the stub thunks (whose body is the rt_loader
 * call), and before a backward transfer (target at or below the transferring instruction)
 * that stays in the current function or run: a goto in generated C, an inline continue in the
 * interpreter. A transfer that leaves through a call (a direct call, rt_call, rt_jump,
 * rt_loader, rt_host_switch) does not check; the callee's entry does. Where the interpreter
 * runs on inline into another function's start that generated C calls (a branch to it, a
 * fall-through off the end, a jump to an unregistered start), the counts differ by that entry
 * check. Each check counts one unit of the runtime's clock; an interrupt is delivered at the
 * check where slice reaches 0. */
#define RT_CHECK(ram, c)                                                                    \
    do {                                                                                    \
        if (--(c)->slice <= 0)                                                              \
            rt_tick((ram), (c));                                                            \
    } while (0)

/* --- provided by runtime/interp ------------------------------------------------------- */

/* interp_run: run from pc until the function entered there returns (its jr ra), calling
 * through rt_call (runtime/interp/interp.c says how each transfer is handled). It fetches
 * code from ram, so the runtime must keep the EXE image there. A fetch outside RAM (a jump
 * through a null or corrupt pointer, or into the boot ROM) goes to rt_bad_target first. Such
 * a fetch, or one from an unaligned pc, stops the process with a message naming the
 * address, as does an instruction outside the census: a limitation that M2 lifts. */
void interp_run(uint8_t *ram, cpu_t *c, uint32_t pc);
/* interp_forget_entries: forget the entry addresses already given to rt_log_unrecompiled
 * (for test harnesses that run independent cases in one process). */
void interp_forget_entries(void);

/* --- memory ---------------------------------------------------------------------------- */

/* Board/loader fact (docs/analysis/boot-handoff.md, "kuseg is mapped onto RAM"): the resident
 * loader's routine at 0x80004160 sets PageMask to 256 KB pages and writes TLB entries 0-15
 * mapping virtual 0x00000000-0x007FFFFF onto the same physical addresses, every page valid
 * except the even page of entry 0 (0x00000000-0x0003FFFF), and entries 16-47 (virtual 8 MB
 * to 24 MB) with both pages invalid; GAME.EXE writes no TLB entry (no mtc0 in its reached
 * code). MAME 0.276 agrees: GAME.EXE's stores through 0x00237xxx and 0x00317xxx-0x0031Fxxx
 * changed RAM at the same physical addresses (the function-replay captures). So kuseg
 * 0x00040000-0x007FFFFF is RAM; every other kuseg address (a TLB exception on the board) is
 * the bus here. The interpreter's fetch goes through mem_is_ram too.
 *
 * Alignment is not checked: an unaligned load or store is performed (a memcpy in RAM, or a
 * bus access at the unaligned address), where the board would raise an address error. */
#define KUSEG_RAM_START 0x00040000u

/* RAM is kseg0 and kseg1 below RAM_SIZE, and kuseg from KUSEG_RAM_START to RAM_SIZE; for
 * all three, (addr & 0x1FFFFFFF) is the offset into ram. */
static inline int mem_is_ram(uint32_t addr, uint32_t size)
{
    if (addr < 0x80000000u)
        return addr >= KUSEG_RAM_START && addr <= RAM_SIZE - size;
    return addr <= 0xBFFFFFFFu && (addr & 0x1FFFFFFFu) + size <= RAM_SIZE;
}

static inline uint32_t mem_r8(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    if (mem_is_ram(addr, 1))
        return ram[addr & 0x1FFFFFFFu];
    return bus_read(c, addr, 1) & 0xFFu;
}

static inline uint32_t mem_r16(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    if (mem_is_ram(addr, 2)) {
        uint16_t v;
        memcpy(&v, ram + (addr & 0x1FFFFFFFu), 2);
        return v;
    }
    return bus_read(c, addr, 2) & 0xFFFFu;
}

static inline uint32_t mem_r32(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    if (mem_is_ram(addr, 4)) {
        uint32_t v;
        memcpy(&v, ram + (addr & 0x1FFFFFFFu), 4);
        return v;
    }
    return bus_read(c, addr, 4);
}

static inline void mem_w8(uint8_t *ram, cpu_t *c, uint32_t addr, uint32_t value)
{
    if (mem_is_ram(addr, 1))
        ram[addr & 0x1FFFFFFFu] = (uint8_t)value;
    else
        bus_write(c, addr, 1, value & 0xFFu);
}

static inline void mem_w16(uint8_t *ram, cpu_t *c, uint32_t addr, uint32_t value)
{
    if (mem_is_ram(addr, 2)) {
        uint16_t v = (uint16_t)value;
        memcpy(ram + (addr & 0x1FFFFFFFu), &v, 2);
    } else {
        bus_write(c, addr, 2, value & 0xFFFFu);
    }
}

static inline void mem_w32(uint8_t *ram, cpu_t *c, uint32_t addr, uint32_t value)
{
    if (mem_is_ram(addr, 4))
        memcpy(ram + (addr & 0x1FFFFFFFu), &value, 4);
    else
        bus_write(c, addr, 4, value);
}

/* --- integer helpers -------------------------------------------------------------------- */

/* MAME: division by zero leaves hi and lo unchanged. INT_MIN / -1 (which MAME cannot run)
 * gives lo = 0x80000000, hi = 0, as the R5000 does. */
static inline void rc_div(cpu_t *c, uint32_t rs, uint32_t rt)
{
    if (rt == 0)
        return;
    if (rs == 0x80000000u && rt == 0xFFFFFFFFu) {
        c->lo = 0x80000000u;
        c->hi = 0;
        return;
    }
    int32_t a = (int32_t)rs, b = (int32_t)rt;
    c->lo = (uint32_t)(a / b);
    c->hi = (uint32_t)(a % b);
}

static inline void rc_divu(cpu_t *c, uint32_t rs, uint32_t rt)
{
    if (rt == 0)
        return;
    c->lo = rs / rt;
    c->hi = rs % rt;
}

static inline void rc_mult(cpu_t *c, uint32_t rs, uint32_t rt)
{
    uint64_t p = (uint64_t)((int64_t)(int32_t)rs * (int64_t)(int32_t)rt);
    c->lo = (uint32_t)p;
    c->hi = (uint32_t)(p >> 32);
}

static inline void rc_multu(cpu_t *c, uint32_t rs, uint32_t rt)
{
    uint64_t p = (uint64_t)rs * rt;
    c->lo = (uint32_t)p;
    c->hi = (uint32_t)(p >> 32);
}

/* --- FP registers ----------------------------------------------------------------------- */

/* NaNs: FP arithmetic here is plain host arithmetic, and no helper canonicalises NaNs. A NaN
 * an operation generates (0/0, inf - inf, sqrt of a negative) has the host's bits:
 * 0x7FC00000 on arm64, 0xFFC00000 on x86-64; the R5000 (legacy NaN encoding) gives
 * 0x7FBFFFFF. An accepted host divergence. A NaN operand's payload propagates the same on
 * both hosts. */

static inline float rc_fs(const cpu_t *c, unsigned n)
{
    float f;
    memcpy(&f, &c->fpr[n & 31], 4);
    return f;
}

static inline void rc_set_fs(cpu_t *c, unsigned n, float f)
{
    memcpy(&c->fpr[n & 31], &f, 4);
}

/* A double in register n: halves n & ~1 (low) and (n & ~1) + 1 (high). An odd n is not a
 * double register under FR = 0; masking it keeps the access inside fpr (what the R5000 or
 * MAME does with one is not verified, and the reached code is not known to use one). */
static inline double rc_fd(const cpu_t *c, unsigned n)
{
    uint64_t bits = (uint64_t)c->fpr[n & 30] | (uint64_t)c->fpr[(n & 30) + 1] << 32;
    double d;
    memcpy(&d, &bits, 8);
    return d;
}

static inline void rc_set_fd(cpu_t *c, unsigned n, double d)
{
    uint64_t bits;
    memcpy(&bits, &d, 8);
    c->fpr[n & 30] = (uint32_t)bits;
    c->fpr[(n & 30) + 1] = (uint32_t)(bits >> 32);
}

/* --- FP divide -------------------------------------------------------------------------- */

/* div.s, div.d and recip.s (1 / x), in generated code and in the interpreter: the one place
 * an FP division raises the divide-by-zero trap. The game enables that trap: main installs
 * FPU exception handlers through the loader (slot 0x1E8, which also sets the matching FCSR
 * enable), from 0x800C46A0 for cause 5 (handler 0x80150860) and from 0x800C46C0 for cause 3,
 * divide by zero (handler 0x8014FEF0). MAME raises the exception when the enable is set. Here
 * a division of a finite non-zero value by zero (of either sign) with FCSR bit 10 (enable Z)
 * set calls rt_fp_trap with cause bit 15 (Z): the IEEE 754 divide-by-zero case. 0 / 0 (invalid
 * in IEEE 754), inf / 0 (exact) and NaN / 0 do not; what MAME does with them is not measured.
 * Otherwise, or when rt_fp_trap returns, the IEEE result. recip.s is 1 / x, so recip.s of 0
 * divides through rc_div_s too (the M1 re-review's reading of the R5000 manual: it raises Z
 * there; whether MAME raises it for recip.s is not measured). */
#define RC_FCSR_ENABLE_Z (1u << 10)
#define RC_FCSR_CAUSE_Z (1u << 15)

static inline float rc_div_s(cpu_t *c, float a, float b)
{
    if (b == 0.0f && a != 0.0f && isfinite(a) && (c->fcsr & RC_FCSR_ENABLE_Z))
        rt_fp_trap(c, RC_FCSR_CAUSE_Z);
    return a / b;
}

static inline double rc_div_d(cpu_t *c, double a, double b)
{
    if (b == 0.0 && a != 0.0 && isfinite(a) && (c->fcsr & RC_FCSR_ENABLE_Z))
        rt_fp_trap(c, RC_FCSR_CAUSE_Z);
    return a / b;
}

/* --- FP conversions --------------------------------------------------------------------- */

/* MAME: a NaN or a value outside int32 gives 0x80000000 (the x86 conversion result). */
static inline uint32_t rc_trunc_w_s(float f)
{
    if (f >= -2147483648.0f && f < 2147483648.0f)
        return (uint32_t)(int32_t)f;
    return 0x80000000u;
}

static inline uint32_t rc_trunc_w_d(double d)
{
    if (d > -2147483649.0 && d < 2147483648.0)
        return (uint32_t)(int32_t)d;
    return 0x80000000u;
}

static inline float rc_sqrt_s(float f)
{
    return sqrtf(f);
}

/* --- FP condition codes and compares ---------------------------------------------------- */

static inline int rc_cc(const cpu_t *c, unsigned cc)
{
    return (c->fcc >> (cc & 7)) & 1;
}

static inline void rc_set_cc(cpu_t *c, unsigned cc, int cond)
{
    uint8_t bit = (uint8_t)(1u << (cc & 7));
    c->fcc = (uint8_t)(cond ? c->fcc | bit : c->fcc & ~bit);
}

/* An unordered pair (a NaN) compares false. MAME raises no exception for it. */
static inline void rc_c_eq_s(cpu_t *c, unsigned cc, float a, float b) { rc_set_cc(c, cc, a == b); }
static inline void rc_c_lt_s(cpu_t *c, unsigned cc, float a, float b) { rc_set_cc(c, cc, a < b); }
static inline void rc_c_le_s(cpu_t *c, unsigned cc, float a, float b) { rc_set_cc(c, cc, a <= b); }
static inline void rc_c_eq_d(cpu_t *c, unsigned cc, double a, double b) { rc_set_cc(c, cc, a == b); }
static inline void rc_c_lt_d(cpu_t *c, unsigned cc, double a, double b) { rc_set_cc(c, cc, a < b); }
static inline void rc_c_le_d(cpu_t *c, unsigned cc, double a, double b) { rc_set_cc(c, cc, a <= b); }

/* --- the multiply-add family ------------------------------------------------------------ */

/* MAME: the product is rounded to the format, then the sum (two roundings; never fused, so
 * two statements); nmadd and nmsub negate the rounded sum. Operands in assembler order:
 * madd.fmt fd, fr, fs, ft is fd = fs * ft + fr. */
static inline float rc_madd_s(float fr, float fs, float ft)
{
    float p = fs * ft;
    return p + fr;
}

static inline float rc_msub_s(float fr, float fs, float ft)
{
    float p = fs * ft;
    return p - fr;
}

static inline float rc_nmadd_s(float fr, float fs, float ft)
{
    float p = fs * ft;
    float s = p + fr;
    return -s;
}

static inline float rc_nmsub_s(float fr, float fs, float ft)
{
    float p = fs * ft;
    float s = p - fr;
    return -s;
}

static inline double rc_madd_d(double fr, double fs, double ft)
{
    double p = fs * ft;
    return p + fr;
}

#endif
