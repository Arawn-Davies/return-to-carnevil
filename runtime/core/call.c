/* call.c: calls and jumps to an address, recompiled code first, the interpreter otherwise. */
#include "recomp.h"

void (*rt_log_unrecompiled)(uint32_t addr) = 0;

void rt_call(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    recomp_fn fn = recomp_lookup(addr);
    if (fn)
        fn(ram, c);
    else
        interp_run(ram, c, addr);
}

void rt_jump(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    rt_call(ram, c, addr);
}
