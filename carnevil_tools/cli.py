"""Command-line entry point: python3 -m carnevil_tools <command>."""
import argparse
import os
import sys

from carnevil_tools import disk, exe, mips

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


def _census(path):
    if not os.path.isfile(path):
        return _missing(path)
    try:
        image = exe.load_exe(path)
    except (exe.UnknownRevisionError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    start, end = image.load_addr, image.end_addr() & ~3
    counts = {}
    unknown = []
    cc_nonzero = 0
    for addr, insn in mips.walk(image, start, end):
        counts[insn.mnemonic] = counts.get(insn.mnemonic, 0) + 1
        if insn.kind == "unknown":
            unknown.append(addr)
        elif mips.fp_condition_code(insn):
            cc_nonzero += 1
    print(f"range 0x{start:08X}-0x{end:08X}, {(end - start) // 4} words")
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
    args = parser.parse_args(argv)

    if args.command == "census":
        return _census(args.exe)
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
