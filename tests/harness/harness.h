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

/* Registers pc as a host-switch point of kind (RT_SWITCH_*), for rt_switch_kind; typically
 * called from a constructor in the linked file. Registering a pc again replaces its kind. */
void harness_register_switch(uint32_t pc, int kind);

#endif
