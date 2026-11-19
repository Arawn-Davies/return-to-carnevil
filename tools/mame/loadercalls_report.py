"""Summarise a tools/mame/loadercalls.lua run (docs/analysis/loader-calls.md).

python3 tools/mame/loadercalls_report.py <run folder> [--console <out.txt>]

Reads <run folder>/loader-calls-first.txt and <run folder>/debug.log. Prints per-slot call
counts for the whole run, the ioctl (fd, command) counts, the opens with their paths and
results, and the console (UART) output as lines with emulated time and frame; --console
also writes those lines to a file. Emulated seconds for debug.log lines are total CPU cycles
/ 150 MHz (the LT lines give the ratio)."""
import argparse
import collections
import os
import re
import sys

STUB = 0x800C408C
NAMES = {  # boot-handoff.md, "What `_start` copies"
    0x018: "close", 0x028: "open for writing", 0x038: "file-system call", 0x088: "open",
    0x090: "read", 0x0C0: "write", 0x0C8: "ioctl", 0x0D8: "reset handler tables",
    0x0E0: "isatty", 0x0E8: "data cache writeback/invalidate", 0x0F0: "PCI config read",
    0x0F8: "PCI config write", 0x108: "PCI vendor probe", 0x110: "PCI device set-up",
    0x118: "select disk partition", 0x128: "partition table", 0x158: "raw IDE transfer",
    0x190: "file-system call", 0x198: "load and run EXE", 0x1A8: "argument block",
    0x1B8: "file-system call", 0x1C0: "lseek-like", 0x1D8: "disk call with timeout",
    0x1E0: "GT64010 interrupt handler", 0x1E8: "FPU exception handler", 0x1F0: "break 7 hook",
    0x1F8: "break 6 hook", 0x200: "CPU interrupt-line handler", 0x220: "install_disk_info",
}
FIELD = re.compile(r"(\w+)=(\S+)")
CALL = (r'^n (\d+) f (-?\d+) t (\S+) slot (\w+) a0 (\w+) a1 (\w+) a2 (\w+) a3 (\w+) '
        r'ra (\w+) sp (\w+) v0 (\S+) v1 (\S+)')
FIRST = re.compile(CALL + r'(?: s0 "(.*?)")?(?: buf "(.*)")?$')
# in debug.log the script's log() turned every `"` and `%` into `?`, the quotes included
LATER = re.compile(CALL + r'(?: s0 \?(.*?)\?(?= buf |$))?(?: buf \?(.*)\?)?$')


def lo(text):
    return int(text, 16) & 0xFFFFFFFF


