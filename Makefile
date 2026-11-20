# Build the recompiled game (docs/tools.md, "make"). GNU make 3.81 or later.
#
#   make recomp      python3 -m carnevil_tools recompile $(GAME_EXE) into $(GENERATED)
#   make linkcheck   compile the generated C in parallel and link it with the runtime and the
#                    stub runtime into $(BUILD)/game_stub; refuses a folder without
#                    manifest.toml. Nothing is rebuilt when nothing changed.
#   make smoke       the same objects with the runtime and runtime/stub/smoke.c into
#                    $(BUILD)/smoke: GAME.EXE from its entry to the first loader call
#   make replay      ... with runtime/stub/replay.c into $(BUILD)/replay: one function
#                    from a state captured in MAME (tools/mame/README.md)
#   make test        the unit tests
#
# Generated C and objects never go in this repository: by default they are on the share.

ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

CARNEVIL_WORK ?= /Volumes/archive/reincarnevilation/work
GAME_EXE ?= $(CARNEVIL_WORK)/part0_files/GAME.EXE
FUNCTIONS ?= $(ROOT)/config/game.functions.toml
GENERATED ?= $(CARNEVIL_WORK)/generated
BUILD ?= $(CARNEVIL_WORK)/build/$(shell uname -s)-$(shell uname -m)
PYTHON ?= python3
JOBS ?= $(shell getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)

# make splits target and prerequisite names at spaces, so these paths must not contain one
ifneq ($(words $(CARNEVIL_WORK)) $(words $(GENERATED)) $(words $(BUILD)),1 1 1)
$(error CARNEVIL_WORK, GENERATED and BUILD must not contain a space; use a path without one)
endif

C_FLAGS := -std=c11 -O2 -ffp-contract=off -frounding-math -fno-strict-aliasing -fwrapv
RUNTIME_WARNINGS := -Wall -Wextra -Werror
GENERATED_WARNINGS := -Wall -Werror -Wno-unused-label -Wno-unused-variable \
                      -Wno-unused-but-set-variable
INCLUDE := $(ROOT)/runtime/include

CORE_SRCS := $(ROOT)/runtime/core/call.c $(ROOT)/runtime/core/fpu.c \
             $(ROOT)/runtime/interp/interp.c
CORE_OBJS := $(patsubst $(ROOT)/runtime/%.c,$(BUILD)/runtime/%.o,$(CORE_SRCS))
# the disk image, its overlay and the file system (runtime/disk/disk.h), for the runtime
DISK_SRCS := $(ROOT)/runtime/disk/image.c $(ROOT)/runtime/disk/fs.c \
             $(ROOT)/runtime/disk/sha256.c
DISK_OBJS := $(patsubst $(ROOT)/runtime/%.c,$(BUILD)/runtime/%.o,$(DISK_SRCS))
# the virtual clock, interrupt and exception dispatch (runtime/include/machine.h), for the
# runtime
MACHINE_SRCS := $(ROOT)/runtime/core/clock.c $(ROOT)/runtime/core/irq.c \
                $(ROOT)/runtime/core/exc.c
MACHINE_OBJS := $(patsubst $(ROOT)/runtime/%.c,$(BUILD)/runtime/%.o,$(MACHINE_SRCS))
# the host contexts of the game's tasks and the glue rt_host_switch (runtime/core/tasks.h),
# for the runtime; compiled with -pthread, and a program linking them links with -pthread
TASKS_SRCS := $(ROOT)/runtime/core/tasks.c
TASKS_OBJS := $(patsubst $(ROOT)/runtime/%.c,$(BUILD)/runtime/%.o,$(TASKS_SRCS))
# the loader's stand-ins: descriptors, files, console, CMOS, PIC, timers, IOASIC, the
# GT64010 and its DMA channel 0, the PCI and disk slots and the handler registrations
# (runtime/loader/loader.h), for the runtime; they need DISK_OBJS, MACHINE_OBJS and HW_OBJS
LOADER_SRCS := $(addprefix $(ROOT)/runtime/loader/,slots.c fd.c dev_file.c dev_con.c \
               dev_cmos.c dev_pic.c dev_timer.c dev_ioasic.c dev_sysctrl.c diskslots.c)
