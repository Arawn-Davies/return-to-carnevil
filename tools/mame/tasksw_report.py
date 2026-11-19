"""Summarise the debug.log of a tools/mame/tasksw.lua run (docs/analysis/task-switch.md).

python3 tools/mame/tasksw_report.py <debug.log> [--from-frame F]

Prints addresses and counts only. Frame numbers are MAME's (the screen's frame counter);
emulated seconds are total CPU cycles / 150 MHz (the TICK lines give the ratio)."""
import argparse
import collections
import re
import statistics
import sys

CLOCK = 150_000_000
SCHED_SP = None          # the scheduler's sp while tasks run, from the R lines (sp - 0xCC)
STACK_BLOCK = 0x800      # 0x80148FA4 allocates the fifth argument + 0x800 bytes (read)
TRAMPOLINE = 0x8014A930
EXIT_KILL_RA = 0x80151B70  # 0x80149C58 called from the exit 0x80151B50
GAME = (0x800C4000, 0x801A4224)
STUB = (0x800C408C, 0x800C42CC)

FIELD = re.compile(r"(\w+)=([^ ]*)")


def lo(text):
    return int(text, 16) & 0xFFFFFFFF


def parse(path):
    events = []
    with open(path, errors="replace") as f:
        for line in f:
            if not line.startswith("TS "):
                continue
            parts = line.split()
            kind = parts[1]
            fields = dict(FIELD.findall(line))
            events.append((kind, fields))
    return events


def hist(counter, width=None):
    keys = sorted(counter)
    return ", ".join(f"{k}: {counter[k]}" for k in keys)


