"""The machine state at GAME.EXE's entry, from config/boot_state.toml, as a C header for the
runtime's main (runtime/main/carnevil.c): `python3 -m carnevil_tools bootstate`.

The header defines BOOT_ENTRY (the entry and load address), BOOT_STATUS (CP0 Status),
BOOT_FCSR (FCSR as ctc1 writes it), BOOT_HI and BOOT_LO, and BOOT_GPR, an initialiser of 32
words for r0 to r31; every register the file does not list is 0. It is generated output:
`make carnevil` writes it into the generated folder, and it is never committed.

Only the file's top-level values are machine state; its tables ([first_loader_call], the
[[ram_preload]] ranges, the [[external_call]] list) are evidence and are not used: the runtime
places nothing below the image (boot-handoff.md, "Decisions for the runtime", item 4). Config
(the K0 field, `config`) is read but has no counterpart in the runtime, which models no cache;
it is named in a comment. A top-level key that is none of these is refused rather than
ignored, so a new fact in the file cannot be dropped without someone deciding what it means.
"""
import os
import tomllib

REGISTERS = ("zero at v0 v1 a0 a1 a2 a3 t0 t1 t2 t3 t4 t5 t6 t7 s0 s1 s2 s3 s4 s5 s6 s7 t8 t9 "
             "k0 k1 gp sp fp ra").split()
# top-level keys that are not general registers
SPECIAL = ("entry", "status", "fcr31", "config", "hi", "lo")


class BootStateError(ValueError):
    pass


def read(path):
    """{"entry", "status", "fcsr", "config", "hi", "lo": int, "gpr": [32 ints]} from the TOML
    file at path. Raises BootStateError for a missing entry, status or fcr31, a register
    value outside 32 bits, a value for r0, or an unknown top-level key; OSError and
    tomllib.TOMLDecodeError as the reading raises them."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    values = {k: v for k, v in data.items() if not isinstance(v, (dict, list))}
    for key, value in values.items():
        if key not in SPECIAL and key not in REGISTERS:
            raise BootStateError(f"{path}: unknown key {key!r}: not a register or one of "
                                 f"{', '.join(SPECIAL)}")
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFFFFFF:
            raise BootStateError(f"{path}: {key} = {value!r} is not a 32-bit value")
    for key in ("entry", "status", "fcr31"):
        if key not in values:
            raise BootStateError(f"{path}: no {key}")
    if "zero" in values:
        raise BootStateError(f"{path}: r0 (zero) is always 0 and cannot be given")
    return {"entry": values["entry"], "status": values["status"], "fcsr": values["fcr31"],
            "config": values.get("config"), "hi": values.get("hi", 0),
            "lo": values.get("lo", 0), "gpr": [values.get(r, 0) for r in REGISTERS]}


def header(state, source="config/boot_state.toml"):
    """The text of boot_state.h for a state from read()."""
    def u(v):
        return f"0x{v:08X}u"
    gpr = "".join(f" \\\n    {u(v)}{',' if i < 31 else ''} /* {name} */"
                  for i, (name, v) in enumerate(zip(REGISTERS, state["gpr"])))
    config = ("not given" if state["config"] is None else
              f"0x{state['config']:08X} there, which the runtime does not model (no cache)")
    return (f"/* boot_state.h: the machine state at GAME.EXE's entry, written by `python3 -m "
            f"carnevil_tools\n * bootstate` from {source}; generated output, never edited "
            f"or committed. Config is\n * {config}. */\n"
            "#ifndef BOOT_STATE_H\n#define BOOT_STATE_H\n\n"
            f"#define BOOT_ENTRY {u(state['entry'])}\n"
            f"#define BOOT_STATUS {u(state['status'])}\n"
            f"#define BOOT_FCSR {u(state['fcsr'])}\n"
            f"#define BOOT_HI {u(state['hi'])}\n"
            f"#define BOOT_LO {u(state['lo'])}\n"
            f"/* r0 to r31 */\n#define BOOT_GPR {{{gpr} \\\n}}\n\n#endif\n")


def write_header(path, out):
    """Reads the TOML file at path and writes boot_state.h to out (through a temporary file
    next to it and a rename, so a failed run leaves no half header)."""
    text = header(read(path), os.path.basename(os.path.dirname(os.path.abspath(path))) + "/"
                  + os.path.basename(path))
    tmp = f"{out}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, out)
