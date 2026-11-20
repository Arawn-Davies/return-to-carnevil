/* trace.h: what the runtime's main observes of a run (trace.c), and the trace file it writes.
 *
 * The observers are hooks on the runtime's components (the console, rt_loader, the task
 * switch, the interpreter's entries, VBLANK, rt_stop). They are installed on every run,
 * traced or not, and none of them changes guest state: they read registers and RAM (RAM only
 * through mem_r8 and mem_r32 at addresses mem_is_ram accepts, so never the bus) and write
 * only the trace file. A run therefore executes the same guest instructions with and without
 * --trace.
 */
#ifndef CARNEVIL_TRACE_H
#define CARNEVIL_TRACE_H

#include <stddef.h>
#include <stdint.h>

#include "recomp.h"

/* trace_open: the trace file at path (created or truncated); 0, or -1 with the reason in err.
 * Without it the observers count but write nothing. */
int trace_open(const char *path, char *err, size_t errlen);

/* trace_attach: installs the observers on the one guest state; frames, when non-zero, ends
 * the run (trace_end) at the frames-th VBLANK interrupt; quiet (--quiet) leaves out
 * trace_end's line on stderr. Registers trace_close with rt_at_stop. */
void trace_attach(uint8_t *ram, cpu_t *c, uint64_t frames, int quiet);

/* trace_end: the end of a run that reached its limit: an "end" line with how ("seconds N",
 * "frames N"), unless quiet a line on stderr with the time and the SHA-256 of RAM, then
 * rt_exit(0) (which runs trace_close). */
_Noreturn void trace_end(const char *how);

/* trace_close: the counts of the run and the tasks alive, then the file is closed. */
void trace_close(void);

#endif
