"""Command-line entry point: python3 -m carnevil_tools <command>."""
import argparse
import os
import sys

from carnevil_tools import board, disk, exe, functions, hwcensus, loader, mips, recomp, vectors

DEFAULT_WORK = "/Volumes/archive/reincarnevilation/work"


def _missing(path):
    if os.path.isdir(path):
        print(f"error: {path} is not a file", file=sys.stderr)
        return 2
    work = os.environ.get("CARNEVIL_WORK", DEFAULT_WORK)
    print(
        f"error: {path} not found.\n"
        f"Game data is not part of this repository. It is expected under {work}\n"
        f"(set CARNEVIL_WORK to use another location, or mount the share).",
        file=sys.stderr,
    )
    return 2


def _load(path):
    """(image, None) or (None, exit status) after reporting the problem."""
    if not os.path.isfile(path):
        return None, _missing(path)
    try:
        return exe.load_exe(path), None
    except (exe.UnknownRevisionError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 1


def _census(path):
    image, status = _load(path)
    if image is None:
        return status
    start, end = image.load_addr, image.end_addr() & ~3
    print(f"range 0x{start:08X}-0x{end:08X}, {(end - start) // 4} words")
    _print_census(mips.tally(mips.walk(image, start, end)))
    return 0


def _print_census(tally):
    """Mnemonic counts, MIPS IV / COP0 presence and unknown words of a mips.Tally."""
    counts, unknown = tally.counts, tally.unknown
    print("\nmnemonic counts:")
    for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {name} {n}")
    print("\nMIPS IV instructions present:")
    for name in sorted(mips.MIPS_IV_MNEMONICS & counts.keys()):
        print(f"  {name} {counts[name]}")
    print(f"  fp condition code != 0 uses {tally.fp_cc_nonzero}")
    print("\nCOP0 instructions present:")
    for name in sorted(mips.COP0_MNEMONICS & counts.keys()):
        print(f"  {name} {counts[name]}")
    print(f"\nunknown words: {len(unknown)}")
    print("first 20 unknown addresses:")
    for addr in unknown[:20]:
        print(f"  0x{addr:08X}")


def _hex_list(addrs):
    return " ".join(f"0x{a:08X}" for a in addrs) or "-"


def default_manual(out):
    """The manual file `functions` reads when --manual is not given: the output's name with
    `.functions.toml` replaced by `.manual.toml` (config/game.functions.toml ->
    config/game.manual.toml), if that file exists; otherwise None."""
    if not out.endswith(".functions.toml"):
        return None
    path = out[:-len(".functions.toml")] + ".manual.toml"
    return path if os.path.isfile(path) else None


def _functions(path, out, manual_path=None):
    if manual_path is None:
        manual_path = default_manual(out)
    m = functions.Manual()
    if manual_path:
        if not os.path.isfile(manual_path):
            print(f"error: {manual_path} is not a file", file=sys.stderr)
            return 1
        try:
            m = functions.read_manual(manual_path)
        except (ValueError, KeyError, OSError) as exc:  # TOMLDecodeError is a ValueError
            print(f"error: {manual_path}: {exc}", file=sys.stderr)
            return 1
    image, status = _load(path)
    if image is None:
        return status
    try:
        res, notes = functions.discover(image, image.load_addr, m.starts, m.stub_blocks,
                                        m.jump_tables, m.interpreted)
    except ValueError as exc:  # a manual entry the analysis contradicts
        print(f"error: {manual_path}: {exc}", file=sys.stderr)
        return 1
    try:
        functions.write_toml(res, out)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"manual input {manual_path or '-'}: {len(m.starts)} starts, "
          f"{len(m.stub_blocks)} stub blocks, {len(m.jump_tables)} jump tables, "
          f"{len(m.interpreted)} interpreted")
    span = res.text_end - res.text_start
    by_source = {s: sum(s in f.sources for f in res.functions) for s in functions.SOURCES}
    islands = [f.addr for f in res.functions if f.addr >= res.text_end]
    gap = sum(hi - lo for lo, hi in res.unreached)
    print(f"text 0x{res.text_start:08X}-0x{res.text_end:08X} ({span} bytes)")
    print(f"functions {len(res.functions)}; by source (a function counts under each of its sources):")
    for source, n in by_source.items():
        print(f"  {source} {n}")
    print(f"  first found by a lui/addiu pair in code: {notes.code_pointer_functions}")
    print(f"  named from debug strings: {notes.named}")
    print(f"  code islands beyond text_end: {_hex_list(islands)}")
    entries = sum(len(t.targets) for t in res.jump_tables)
    manual_tables = sum(t.table_addr == 0 for t in res.jump_tables)
    print(f"jump tables {len(res.jump_tables)} ({entries} entries), {manual_tables} of them "
          f"from the manual file")
    print(f"interpreted functions {len(res.interpreted)}: {_hex_list(res.interpreted)}")
    print(f"unresolved jr (no table found) {len(notes.unresolved_jr)}: {_hex_list(sorted(notes.unresolved_jr))}")
    print(f"unreached {len(res.unreached)} ranges, {gap} bytes, {100 * gap / span:.2f}% of the text")
    print(f"rejected pointers into unreached code {len(notes.rejected_pointers)}: "
          f"{_hex_list(notes.rejected_pointers)}")
    jumps = " ".join(f"0x{a:08X}->0x{t:08X}" for a, t in notes.jumps_out) or "-"
    print(f"j out of its function, not taken as a tail call {len(notes.jumps_out)}: {jumps}")
    print(f"prologue starts withdrawn {len(notes.withdrawn)}: {_hex_list(sorted(notes.withdrawn))}")
    print(f"control transfers out of the image {len(notes.bad_targets)}, calls out of the image "
          f"{len(notes.external_calls)}, control transfers in delay slots {len(notes.cti_in_delay_slot)}")
    print(f"unknown words reached as code {len(notes.unknown)}: {_hex_list(sorted(notes.unknown))}")
    code = [a for words in functions.reached_words(res).values() for a in sorted(words)]
    print(f"\ncensus of reached code ({len(code)} words):")
    _print_census(mips.tally((a, mips.decode(image.word(a), a)) for a in code))
    print(f"\nwrote {out}")
    return 0


