# Build the recompiled game (docs/tools.md, "make"). GNU make 3.81 or later.
#
#   make recomp      python3 -m carnevil_tools recompile $(GAME_EXE) into $(GENERATED)
#   make linkcheck   compile the generated C in parallel and link it with the runtime and the
#                    stub runtime into $(BUILD)/game_stub; refuses a folder without
#                    manifest.toml. Nothing is rebuilt when nothing changed.
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

C_FLAGS := -std=c11 -O2 -ffp-contract=off -frounding-math -fno-strict-aliasing -fwrapv
RUNTIME_WARNINGS := -Wall -Wextra -Werror
GENERATED_WARNINGS := -Wall -Werror -Wno-unused-label -Wno-unused-variable \
                      -Wno-unused-but-set-variable
INCLUDE := $(ROOT)/runtime/include

RUNTIME_SRCS := $(ROOT)/runtime/core/call.c $(ROOT)/runtime/core/fpu.c \
                $(ROOT)/runtime/interp/interp.c $(ROOT)/runtime/stub/stub.c
RUNTIME_OBJS := $(patsubst $(ROOT)/runtime/%.c,$(BUILD)/runtime/%.o,$(RUNTIME_SRCS))
GENERATED_SRCS := $(sort $(wildcard $(GENERATED)/funcs_*.c)) $(GENERATED)/func_table.c
GENERATED_OBJS := $(patsubst $(GENERATED)/%.c,$(BUILD)/generated/%.o,$(GENERATED_SRCS))

.PHONY: recomp linkcheck test
.DELETE_ON_ERROR:

recomp:
	cd "$(ROOT)" && $(PYTHON) -m carnevil_tools recompile "$(GAME_EXE)" -o "$(GENERATED)" \
	    --functions "$(FUNCTIONS)"

linkcheck:
	@test -f "$(GENERATED)/manifest.toml" || { \
	    echo "error: $(GENERATED)/manifest.toml not found: $(GENERATED) is not a complete" \
	         "recompile output (run make recomp)" >&2; exit 1; }
	@$(MAKE) --no-print-directory -f "$(ROOT)/Makefile" -j $(JOBS) "$(BUILD)/game_stub"

test:
	cd "$(ROOT)" && $(PYTHON) -W error -m unittest

$(BUILD)/game_stub: $(GENERATED_OBJS) $(RUNTIME_OBJS)
	$(CC) $^ -lm -o $@

$(BUILD)/generated/%.o: $(GENERATED)/%.c $(GENERATED)/funcs.h $(INCLUDE)/recomp.h
	@mkdir -p "$(@D)"
	$(CC) $(C_FLAGS) $(GENERATED_WARNINGS) -I "$(INCLUDE)" -I "$(GENERATED)" -c $< -o $@

$(BUILD)/runtime/%.o: $(ROOT)/runtime/%.c $(INCLUDE)/recomp.h
	@mkdir -p "$(@D)"
	$(CC) $(C_FLAGS) $(RUNTIME_WARNINGS) -I "$(INCLUDE)" -c $< -o $@
