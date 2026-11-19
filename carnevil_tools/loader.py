"""Facts about the boot ROM's resident loader: where it lies, its service slots, its
device-driver table and what each slot reaches (docs/analysis/boot-handoff.md and
docs/analysis/hardware-usage.md)."""
import bisect
import hashlib
import tomllib

from carnevil_tools import exe, functions, mips

BOOT_ROM_SHA256 = "05346e09d90a8e5aadc5e0aae8bd53b80816e96ea1e649ba548e4c6b07dac4a6"
LOADER_ROM = (0xF20, 0x16B60)  # file offsets of the loader in boot.u32
LOADER_BASE = 0x80000000
LOADER_ENTRY = 0x80004000
SLOTS = 72
VECTORS = (0x000, 0x080, 0x100, 0x180)


def loader_image(path):
    """The loader as it runs: ROM bytes 0xF20-0x16B60 at 0x80000000 (boot-handoff.md)."""
    with open(path, "rb") as f:
        raw = f.read()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != BOOT_ROM_SHA256:
        raise exe.UnknownRevisionError(
            "unknown boot ROM: expected sha256 %s, got %s" % (BOOT_ROM_SHA256, digest))
    lo, hi = LOADER_ROM
    return exe.ExeImage(0, LOADER_BASE, raw[lo:hi], digest)


def game_slots(boot_state_path):
    """The loader slots GAME.EXE calls, sorted: the `slot` of each `external_call` in
    config/boot_state.toml."""
    with open(boot_state_path, "rb") as f:
        calls = tomllib.load(f)["external_call"]
    return sorted({c["slot"] for c in calls if "slot" in c})


def slot_targets(image):
    """Slot offset -> the routine its `j` jumps to, for the 72 slots at 0x80000000."""
    out = {}
    for k in range(SLOTS):
        addr = image.load_addr + 8 * k
        insn = mips.decode(image.word(addr), addr)
        if insn.mnemonic != "j":
            raise ValueError("slot 0x%03X is not a j" % (8 * k))
        out[8 * k] = insn.target
    return out


def analyse_loader(image):
    """Function discovery over the loader from its entry, the 72 slot targets, the four
    exception vectors and the routines of its device-driver table (reached only through
    that table, so discovery would otherwise guess their starts from prologues)."""
    manual = set(slot_targets(image).values()) | {LOADER_BASE + v for v in VECTORS}
    for _name, routines in loader_devices(image):
        manual.update(routines.values())
    return functions.discover(image, LOADER_ENTRY, sorted(manual))


def call_graph(image, analysis):
    """Function start -> starts it reaches by a direct call (jal/bal) or a `j` out of
    itself, and the set of functions holding an indirect call (jalr) or unresolved jr."""
    starts = sorted(f.addr for f in analysis.functions)
    edges, indirect = {}, set()
    tables = {t.jr_addr for t in analysis.jump_tables}
    insn = mips.Decoder(image).insn
    for start, words in functions.reached_words(analysis).items():
        out = edges.setdefault(start, set())
        for a in words:
            i = insn(a)
            if i.kind == "call_reg" or (i.kind == "jump_reg" and i.rs != 31
                                        and a not in tables):
                indirect.add(start)
            target = i.target
            if target is None or i.kind not in ("call", "jump"):
                continue
            if i.kind == "jump" and target in words:
                continue
            k = bisect.bisect_right(starts, target) - 1
            if k >= 0:
                out.add(starts[k])
    return edges, indirect


DEVICE_TABLE = 0x80013530  # the loader's device drivers: 7 routines, then a name pointer
DEVICE_SLOTS = ("open", "close", "read", "write", "init", "unused", "ioctl")
NO_OP = 0x80006984  # the loader's shared "return 0" routine in unused driver slots


def loader_devices(image):
    """[(name, {slot: routine})] of the loader's driver table, empty slots left out."""
    out = []
    addr = DEVICE_TABLE
    while image.contains(addr + 28):
        routines = [image.word(addr + 4 * k) for k in range(7)]
        name_ptr = image.word(addr + 28)
        if not any(routines) or not image.contains(name_ptr):
            break
        name = bytearray()
        a = name_ptr
        while image.contains(a) and image.data[a - image.load_addr]:
            name.append(image.data[a - image.load_addr])
            a += 1
        out.append((name.decode("ascii"), {s: r for s, r in zip(DEVICE_SLOTS, routines)
                                           if r != NO_OP}))
        addr += 32
    return out


def loader_reach(image, analysis, game_slots):
    """Function start -> labels of what reaches it through direct calls and tail jumps:
    "game" (one of the slots in game_slots), "vector" (an exception vector: runs while the
    game runs), "slot" (one of the other slots), "boot" (the loader's entry), and
    "driver:<name>" (a routine of that device driver, which the file slots call through
    the driver table)."""
    edges, _indirect = call_graph(image, analysis)
    slots = slot_targets(image)
    roots = {"game": [slots[s] for s in game_slots],
             "vector": [LOADER_BASE + v for v in VECTORS] + [slots[v] for v in VECTORS],
             "slot": [t for s, t in slots.items() if s not in game_slots and s not in VECTORS],
             "boot": [LOADER_ENTRY]}
    for name, routines in loader_devices(image):
        roots["driver:" + name] = list(routines.values())
    out = {}
    for label, starts in roots.items():
        for f in closure(edges, starts):
            out.setdefault(f, set()).add(label)
    return out


def closure(edges, roots):
    seen, stack = set(), list(roots)
    while stack:
        f = stack.pop()
        if f in seen:
            continue
        seen.add(f)
        stack.extend(edges.get(f, ()))
    return seen