_REG = ("zero at v0 v1 a0 a1 a2 a3 t0 t1 t2 t3 t4 t5 t6 t7 "
        "s0 s1 s2 s3 s4 s5 s6 s7 t8 t9 k0 k1 gp sp fp ra").split()


def _disasm(path, base, start, count):
    """Decode `count` words of a raw little-endian file mapped at `base`, from `start`."""
    if not os.path.isfile(path):
        return _missing(path)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    end = base + len(raw) - len(raw) % 4
    if start % 4 or not base <= start < end:
        print(f"error: start 0x{start:08X} is not a word inside 0x{base:08X}-0x{end:08X}",
              file=sys.stderr)
        return 1
    for addr in range(start, min(end, start + 4 * count), 4):
        word = int.from_bytes(raw[addr - base:addr - base + 4], "little")
        insn = mips.decode(word, addr)
        line = (f"0x{addr:08X}  {word:08X}  {insn.mnemonic:<10} rs={_REG[insn.rs]} "
                f"rt={_REG[insn.rt]} rd={_REG[insn.rd]} sa={insn.sa} imm=0x{insn.imm & 0xFFFFFFFF:X}")
        if insn.target is not None:
            line += f" -> 0x{insn.target:08X}"
        print(line)
    return 0


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME_FUNCTIONS = os.path.join(REPO, "config", "game.functions.toml")
BOOT_STATE = os.path.join(REPO, "config", "boot_state.toml")


