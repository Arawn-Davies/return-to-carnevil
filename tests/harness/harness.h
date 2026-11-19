/* harness.h: what a C file linked into the test harness (tests/harness/harness.c) uses. */
#ifndef HARNESS_H
#define HARNESS_H

#include "recomp.h"

/* Registers fn as the recompiled function at addr, for recomp_lookup; typically called from
 * a constructor in the linked file. Registering an address again replaces its function. */
void harness_register(uint32_t addr, recomp_fn fn);

#endif