def where(pc):
    if STUB[0] <= pc < STUB[1]:
        return "stub"
    if GAME[0] <= pc < GAME[1]:
        return "game"
    if 0x80000000 <= pc < 0x80015C40:
        return "loader"
    return "other"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--from-frame", type=int, default=None,
                    help="count per-frame statistics from this frame (default: first pass)")
    ap.add_argument("--play-frame", type=int, default=None,
                    help="also give per-frame statistics from this frame on (play)")
    args = ap.parse_args(argv)
    ev = parse(args.log)

    entry = next(f for k, f in ev if k == "ENTRY")
    end = next(f for k, f in reversed(ev) if k == "END")
    f_entry, f_end = int(entry["f"]), int(end["f"])
    c_entry, c_end = int(entry["c"]), int(end["c"])
    ticks = [(float(f["t"]), int(f["c"])) for k, f in ev if k == "TICK"]
    if len(ticks) > 1:
        rate = (ticks[-1][1] - ticks[0][1]) / (ticks[-1][0] - ticks[0][0])
    else:
        rate = CLOCK
    print(f"entry frame {f_entry}, end frame {f_end}; emulated {float(entry['t']):.3f} s to "
          f"{float(end['t']):.3f} s; cycles per emulated second {rate:,.0f}")
    print(f"frames per emulated second {(f_end - f_entry) / ((c_end - c_entry) / rate):.3f}")
    counts = collections.Counter(k for k, _ in ev)
    print("event lines: " + ", ".join(f"{k} {counts[k]}" for k in sorted(counts)))

    # --- tasks: creation, records, stacks
    tasks = {}           # rec -> current task dict
    history = []         # every task ever created
    pending_create = []
    alive_by_frame = {}
    alive = 0
    max_alive, max_alive_frame = 0, None
    kills = collections.Counter()
    sched_sp = collections.Counter()
    exc_depth = 0
    exc_stack = []
    in_exc_events = collections.Counter()
    in_exc_detail = []
    k1_checks = collections.Counter()
    k1_bad = []
    exc_ctx = collections.Counter()
    exc_epc_where = collections.Counter()
    exc_cause = collections.Counter()
    w_by_pc = collections.Counter()
    w_where = collections.Counter()
    v_by_pc = collections.Counter()
    yields_by_frame = collections.Counter()
    passes_by_frame = collections.Counter()
    resumes_by_frame = collections.Counter()
    yield_site = collections.Counter()
    yield_arg = collections.Counter()
    yield_func = collections.Counter()
    sp_violations = []
    resume_mismatch = []
    first_entries = collections.Counter()
    pw = None
    waits = []
    pass_v = collections.Counter()
    pass_tgt = collections.Counter()
    pass_frames = []
    last_pass_frame = None
    i3 = i7 = no_wait = pass_yields = 0
    pass_frame = None
    yields_per_pass, yields_pass_frame = [], []
    tf_values = collections.Counter()
    exits = 0
    creators = collections.Counter()
    task_of_frame_first = None

    def task_for_sp(sp):
        for rec, t in tasks.items():
            if t["base"] <= sp < t["base"] + t["size"]:
                return rec, t
        return None, None

    for kind, f in ev:
        frame = int(f["f"]) if "f" in f else None
        if kind in ("CA", "CB"):
            pending_create.append((kind, f))
        elif kind == "CR":
            ck, cf = pending_create.pop() if pending_create else (None, {})
            rec = lo(f["rec"])
            if rec == 0:
                continue
            t = {"rec": rec, "base": lo(f["base"]), "tsp": lo(f["tsp"]), "size": STACK_BLOCK,
                 "func": lo(f["func"]), "id": lo(f["id"]), "name": f.get("name", ""),
                 "created": frame, "freed": None, "min_sp": lo(f["tsp"]), "yields": 0,
                 "a4": lo(cf.get("a4", "0")), "via": ck, "creator": lo(cf.get("wra", "0")),
                 "last_sp": lo(f["tsp"]), "depth_diff": None, "exited": False}
            if t["a4"]:
                t["size"] = STACK_BLOCK + t["a4"]
            creators[(t["name"], t["creator"])] += 1
            tasks[rec] = t
            history.append(t)
            alive += 1
            if alive > max_alive:
                max_alive, max_alive_frame = alive, frame
        elif kind == "KF":
            rec = lo(f["a0"])
            t = tasks.pop(rec, None)
            if t:
                t["freed"] = frame
                alive -= 1
        elif kind in ("KS", "LS"):
            rec = lo(f["rec"])
            for t in reversed(history):
                if t["rec"] == rec and t["depth_diff"] is None:
                    low16 = lo(f["lowest16"])
                    t["depth_diff"] = (t["base"] + t["size"] - low16) if low16 else 0
                    t["changed"] = int(f["changed"])
                    t["map"] = f["map"]
                    break
        elif kind == "KC":
            a0, ra, cur = lo(f["a0"]), lo(f["ra"]), lo(f["cur"])
            kills["exit" if ra == EXIT_KILL_RA else ("self" if a0 == cur else "other")] += 1
            if exc_depth:
                in_exc_events["KC"] += 1
        elif kind == "KM":
            kills["by id (0x80149D20)"] += 1
            if exc_depth:
                in_exc_events["KM"] += 1
        elif kind == "X":
            exits += 1
            cur, sp = lo(f["cur"]), lo(f["sp"])
            if cur in tasks:
                tasks[cur]["exited"] = True
                t = tasks[cur]
                if not (t["base"] <= sp < t["base"] + t["size"]):
                    sp_violations.append(("X", frame, cur, sp))
            if exc_depth:
                in_exc_events["X"] += 1
        elif kind == "Y":
            cur, sp, ra, a0, k1 = (lo(f["cur"]), lo(f["sp"]), lo(f["ra"]), lo(f["a0"]),
                                   lo(f["k1"]))
            yields_by_frame[frame] += 1
            pass_yields += 1
            yield_site[ra - 8] += 1
            yield_arg[a0] += 1
            t = tasks.get(cur)
            if t:
                t["yields"] += 1
                t["min_sp"] = min(t["min_sp"], sp)
                t["last_sp"] = sp
                yield_func[(t["name"], t["func"])] += 1
                if not (t["base"] <= sp < t["base"] + t["size"]):
                    sp_violations.append(("Y", frame, cur, sp))
            else:
                sp_violations.append(("Y unknown task", frame, cur, sp))
            k1_checks["yield k1 == scheduler sp"] += (k1 in sched_sp)
            k1_checks["yield k1 != scheduler sp"] += (k1 not in sched_sp)
            if exc_depth:
                in_exc_events["Y"] += 1
                in_exc_detail.append(("Y", frame, ra))
        elif kind == "R":
            cur, sp, tsp, tra = lo(f["cur"]), lo(f["sp"]), lo(f["tsp"]), lo(f["tra"])
            sched_sp[sp - 0xCC] += 1
            resumes_by_frame[frame] += 1
            t = tasks.get(cur)
            if t:
                if tsp != t["last_sp"]:
                    resume_mismatch.append((frame, cur, tsp, t["last_sp"]))
                if tra == TRAMPOLINE:
                    first_entries[(t["name"], t["func"])] += 1
        elif kind == "PW":
            if pass_frame is not None:
                yields_per_pass.append(pass_yields)
                yields_pass_frame.append((pass_frame, pass_yields))
            pass_frame, pass_yields = frame, 0
            pw = f
            passes_by_frame[frame] += 1
            pass_v[int(f["v"], 16)] += 1
            no_wait += int(f["v"], 16) >= int(f["tgt"], 16)
            pass_tgt[int(f["tgt"], 16)] += 1
            tf_values[int(f["tf"], 16)] += 1
            if last_pass_frame is not None:
                pass_frames.append(frame - last_pass_frame)
            last_pass_frame = frame
            alive_by_frame[frame] = alive
        elif kind == "PD":
            if pw is not None:
                waits.append(int(f["c"]) - int(pw["c"]))
            pw = None
        elif kind == "I3":
            i3 += 1
        elif kind == "I7":
            i7 += 1
        elif kind == "EV":
            sp, k1, epc = lo(f["sp"]), lo(f["k1"]), lo(f["epc"])
            cause = lo(f["cause"])
            exc_cause[(cause >> 2) & 0x1F] += 1
            rec, t = task_for_sp(sp)
            ctx = "task" if t else ("scheduler/main" if 0x807F0000 <= sp < 0x80800000 else "other")
            if exc_depth:
                ctx = "nested"
            exc_ctx[ctx] += 1
            exc_epc_where[(ctx, where(epc))] += 1
            if ctx == "task":
                k1_checks["exception in a task: k1 == scheduler sp at entry"] += (k1 in sched_sp)
                if k1 not in sched_sp:
                    k1_bad.append(("entry", frame, epc, k1))
            exc_stack.append((ctx, k1, epc))
            exc_depth += 1
        elif kind in ("ER", "EM"):
            if exc_stack:
                ctx, k1_in, epc_in = exc_stack.pop()
                k1 = lo(f["k1"])
                key = f"{ctx}: k1 at eret == k1 at entry"
                k1_checks[key] += (k1 == k1_in)
                if k1 != k1_in:
                    k1_bad.append(("eret", frame, epc_in, k1_in, k1))
                    k1_checks[f"{ctx}: k1 at eret != k1 at entry"] += 1
            exc_depth = max(0, exc_depth - 1)
        elif kind == "W":
            pc = lo(f["pc"])
            w_by_pc[(lo(f["a"]), pc)] += 1
            w_where[(where(pc), "in exception" if exc_depth else "not in exception")] += 1
        elif kind == "V":
            pc = lo(f["pc"])
            v_by_pc[(pc, "in exception" if exc_depth else "not in exception")] += 1
        if kind in ("CA", "CB", "KF") and exc_depth:
            in_exc_events[kind] += 1

    # --- report
    first_pass = min(passes_by_frame) if passes_by_frame else f_entry
    f0 = args.from_frame if args.from_frame is not None else first_pass
    frames = range(f0, f_end)
    print(f"\nscheduler sp while tasks run (from R, sp - 0xCC): "
          + ", ".join(f"{s:08X} x{n}" for s, n in sched_sp.most_common()))

    print("\n== 1. tasks")
    print(f"created {len(history)}; freed {sum(t['freed'] is not None for t in history)}; "
          f"alive at the end {len(tasks)}; exits through 0x80151B50 {exits}")
    print(f"max alive {max_alive} (first at frame {max_alive_frame})")
    vals = [alive_by_frame[fr] for fr in sorted(alive_by_frame) if fr >= f0]
    if vals:
        print(f"alive at the start of each pass from frame {f0}: median {statistics.median(vals)}, "
              f"mode {statistics.mode(vals)}, min {min(vals)}, max {max(vals)}; histogram "
              f"{hist(collections.Counter(vals))}")
    by_func = collections.defaultdict(list)
    for t in history:
        by_func[(t["name"], t["func"])].append(t)
    print(f"distinct task functions {len(by_func)}:")
    for (name, func), ts in sorted(by_func.items(), key=lambda kv: kv[1][0]["created"]):
        lives = [t["freed"] - t["created"] for t in ts if t["freed"] is not None]
        ids = sorted({t["id"] for t in ts})
        sites = sorted({t["creator"] for t in ts})
        life = (f"lifetime frames min {min(lives)} median {statistics.median(lives)} max "
                f"{max(lives)}" if lives else "never freed")
        print(f"  {name or '?':8} func {func:08X} id {','.join(f'{i:X}' for i in ids)} started "
              f"{len(ts)}, freed {len(lives)}, exited {sum(t['exited'] for t in ts)}, "
              f"first frame {ts[0]['created']}; {life}; created from "
              f"{','.join(f'{s - 8:08X}' for s in sites)}")
    print("kills (0x80149C58 calls): " + ", ".join(f"{k} {v}" for k, v in kills.items()))
    print("first entries through the trampoline (R with ra 0x8014A930): "
          f"{sum(first_entries.values())}")

    print("\n== 2. yields")
    per_frame = [yields_by_frame.get(fr, 0) for fr in frames]
    print(f"frames {f0}..{f_end - 1}: {len(per_frame)}; yields {sum(per_frame)}; per frame mean "
          f"{statistics.mean(per_frame):.2f}, median {statistics.median(per_frame)}, max "
          f"{max(per_frame)} (frame {max(frames, key=lambda fr: yields_by_frame.get(fr, 0))})")
    print("histogram of yields per frame: " + hist(collections.Counter(per_frame)))
    if args.play_frame is not None:
        pf = [yields_by_frame.get(fr, 0) for fr in range(args.play_frame, f_end)]
        print(f"from frame {args.play_frame} (play): {len(pf)} frames, mean "
              f"{statistics.mean(pf):.2f}, max {max(pf)}; histogram "
              + hist(collections.Counter(pf)))
        pa = [alive_by_frame[fr] for fr in sorted(alive_by_frame) if fr >= args.play_frame]
        if pa:
            print(f"alive from frame {args.play_frame}: median {statistics.median(pa)}, max "
                  f"{max(pa)}")
    print("yields per scheduler pass: " + hist(collections.Counter(yields_per_pass)))
    if args.play_frame is not None:
        print("yields per pass from the play frame: " + hist(collections.Counter(
            n for fr, n in yields_pass_frame if fr >= args.play_frame)))
    rf = [resumes_by_frame.get(fr, 0) for fr in frames]
    print(f"resumes per frame mean {statistics.mean(rf):.2f}, max {max(rf)}")
    print(f"call sites seen {len(yield_site)}:")
    for site, n in yield_site.most_common():
        print(f"  {site:08X} {n}")
    print("sleep argument a0: " + ", ".join(f"{a:#x}: {n}" for a, n in sorted(yield_arg.items())))
    print("yields by task function: " + ", ".join(
        f"{name}/{func:08X} {n}" for (name, func), n in yield_func.most_common()))

    print("\n== 3. stacks")
    sizes = collections.Counter(t["size"] for t in history)
    print("stack block sizes: " + ", ".join(f"{s:#x} x{n}" for s, n in sizes.items()))
    print("fifth argument (extra stack) values: "
          + hist(collections.Counter(t["a4"] for t in history)))
    for (name, func), ts in sorted(by_func.items(), key=lambda kv: kv[1][0]["created"]):
        dy = max(t["base"] + t["size"] - t["min_sp"] for t in ts)
        dd = [t["depth_diff"] for t in ts if t["depth_diff"] is not None]
        maps = {t.get("map") for t in ts if t.get("map")}
        diff = f"{max(dd):#x}" if dd else "-"
        print(f"  {name or '?':8} func {func:08X}: {len(ts)} stacks; deepest sp at a yield "
              f"{dy:#x} bytes below the block end; deepest changed word above base + 16 "
              f"{diff} bytes below it")
        lowbase = [t for t in ts if t.get("map", ".")[:1] == "x"]
        if lowbase:
            print(f"    {len(lowbase)} of its stacks had a change in the first 256 bytes")
    print(f"sp outside the current task's block at Y/X: {len(sp_violations)} "
          + " ".join(str(v) for v in sp_violations[:10]))
    print(f"resume with a saved sp other than the task's last yield sp: {len(resume_mismatch)} "
          + " ".join(str(v) for v in resume_mismatch[:10]))

    print("\n== 4. scheduler passes")
    pp = [passes_by_frame.get(fr, 0) for fr in frames]
    print(f"passes {sum(pp)} over {len(pp)} frames: per frame mean {statistics.mean(pp):.3f}, max "
          f"{max(pp)}; histogram {hist(collections.Counter(pp))}")
    print("frames between pass starts: " + hist(collections.Counter(pass_frames)))
    print("vblank count 0x801A2FE8 at pass start: " + hist(pass_v))
    print("target 0x801A36BC: " + hist(pass_tgt))
    print("timer flag 0x801A36C4: " + hist(tf_values) + f"; ioctl 0x7403 calls {i3}, 0x7407 {i7}")
    if waits:
        print(f"cycles waiting per pass: mean {statistics.mean(waits):,.0f}, median "
              f"{statistics.median(waits):,.0f}, min {min(waits):,}, max {max(waits):,} "
              f"({statistics.mean(waits) / rate * 1000:.2f} ms mean); passes with no wait "
              f"(count already at target) {no_wait}")
    print("writes to 0x801A2FE8 by pc (next pc, see README): " + ", ".join(
        f"{pc:08X} {ctx} {n}" for (pc, ctx), n in v_by_pc.most_common()))

    print("\n== 5. exceptions")
    print("exception codes: " + ", ".join(f"{c}: {n}" for c, n in sorted(exc_cause.items())))
    print("context at entry (by sp): " + ", ".join(f"{k} {v}" for k, v in exc_ctx.items()))
    print("context and EPC region: " + ", ".join(f"{a}/{b} {n}" for (a, b), n in exc_epc_where.items()))
    for k, v in sorted(k1_checks.items()):
        print(f"  {k}: {v}")
    print(f"k1 anomalies: {len(k1_bad)} " + " ".join(str(x) for x in k1_bad[:10]))
    print("task events inside an exception: " + (", ".join(f"{k} {v}" for k, v in in_exc_events.items()) or "none"))
    print("writes to 0x801A3110-0x801A311B by region/context: " + ", ".join(
        f"{a}/{b} {n}" for (a, b), n in w_where.items()))
    print("  by address and next pc:")
    for (a, pc), n in sorted(w_by_pc.items()):
        print(f"    {a:08X} {pc:08X} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