LOADER_OBJS := $(patsubst $(ROOT)/runtime/%.c,$(BUILD)/runtime/%.o,$(LOADER_SRCS))
# the bus and the hardware GAME.EXE touches directly: the Voodoo (registers, frame buffer,
# texture unit), the gun, the sound FIFO and 0x17F00000 (runtime/hw/hw.h), for the runtime;
# they need LOADER_OBJS (cmos_byte) and MACHINE_OBJS
HW_SRCS := $(ROOT)/runtime/core/bus.c $(addprefix $(ROOT)/runtime/hw/,voodoo_null.c \
           voodoo_fb.c voodoo_tmu.c gun.c misc.c)
HW_OBJS := $(patsubst $(ROOT)/runtime/%.c,$(BUILD)/runtime/%.o,$(HW_SRCS))
GENERATED_SRCS := $(sort $(wildcard $(GENERATED)/funcs_*.c)) $(GENERATED)/func_table.c
GENERATED_OBJS := $(patsubst $(GENERATED)/%.c,$(BUILD)/generated/%.o,$(GENERATED_SRCS))

.PHONY: recomp linkcheck smoke replay test
.DELETE_ON_ERROR:

recomp:
	cd "$(ROOT)" && $(PYTHON) -m carnevil_tools recompile "$(GAME_EXE)" -o "$(GENERATED)" \
	    --functions "$(FUNCTIONS)"

linkcheck smoke replay:
	@test -f "$(GENERATED)/manifest.toml" || { \
	    echo "error: $(GENERATED)/manifest.toml not found: $(GENERATED) is not a complete" \
	         "recompile output (run make recomp)" >&2; exit 1; }
	@$(MAKE) --no-print-directory -f "$(ROOT)/Makefile" -j $(JOBS) \
	    "$(BUILD)/$(if $(filter linkcheck,$@),game_stub,$@)"

test:
	cd "$(ROOT)" && $(PYTHON) -W error -m unittest

$(BUILD)/game_stub: $(GENERATED_OBJS) $(CORE_OBJS) $(BUILD)/runtime/stub/stub.o
	$(CC) $^ -lm -o $@

$(BUILD)/smoke: $(GENERATED_OBJS) $(CORE_OBJS) $(BUILD)/runtime/stub/smoke.o
	$(CC) $^ -lm -o $@

# replay provides rt_call and rt_jump itself (its --interp mode), in place of call.c
$(BUILD)/replay: $(GENERATED_OBJS) $(filter-out %/call.o,$(CORE_OBJS)) $(BUILD)/runtime/stub/replay.o
	$(CC) $^ -lm -o $@

$(BUILD)/runtime/stub/smoke.o $(BUILD)/runtime/stub/replay.o: $(ROOT)/runtime/stub/common.h
$(DISK_OBJS): $(ROOT)/runtime/disk/disk.h
$(MACHINE_OBJS): $(INCLUDE)/machine.h
$(TASKS_OBJS): $(INCLUDE)/machine.h $(ROOT)/runtime/core/tasks.h
$(TASKS_OBJS): C_FLAGS += -pthread
$(LOADER_OBJS): $(ROOT)/runtime/loader/loader.h $(INCLUDE)/machine.h $(ROOT)/runtime/disk/disk.h \
                $(ROOT)/runtime/hw/hw.h
$(HW_OBJS): $(ROOT)/runtime/hw/hw.h $(ROOT)/runtime/loader/loader.h $(INCLUDE)/machine.h

$(BUILD)/generated/%.o: $(GENERATED)/%.c $(GENERATED)/funcs.h $(INCLUDE)/recomp.h
	@mkdir -p "$(@D)"
	$(CC) $(C_FLAGS) $(GENERATED_WARNINGS) -I "$(INCLUDE)" -I "$(GENERATED)" -c $< -o $@

$(BUILD)/runtime/%.o: $(ROOT)/runtime/%.c $(INCLUDE)/recomp.h
	@mkdir -p "$(@D)"
	$(CC) $(C_FLAGS) $(RUNTIME_WARNINGS) -I "$(INCLUDE)" -c $< -o $@