def first_calls(path):
    out = []
    with open(path) as f:
        for line in f:
            m = FIRST.match(line.rstrip("\n"))
            if not m:
                raise ValueError(f"bad line in {path}: {line!r}")
            g = m.groups()
            out.append({"n": int(g[0]), "f": int(g[1]), "t": float(g[2]), "slot": int(g[3], 16),
                        "a": [int(x, 16) for x in g[4:8]], "ra": int(g[8], 16),
                        "sp": int(g[9], 16), "v0": g[10], "v1": g[11], "s0": g[12], "buf": g[13]})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--console")
    args = ap.parse_args(argv)
    first = first_calls(os.path.join(args.run, "loader-calls-first.txt"))
    slot_n = collections.Counter(e["slot"] for e in first)
    ioctl = collections.Counter((e["a"][0], e["a"][1]) for e in first if e["slot"] == 0x0C8)
    ioctl_ra = collections.Counter()
    opens = [e for e in first if e["slot"] in (0x028, 0x088, 0x038, 0x190, 0x1B8)]
    writes = [e for e in first if e["slot"] == 0x0C0]
    uart = []
    lt = []
    later_lua = []
    first_frames = (first[0]["f"], first[-1]["f"]) if first else None
    with open(os.path.join(args.run, "debug.log"), errors="replace") as f:
        for line in f:
            tag = line[:3]
            if tag == "LC ":
                d = dict(FIELD.findall(line))
                slot = lo(d["pc"]) - STUB
                slot_n[slot] += 1
                if slot == 0x0C8:
                    key = (lo(d["a0"]), lo(d["a1"]))
                    ioctl[key] += 1
                    ioctl_ra[(key, lo(d["ra"]))] += 1
            elif tag == "LS ":
                m = LATER.match(line[3:].rstrip("\n"))
                if not m:
                    raise ValueError(f"bad LS line: {line!r}")
                if m:
                    g = m.groups()
                    e = {"n": int(g[0]), "f": int(g[1]), "t": float(g[2]), "slot": int(g[3], 16),
                         "a": [int(x, 16) for x in g[4:8]], "ra": int(g[8], 16), "v0": g[10],
                         "v1": g[11], "s0": g[12], "buf": g[13]}
                    slot_n[e["slot"]] += 1
                    later_lua.append(e)
                    if e["slot"] == 0x0C0:
                        writes.append(e)
                    else:
                        opens.append(e)
            elif line.startswith("UART "):
                d = dict(FIELD.findall(line))
                uart.append((int(d["f"]), int(d["c"]), lo(d["pc"]), int(d["d"], 16)))
            elif tag == "LT ":
                d = dict(FIELD.findall(line))
                lt.append((float(d["t"]), int(d["c"])))
    rate = (lt[-1][1] - lt[0][1]) / (lt[-1][0] - lt[0][0]) if len(lt) > 1 else 150e6
    print(f"first calls {len(first)} (frames {first_frames}); later calls handled from Lua "
          f"(returned) {len(later_lua)}; cycles per second {rate:,.0f}; run to "
          f"{lt[-1][0] if lt else 0:.3f} s emulated")
    print(f"\nper-slot calls, whole run ({sum(slot_n.values())}):")
    for slot in sorted(slot_n):
        print(f"  {slot:03X} {NAMES.get(slot, '?'):32} {slot_n[slot]}")
    print("\nioctl (fd, command) counts:")
    for (fd, cmd), n in sorted(ioctl.items()):
        print(f"  fd {fd} cmd {cmd:#06x} {n}")
    print("\nioctl hottest call sites (fd, cmd, ra - 8):")
    for ((fd, cmd), ra), n in ioctl_ra.most_common(15):
        print(f"  fd {fd} cmd {cmd:#06x} at {ra - 8:08X} {n}")
    print("\nopens and file-system calls (slot, a0 string, a1, v0):")
    for e in opens:
        print(f"  t {e['t']:.3f} f {e['f']} slot {e['slot']:03X} s0 {e['s0']!r} a1 {e['a'][1]:#x} "
              f"a2 {e['a'][2]:#x} ra {e['ra']:08X} -> v0 {e['v0']}")
    print(f"\nwrites (slot 0x0C0): {len(writes)}; to fd 0-2: "
          f"{sum(1 for e in writes if e['a'][0] <= 2)}")
    by_fd = collections.Counter(e["a"][0] for e in writes)
    print("  by fd: " + ", ".join(f"{fd}: {n}" for fd, n in sorted(by_fd.items())))
    # console lines from the UART stream
    lines, cur, start = [], [], None
    for fr, c, pc, ch in uart:
        if start is None:
            start = (fr, c, pc)
        if ch == 0x0A:
            text = "".join(chr(x) if 0x20 <= x < 0x7F else f"\\x{x:02X}" for x in cur if x != 0x0D)
            lines.append((start[0], start[1] / rate, fr, c / rate, start[2], text))
            cur, start = [], None
        else:
            cur.append(ch)
    if cur:
        lines.append((start[0], start[1] / rate, None, None, start[2], "".join(map(chr, cur))))
    print(f"\nUART: {len(uart)} bytes, {len(lines)} lines (frame and emulated second of the "
          f"line's first byte, the writing pc):")
    out = []
    for fr, t, fr_end, t_end, pc, text in lines:
        out.append(f"f {fr} t {t:.6f} pc {pc:08X} | {text}")
    for line in out:
        print("  " + line)
    if args.console:
        with open(args.console, "w") as f:
            f.write("# UART output of a loadercalls.lua run: frame, emulated seconds since "
                    "power-on and pc of each line's first byte (next pc, see README), text\n")
            for line in out:
                f.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
