"""Command-line entry point: python3 -m carnevil_tools <command>."""
import argparse
import os
import sys

from carnevil_tools import disk, exe, functions, mips

DEFAULT_WORK = "/Volumes/archive/reincarnevilation/work"


def _missing(path):
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
    except (exe.UnknownRevisionError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 1


def _census(path):
    image, status = _load(path)
    if image is None:
        return status
    start, end = image.load_addr, image.end_addr() & ~3
    print(f"range 0x{start:08X}-0x{end:08X}, {(end - start) // 4} words")
    _print_census(mips.walk(image, start, end))
    return 0


def _print_census(insns):
    """Mnemonic counts, MIPS IV / COP0 presence and unknown words of (addr, Insn) pairs."""
    counts = {}
    unknown = []
    cc_nonzero = 0
    for addr, insn in insns:
        counts[insn.mnemonic] = counts.get(insn.mnemonic, 0) + 1
        if insn.kind == "unknown":
            unknown.append(addr)
        elif mips.fp_condition_code(insn):
            cc_nonzero += 1
    print("\nmnemonic counts:")
    for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {name} {n}")
    print("\nMIPS IV instructions present:")
    for name in sorted(mips.MIPS_IV_MNEMONICS & counts.keys()):
        print(f"  {name} {counts[name]}")
    print(f"  fp condition code != 0 uses {cc_nonzero}")
    print("\nCOP0 instructions present:")
    for name in sorted(mips.COP0_MNEMONICS & counts.keys()):
        print(f"  {name} {counts[name]}")
    print(f"\nunknown words: {len(unknown)}")
    print("first 20 unknown addresses:")
    for addr in unknown[:20]:
        print(f"  0x{addr:08X}")


def _hex_list(addrs):
    return " ".join(f"0x{a:08X}" for a in addrs) or "-"


def _functions(path, out):
    image, status = _load(path)
    if image is None:
        return status
    res, notes = functions.discover(image, image.load_addr)
    functions.write_toml(res, out)
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
    print(f"jump tables {len(res.jump_tables)} ({entries} entries)")
    print(f"unresolved jr (no table found) {len(notes.unresolved_jr)}: {_hex_list(sorted(notes.unresolved_jr))}")
    print(f"unreached {len(res.unreached)} ranges, {gap} bytes, {100 * gap / span:.2f}% of the text")
    print(f"rejected pointers at the start of an unreached range {len(notes.rejected_pointers)}: "
          f"{_hex_list(notes.rejected_pointers)}")
    print(f"control transfers out of the image {len(notes.bad_targets)}, calls out of the image "
          f"{len(notes.external_calls)}, control transfers in delay slots {len(notes.cti_in_delay_slot)}")
    print(f"unknown words reached as code {len(notes.unknown)}: {_hex_list(sorted(notes.unknown))}")
    skip = set()
    for lo, hi in res.unreached:
        skip.update(range(lo, hi, 4))
    code = [a for f in res.functions for a in range(f.addr, f.addr + f.size, 4) if a not in skip]
    print(f"\ncensus of reached code ({len(code)} words):")
    _print_census((a, mips.decode(image.word(a), a)) for a in code)
    print(f"\nwrote {out}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="carnevil_tools")
    sub = parser.add_subparsers(dest="command", required=True)
    split = sub.add_parser("split", help="split a raw disk image into partitions")
    split.add_argument("raw")
    split.add_argument("-o", "--out", required=True, metavar="DIR")
    extract = sub.add_parser("extract", help="extract the files of a partition")
    extract.add_argument("partition")
    extract.add_argument("-o", "--out", required=True, metavar="DIR")
    census = sub.add_parser("census", help="count the instructions of an EXE image")
    census.add_argument("exe")
    funcs = sub.add_parser("functions", help="discover the functions of an EXE image")
    funcs.add_argument("exe")
    funcs.add_argument("-o", "--out", required=True, metavar="TOML")
    args = parser.parse_args(argv)

    if args.command == "census":
        return _census(args.exe)
    if args.command == "functions":
        return _functions(args.exe, args.out)
    source = args.raw if args.command == "split" else args.partition
    if not os.path.isfile(source):
        return _missing(source)
    try:
        if args.command == "split":
            paths = disk.split_partitions(args.raw, args.out)
        else:
            paths = disk.extract_files(args.partition, args.out)
    except disk.DiskFormatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {len(paths)} files to {args.out}")
    return 0