def _print_hwcensus(accesses, reach=None):
    """Per region: sites and addresses, each address with reads, writes and functions."""
    regions = {}
    for a in accesses:
        name = board.classify(a.addr)
        if name in ("ram", "unknown") or not board.is_kseg01(a.addr):
            continue
        regions.setdefault(name, {}).setdefault(board.physical(a.addr), []).append(a)
    for name in sorted(regions, key=lambda n: min(regions[n])):
        regs = regions[name]
        sites = [a for lst in regs.values() for a in lst]
        via = sum(a.via is not None for a in sites)
        print(f"{name}: {len(sites)} sites ({via} through a base held in memory), "
              f"{len(regs)} addresses")
        for addr in sorted(regs):
            lst = regs[addr]
            ops = {op: sum(a.op == op for a in lst) for op in ("read", "write", "cache")}
            funcs = sorted({a.function for a in lst})
            line = (f"  0x{addr:08X} {board.register_name(addr | 0xA0000000)}: "
                    f"r{ops['read']} w{ops['write']}")
            if ops["cache"]:
                line += f" cache{ops['cache']}"
            if any(a.indexed for a in lst):
                line += " (indexed)"
            line += f", {len(funcs)} functions: {_hex_list(funcs[:6])}"
            if len(funcs) > 6:
                line += " ..."
            if reach is not None:
                labels = sorted(set().union(*(reach.get(f, set()) for f in funcs)))
                line += f" [{' '.join(labels) or 'unreached by direct calls'}]"
            print(line)


def _print_reach(image, analysis, accesses, game_slots):
    """Per service slot: the loader functions it reaches by direct calls and tail jumps,
    how many of them make an indirect call, and the hardware regions they access."""
    edges, indirect = loader.call_graph(image, analysis)
    by_function = {}
    for a in accesses:
        by_function.setdefault(a.function, set()).add(board.classify(a.addr))
    print("\nper slot (direct calls and tail jumps):")
    for slot, target in sorted(loader.slot_targets(image).items()):
        reached = loader.closure(edges, [target])
        regions = sorted(set().union(*(by_function.get(f, set()) for f in reached)))
        who = "game" if slot in game_slots else "    "
        print(f"  slot 0x{slot:03X} {who} -> 0x{target:08X}: {len(reached)} functions, "
              f"{len(reached & indirect)} with indirect calls; {' '.join(regions) or '-'}")


