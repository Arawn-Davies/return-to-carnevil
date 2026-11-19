/* harness.h: what a C file linked into the test harness (tests/harness/harness.c) uses. */
#ifndef HARNESS_H
#define HARNESS_H

#include "recomp.h"

/* Registers fn as the recompiled function at addr, for recomp_lookup; typically called from
 * a constructor in the linked file. Registering an address again replaces its function. */
void harness_register(uint32_t addr, recomp_fn fn);

/* Registers fn under name for "recomp" mode, where a case's "fn" line selects it. name must
 * outlive the process (a string literal). Registering a name again replaces its function. */
void harness_register_named(const char *name, recomp_fn fn);

#endif
