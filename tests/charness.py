"""Build the C test harness (tests/harness/harness.c with the runtime) and run cases in it.

`build(sources)` compiles the harness, runtime/core and runtime/interp, plus `sources` (C
files a later task links in, e.g. generated functions), with the global C flags and the
sanitizer flags, into a cache directory under the system temp dir keyed by a hash of every
input and flag; nothing is written into the repo. `run(exe, mode, cases)` feeds the cases to
the binary and returns their after-states. The wire format is described at the top of
tests/harness/harness.c. Modes: "interp" runs each case in the reference interpreter;
"recomp" calls, per case, the linked C function that `functions` names for it (see
`wrap_function`, `register_named` and `write_generated`, which build the C file to link).

Each returned `vectors.State` also carries, as attributes outside its dataclass fields (so
equality with a recorded State is unaffected):
- `calls`: the runtime calls in order, as tuples ("bus_read", addr, size),
  ("bus_write", addr, size, value), ("loader", slot), ("break", pc, code), ("interrupt",);
- `unrecompiled`: the addresses `rt_log_unrecompiled` reported, in order.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

from carnevil_tools import vectors

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INCLUDE = os.path.join(REPO, "runtime", "include")
HARNESS = os.path.join(REPO, "tests", "harness", "harness.c")
RUNTIME = [os.path.join(REPO, "runtime", "core", "call.c"),
           os.path.join(REPO, "runtime", "core", "fpu.c"),
           os.path.join(REPO, "runtime", "interp", "interp.c")]

CFLAGS = ("-std=c11", "-O2", "-ffp-contract=off", "-frounding-math", "-fno-strict-aliasing",
          "-fwrapv")
RUNTIME_WARNINGS = ("-Wall", "-Wextra", "-Werror")
GENERATED_WARNINGS = ("-Wall", "-Werror", "-Wno-unused-label", "-Wno-unused-variable",
                      "-Wno-unused-but-set-variable")
SANITIZE = ("-fsanitize=undefined", "-fno-sanitize-recover=undefined")

BUS_READ_VALUE = 0xDEADBEEF  # what the harness bus returns, masked to the access size

CACHE = os.path.join(tempfile.gettempdir(), "carnevil-charness")


class BuildError(Exception):
    pass


class HarnessError(Exception):
    pass


def _cc():
    cc = shutil.which("cc")
    if cc is None:
        raise unittest.SkipTest("no C compiler: `cc` is not on PATH")
    return cc


def _inputs(sources):
    """Every file the build reads: the runtime tree, the harness, and `sources`."""
    files = []
    for root in (os.path.join(REPO, "runtime"), os.path.dirname(HARNESS)):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            files += [os.path.join(dirpath, f) for f in sorted(filenames)
                      if f.endswith((".c", ".h"))]
    return files + [os.path.abspath(s) for s in sources]


def _key(cc, sources, extra_flags):
    h = hashlib.sha256()
    st = os.stat(os.path.realpath(cc))
    h.update(f"{os.path.realpath(cc)}\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
    h.update("\0".join(CFLAGS + RUNTIME_WARNINGS + GENERATED_WARNINGS + SANITIZE
                       + tuple(extra_flags)).encode())
    h.update(b"\0sources\0" + "\0".join(os.path.abspath(s) for s in sources).encode())
    for path in _inputs(sources):
        with open(path, "rb") as f:
            h.update(f"\0{path}\0".encode() + hashlib.sha256(f.read()).digest())
    return h.hexdigest()[:32]


def _cc_run(argv):
    res = subprocess.run(argv, capture_output=True, text=True)
    if res.returncode != 0:
        raise BuildError(" ".join(argv) + "\n" + res.stdout + res.stderr)


def build(sources, extra_flags=()):
    """Path of the harness binary built from the harness, the runtime and `sources`.
    Raises unittest.SkipTest when there is no `cc`, BuildError when compiling fails."""
    cc = _cc()
    sources = list(sources)
    out = os.path.join(CACHE, _key(cc, sources, extra_flags))
    exe = os.path.join(out, "harness")
    if os.path.exists(exe):
        return exe
    os.makedirs(CACHE, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=CACHE, prefix="build-")
    try:
        objs = []
        units = [(s, RUNTIME_WARNINGS) for s in RUNTIME + [HARNESS]] + \
                [(s, GENERATED_WARNINGS) for s in sources]
        for i, (src, warnings) in enumerate(units):
            obj = os.path.join(tmp, f"{i}-{os.path.basename(src)}.o")
            _cc_run([cc, *CFLAGS, *warnings, *SANITIZE, *extra_flags, "-I", INCLUDE,
                     "-I", os.path.dirname(HARNESS), "-c", src, "-o", obj])
            objs.append(obj)
        _cc_run([cc, *SANITIZE, *extra_flags, *objs, "-lm", "-o", os.path.join(tmp, "harness")])
        try:
            os.rename(tmp, out)
        except OSError:  # another process built the same key first
            if not os.path.exists(exe):
                raise
    finally:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
    return exe


def make_case(name, *words, r=None, fpr=None, fcsr=0, mem=None):
    """A case program at vectors.CODE_BASE: `words`, then `jr ra; nop`, entered with
    ra = vectors.RA; all-zero state apart from `r` (register -> value), `fpr`, `fcsr`, `mem`."""
    regs = [0] * 32
    regs[31] = vectors.RA
    for reg, value in (r or {}).items():
        regs[reg] = value
    fp = [0] * 32
    for reg, value in (fpr or {}).items():
        fp[reg] = value
    state = vectors.State(regs, 0, 0, fp, fcsr, dict(mem or {}))
    return vectors.Case(name, list(words) + [vectors.assemble("jr", rs=31), 0], state)


def first_difference(want, got):
    """The first field in which two states differ, as text, or None."""
    for i, (u, v) in enumerate(zip(want.r, got.r)):
        if u != v:
            return f"r[{i}]: want {u:08X} got {v:08X}"
    for name in ("hi", "lo"):
        u, v = getattr(want, name), getattr(got, name)
        if u != v:
            return f"{name}: want {u:08X} got {v:08X}"
    for i, (u, v) in enumerate(zip(want.fpr, got.fpr)):
        if u != v:
            return f"fpr[{i}]: want {u:08X} got {v:08X}"
    if want.fcsr != got.fcsr:
        return f"fcsr: want {want.fcsr:08X} got {got.fcsr:08X}"
    for addr in sorted(set(want.mem) | set(got.mem)):
        u, v = want.mem.get(addr, 0), got.mem.get(addr, 0)
        if u != v:
            return f"mem[{addr:08X}]: want {u:08X} got {v:08X}"
    return None


def wrap_function(name, statements):
    """C source of `void name(uint8_t *ram, cpu_t *c) { ... }` whose body is `statements`
    (strings, one per line). Not static, so it can be registered from the same file or not."""
    body = "".join(f"    {line}\n" for line in statements)
    return f"void {name}(uint8_t *ram, cpu_t *c)\n{{\n    (void)ram;\n    (void)c;\n{body}}}\n"


def register_named(names):
    """C source of a constructor that registers each function `names` for "recomp" mode,
    under its own name (the functions must be defined above it)."""
    lines = "".join(f'    harness_register_named("{n}", {n});\n' for n in names)
    return ("static void __attribute__((constructor)) register_all(void)\n{\n"
            f"{lines}}}\n")


def write_generated(source):
    """Write C `source`, headed by an include of harness.h, to a path derived from its content
    (so the build cache stays valid between runs) under the cache directory; returns the path."""
    text = '#include "harness.h"\n\n' + source
    path = os.path.join(CACHE, "generated",
                        hashlib.sha256(text.encode()).hexdigest()[:32] + ".c")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    return path


def _hex(v):
    return f"{v & 0xFFFFFFFF:08X}"


def _input(cases, functions):
    lines = []
    for case in cases:
        b = case.before
        lines.append(f"case {case.name}")
        if functions is not None:
            lines.append(f"fn {functions[case.name]}")
        lines.append("r " + " ".join(_hex(v) for v in b.r))
        lines.append(f"hilo {_hex(b.hi)} {_hex(b.lo)}")
        lines.append("fpr " + " ".join(_hex(v) for v in b.fpr))
        lines.append(f"fcsr {_hex(b.fcsr)}")
        irq = getattr(case, "irq_pending", 0)
        if irq:
            lines.append(f"irq {_hex(irq)}")
        for i, word in enumerate(case.words):
            lines.append(f"mem {_hex(vectors.CODE_BASE + 4 * i)} {_hex(word)}")
        for addr, value in sorted(b.mem.items()):
            lines.append(f"mem {_hex(addr)} {_hex(value)}")
        lines.append(f"run {_hex(vectors.CODE_BASE)}")
    return "\n".join(lines) + "\n"


def _parse(text):
    states, cur = [], None
    for line in text.splitlines():
        key, *f = line.split()
        if key == "case":
            cur = {"name": f[0], "calls": [], "unrecompiled": [], "mem": {}}
        elif key == "call":
            cur["calls"].append((f[0],) + tuple(int(x, 16) for x in f[1:]))
        elif key == "unrecompiled":
            cur["unrecompiled"].append(int(f[0], 16))
        elif key == "mem":
            cur["mem"][int(f[0], 16)] = int(f[1], 16)
        elif key in ("r", "fpr", "hilo", "fcsr"):
            cur[key] = [int(x, 16) for x in f]
        elif key == "end":
            state = vectors.State(cur["r"], cur["hilo"][0], cur["hilo"][1], cur["fpr"],
                                  cur["fcsr"][0], cur["mem"])
            state.name = cur["name"]
            state.calls = cur["calls"]
            state.unrecompiled = cur["unrecompiled"]
            states.append(state)
            cur = None
        else:
            raise HarnessError(f"harness output: unexpected line {line!r}")
    return states


def run(exe, mode, cases, functions=None):
    """After-states of `cases` run in the harness in `mode` ("interp" or "recomp"), in order.
    `functions` maps a case's name to the name of its linked C function (registered with
    `register_named`); "recomp" mode needs it, "interp" mode ignores it. Raises HarnessError,
    with the harness's message, when it exits non-zero."""
    cases = list(cases)
    res = subprocess.run([exe, mode], input=_input(cases, functions), capture_output=True,
                         text=True)
    if res.returncode != 0:
        raise HarnessError(f"harness exited with status {res.returncode}: {res.stderr.strip()}")
    states = _parse(res.stdout)
    if [s.name for s in states] != [c.name for c in cases]:
        raise HarnessError("harness output does not match its input cases")
    return states