def _hwcensus(path, rom, ioctl=False, reach=False):
    if rom is not None:
        if not os.path.isfile(rom):
            return _missing(rom)
        try:
            image = loader.loader_image(rom)
        except (exe.UnknownRevisionError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        analysis, _notes = loader.analyse_loader(image)
        game_slots = loader.game_slots(BOOT_STATE)
        labels = loader.loader_reach(image, analysis, game_slots)
        print(f"loader 0x{image.load_addr:08X}-0x{image.end_addr():08X}, "
              f"{len(analysis.functions)} functions; {len(game_slots)} slots called by GAME.EXE")
        accesses = hwcensus.all_accesses(hwcensus.propagate(image, analysis))
        _print_hwcensus(accesses, labels)
        if reach:
            hardware = [a for a in accesses if board.is_kseg01(a.addr)
                        and board.classify(a.addr) not in ("ram", "unknown")]
            _print_reach(image, analysis, hardware, set(game_slots))
        return 0
    image, status = _load(path)
    if image is None:
        return status
    try:
        analysis = functions.read_toml(GAME_FUNCTIONS)
    except (ValueError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{os.path.basename(path)} 0x{image.load_addr:08X}-0x{image.end_addr():08X}, "
          f"{len(analysis.functions)} functions (config/game.functions.toml)")
    _print_hwcensus(hwcensus.hardware_accesses(image, analysis, hwcensus.GAME_RETURNS))
    if ioctl:
        _print_ioctls(*hwcensus.ioctl_calls(image, analysis, hwcensus.GAME_RETURNS))
    return 0


def _recompile(path, out, functions_path):
    """C for every function of the functions file, from the EXE it was made from."""
    try:
        analysis = functions.read_toml(functions_path)
    except (ValueError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not os.path.isfile(path):
        return _missing(path)
    try:
        image = exe.load_exe(path, expect_sha256=None)  # the functions file pins the hash
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if image.sha256 != analysis.exe_sha256:
        print(f"error: {path} has SHA-256 {image.sha256}, but {functions_path} was made from "
              f"the EXE with SHA-256 {analysis.exe_sha256}; refusing to recompile it",
              file=sys.stderr)
        return 1
    try:
        m = recomp.recompile(image, analysis, out)
    except (recomp.RecompileError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {m.functions} functions in {m.files} files to {out}: {m.stubs} loader stubs, "
          f"{m.interpreted} interpreted, {m.jump_tables} jump tables, "
          f"{m.unresolved_jr} jr left to rt_jump")
    return 0


def _call_edges(image, analysis):
    """(edges, unclean): function start -> the starts control can leave it for (a direct call,
    a branch, `j` or jump-table entry outside it, running off its end), and the starts that
    are not clean themselves, with the reason. Not clean: a stub, an interpreted function, or
    one holding a `break`, a `jalr`, a `jr` (not on ra) without a jump table, or a call or
    jump to an address that is neither its own nor a function start."""
    insn = mips.Decoder(image).insn
    starts = {f.addr for f in analysis.functions}
    tables = {t.jr_addr: t for t in analysis.jump_tables}
    edges, unclean = {}, {}

    def stub(addr):
        return any(lo <= addr < hi for lo, hi in analysis.stub_blocks)

    for f in analysis.functions:
        if stub(f.addr):
            unclean[f.addr] = "stub"
        elif f.addr in analysis.interpreted:
            unclean[f.addr] = "interpreted"
    for start, words in functions.reached_words(analysis).items():
        out = edges.setdefault(start, set())
        if start in unclean:
            continue

        def leave(target, why):
            if target in words:
                return
            if stub(target):
                unclean.setdefault(start, "stub call")
            elif target in starts:
                out.add(target)
            else:
                unclean.setdefault(start, why)

        for a in sorted(words):
            i = insn(a)
            if i.mnemonic == "break":
                unclean.setdefault(start, "break")
            elif i.kind == "call_reg":
                unclean.setdefault(start, "jalr")
            elif i.kind == "jump_reg" and i.rs != mips.RA:
                if a not in tables:
                    unclean.setdefault(start, "unresolved jr")
                else:
                    for t in tables[a].targets:
                        leave(t, "jump-table entry outside the function")
            elif i.kind in ("call", "jump", "branch", "branch_likely"):
                leave(i.target, "transfer to an address that is not a function start")
            # where execution goes on after this word, if anywhere: after a transfer's delay
            # slot, or the next word (a slot's own successor is its transfer's business)
            if i.kind in recomp._TRANSFERS:
                ends = i.kind in ("jump", "jump_reg") or (
                    i.kind in ("branch", "branch_likely") and recomp._condition(i) is True)
                after = None if ends else a + 8
            elif a - 4 in words and insn(a - 4).kind in recomp._TRANSFERS:
                after = None
            else:
                after = a + 4
            if after is not None:
                leave(after, "runs off its end into an address that is not a function start")
    return edges, unclean


def pure_functions(image, analysis, returns=hwcensus.GAME_RETURNS):
    """(starts, stats): the functions statically pure by the census, whose call closure
    (`_call_edges`) holds no stub call,
    no hardware access (`hwcensus.hardware_accesses`), no `break`, no `jalr`, no unresolved
    `jr` and no interpreted function, ascending. stats: "candidates" (the length of the list)
    and "removed_by_interpreted", the functions the interpreted rule alone takes out. A
    superset of the functions pure at run time: hardware reached through a pointer held in
    memory or passed as an argument is invisible to the census (test_against_mame tells the
    two apart from MAME captures)."""
    edges, unclean = _call_edges(image, analysis)
    for a in hwcensus.hardware_accesses(image, analysis, returns):
        unclean.setdefault(a.function, "hardware access")
    callers = {}
    for start, targets in edges.items():
        for t in targets:
            callers.setdefault(t, set()).add(start)

    def impure(seeds):
        seen, stack = set(), list(seeds)
        while stack:
            f = stack.pop()
            if f not in seen:
                seen.add(f)
                stack.extend(callers.get(f, ()))
        return seen

    every = {f.addr for f in analysis.functions}
    pure = every - impure(unclean)
    without = every - impure(a for a, why in unclean.items() if why != "interpreted")
    return sorted(pure), {"candidates": len(pure), "removed_by_interpreted": len(without - pure)}


def _pure_functions(path, functions_path):
    try:
        analysis = functions.read_toml(functions_path)
    except (ValueError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    image, status = _load(path)
    if image is None:
        return status
    if image.sha256 != analysis.exe_sha256:
        print(f"error: {path} has SHA-256 {image.sha256}, but {functions_path} was made from "
              f"the EXE with SHA-256 {analysis.exe_sha256}", file=sys.stderr)
        return 1
    pure, stats = pure_functions(image, analysis)
    for addr in pure:
        print(f"0x{addr:08X}")
    print(f"{stats['candidates']} pure functions of {len(analysis.functions)}; the interpreted "
          f"rule alone removes {stats['removed_by_interpreted']}", file=sys.stderr)
    return 0


def _value_text(value):
    if isinstance(value, int):
        return f"0x{value:X}"
    if isinstance(value, hwcensus.Idx):
        return f"0x{value.base:X}+reg"
    if isinstance(value, hwcensus.Ptr):
        path = "/".join(f"0x{p:X}" for p in value.path)
        return f"*[{path}]{value.delta:+#x}" + ("+reg" if value.indexed else "")
    return "?"


def _print_ioctls(counts, other):
    """ioctl(fd, cmd) at the slot 0x0C8 stub: constant pairs, then the other sites."""
    total = sum(counts.values()) + len(other)
    print(f"\nioctl calls (stub 0x{hwcensus.IOCTL_STUB:08X}): {total} sites")
    for (fd, cmd), n in sorted(counts.items()):
        print(f"  fd {fd} cmd 0x{cmd:04X}: {n}")
    print(f"sites without a constant descriptor or command: {len(other)}")
    for call in other:
        print(f"  0x{call.site:08X} in 0x{call.function:08X}: fd {_value_text(call.args.get(4))}"
              f" cmd {_value_text(call.args.get(5))}")


def _verify(partition):
    """Check a partition's files against the game's own FILESYS.CHK table."""
    if not os.path.isfile(partition):
        return _missing(partition)
    try:
        result = disk.verify_files(partition)
    except (disk.DiskFormatError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{len(result.matched)} of {result.listed} listed files match")
    print(f"{len(result.missing)} listed files are not on the partition")
    print(f"{len(result.unlisted)} files on the partition are not listed (not checked)")
    for name in result.mismatched:
        print(f"mismatch: {name}")
    return 1 if result.mismatched else 0


def _vectors(write_cases, merge, out, seed):
    """Write the input of tools/mame/insnvec.lua, or merge its output into the vector file."""
    try:
        if write_cases is not None:
            case_list = vectors.cases(seed)
            vectors.write_cases(case_list, write_cases)
            run = sum(not c.no_mame for c in case_list)
            print(f"wrote {run} cases to {write_cases} ({len(case_list) - run} not run in MAME)")
        else:
            n = vectors.merge(merge[0], merge[1], out)
            print(f"wrote {n} cases to {out}")
    except (ValueError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{text} is not a positive number")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(prog="carnevil_tools")
    sub = parser.add_subparsers(dest="command", required=True)
    split = sub.add_parser("split", help="split a raw disk image into partitions")
    split.add_argument("raw")
    split.add_argument("-o", "--out", required=True, metavar="DIR")
    extract = sub.add_parser("extract", help="extract the files of a partition")
    extract.add_argument("partition")
    extract.add_argument("-o", "--out", required=True, metavar="DIR")
    verify = sub.add_parser("verify", help="check a partition's files against FILESYS.CHK")
    verify.add_argument("partition")
    census = sub.add_parser("census", help="count the instructions of an EXE image")
    census.add_argument("exe")
    funcs = sub.add_parser("functions", help="discover the functions of an EXE image")
    funcs.add_argument("exe")
    funcs.add_argument("-o", "--out", required=True, metavar="TOML")
    funcs.add_argument("--manual", metavar="TOML",
                       help="hand-maintained starts and stub blocks (default: the output's "
                            "name with .functions.toml replaced by .manual.toml, if present)")
    disasm = sub.add_parser("disasm", help="decode words of a raw file to the terminal")
    disasm.add_argument("file")
    disasm.add_argument("--base", required=True, type=lambda s: int(s, 0), metavar="ADDR",
                        help="address of the file's first byte")
    disasm.add_argument("--start", required=True, type=lambda s: int(s, 0), metavar="ADDR")
    disasm.add_argument("--count", required=True, type=_positive_int, metavar="N")
    hwc = sub.add_parser("hwcensus", help="hardware registers the code of an image touches")
    hwc.add_argument("exe", nargs="?", help="GAME.EXE (uses config/game.functions.toml)")
    hwc.add_argument("--loader", metavar="BOOT_U32",
                     help="census the boot ROM's resident loader instead")
    hwc.add_argument("--ioctl", action="store_true",
                     help="also list the EXE's ioctl calls into the loader (slot 0x0C8)")
    hwc.add_argument("--reach", action="store_true",
                     help="with --loader: per service slot, the loader functions it reaches "
                          "and the hardware regions among them")
    rec = sub.add_parser("recompile", help="write the C of every function of an EXE")
    rec.add_argument("exe")
    rec.add_argument("-o", "--out", required=True, metavar="DIR",
                     help="output folder (replaced only if it holds a manifest.toml)")
    rec.add_argument("--functions", default=GAME_FUNCTIONS, metavar="TOML",
                     help="the analysis to recompile (default: config/game.functions.toml)")
    pure = sub.add_parser("pure-functions",
                          help="functions statically pure by the census: their call closure "
                               "touches no loader stub, attributable hardware, break, indirect "
                               "call or interpreted function (a superset of what is pure at "
                               "run time: hardware reached through pointers held in memory is "
                               "invisible to it)")
    pure.add_argument("exe", nargs="?",
                      default=os.path.join(os.environ.get("CARNEVIL_WORK", DEFAULT_WORK),
                                           "part0_files", "GAME.EXE"),
                      help="the EXE the functions file was made from (default: "
                           "$CARNEVIL_WORK/part0_files/GAME.EXE)")
    pure.add_argument("--functions", default=GAME_FUNCTIONS, metavar="TOML",
                      help="the analysis (default: config/game.functions.toml)")
    vec = sub.add_parser("vectors", help="instruction test cases for MAME, and their results")
    vec_mode = vec.add_mutually_exclusive_group(required=True)
    vec_mode.add_argument("--write-cases", metavar="JSON",
                          help="write the cases for tools/mame/insnvec.lua")
    vec_mode.add_argument("--merge", nargs=2, metavar=("CASES", "RESULTS"),
                          help="merge the script's results into the vector file (-o)")
    vec.add_argument("-o", "--out", metavar="JSON")
    vec.add_argument("--seed", type=int, help="with --write-cases (default 1)")
    args = parser.parse_args(argv)

    if args.command == "vectors":
        if args.merge is not None and args.out is None:
            parser.error("--merge needs -o <file>")
        if args.merge is not None and args.seed is not None:
            parser.error("--seed applies to --write-cases only")
        if args.write_cases is not None and args.out is not None:
            parser.error("-o applies to --merge only (--write-cases names its own file)")
        return _vectors(args.write_cases, args.merge, args.out,
                        1 if args.seed is None else args.seed)

    if args.command == "hwcensus":
        if (args.exe is None) == (args.loader is None):
            parser.error("hwcensus takes an EXE or --loader <boot.u32>, not both")
        if args.ioctl and args.loader is not None:
            parser.error("--ioctl applies to an EXE, not to --loader")
        if args.reach and args.loader is None:
            parser.error("--reach applies to --loader")
        return _hwcensus(args.exe, args.loader, args.ioctl, args.reach)
    if args.command == "disasm":
        return _disasm(args.file, args.base, args.start, args.count)
    if args.command == "recompile":
        return _recompile(args.exe, args.out, args.functions)
    if args.command == "pure-functions":
        return _pure_functions(args.exe, args.functions)
    if args.command == "verify":
        return _verify(args.partition)
    if args.command == "census":
        return _census(args.exe)
    if args.command == "functions":
        return _functions(args.exe, args.out, args.manual)
    source = args.raw if args.command == "split" else args.partition
    if not os.path.isfile(source):
        return _missing(source)
    try:
        if args.command == "split":
            paths = disk.split_partitions(args.raw, args.out)
        else:
            paths = disk.extract_files(args.partition, args.out)
    except (disk.DiskFormatError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    what = "partitions" if args.command == "split" else "files"
    print(f"wrote {len(paths)} {what} to {args.out}")
    return 0
