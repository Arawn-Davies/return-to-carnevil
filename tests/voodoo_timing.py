"""The Voodoo's video timing as the tests expect it (docs/analysis/hardware-usage.md, "The
Voodoo model", "Video timing and retrace"): the game's timing registers (measured,
voodoo-ramp.txt), the time of GAME.EXE's entry since power-on (measured), and the first
retrace after the timing is programmed by MAME's rule, written out independently of
runtime/hw/voodoo_null.c. Times are picoseconds since power-on unless they say ns."""

ENTRY_PS = 5003569666667          # GAME.EXE's entry since power-on (hw.h, VOODOO_ENTRY_PS)
ENTRY_NS = (ENTRY_PS + 500) // 1000  # the same in whole ns, for replaying capture times
VOODOO = 0xA8000000
# the game's video-timing writes at 6.5675 s: hSync, vSync, backPorch, videoDimensions
GAME_TIMING = ((0x220, 0x01CF002D), (0x224, 0x011F0004), (0x208, 0x00110030),
               (0x20C, 0x0100018F))
AS = 10 ** 18
DEFAULT_FRAME_AS = AS // 57       # MAME's seattle.cpp screen, 57 Hz from power-on


def ps(attoseconds):
    return (attoseconds + 500_000) // 1_000_000


def retrace_after(t_ps, timing=GAME_TIMING):
    """(first retrace start, its length, the frame) once `timing` is written at t_ps on a
    card whose screen still runs MAME's default 57 Hz frames from power-on."""
    regs = dict(timing)
    hs, vs, bp, dim = regs[0x220], regs[0x224], regs[0x208], regs[0x20C]
    vsyncoff = (vs >> 16) & 0xFFF
    vtotal = vsyncoff + (vs & 0xFFF)
    htotal = ((hs >> 16) & 0x3FF) + 1 + (hs & 0xFF) + 1
    vbp, vvis = (bp >> 16) & 0xFF, (dim >> 16) & 0x3FF
    vis_end = min(vbp + max(vvis - 1, 0), vtotal - 1) + 1
    old = ps(DEFAULT_FRAME_AS)
    periods = [AS // rate * vtotal for rate in (15750, 25000, 31500)]
    diffs = [abs(p - DEFAULT_FRAME_AS) for p in periods]
    if diffs[0] < diffs[1] and diffs[0] < diffs[2]:
        frame_as = periods[0]
    elif diffs[1] < diffs[2]:
        frame_as = periods[1]
    else:
        frame_as = periods[2]
    frame = ps(frame_as)
    ref = t_ps // old * old
    if t_ps - ref >= frame:
        ref = t_ps
    scan, pixel = frame // vtotal, frame // (vtotal * htotal)

    def until(now, row):
        cur, target = now - ref, ((row + vtotal - vis_end) % vtotal) * scan
        if target <= cur + pixel // 2:
            target += frame
        while target <= cur:
            target += frame
        return ref + target

    first = until(t_ps, vsyncoff)
    return first, until(first, 0) - first, frame


def to_ns(t_ps):
    """The virtual time (ns) at or after a card time."""
    return max(0, -(-(t_ps - ENTRY_PS) // 1000))


def timing_writes(wr):
    """The commands writing the game's video timing with `wr(size, addr, value)`."""
    return [wr(4, VOODOO + reg, value) for reg, value in GAME_TIMING]
