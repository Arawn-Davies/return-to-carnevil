"""Name functions after the debug strings they print.

A string in the data section of the form "name(): ..." or "name: ..." whose
address is formed (lui + addiu/ori) by exactly one function names that
function. A function whose strings give two different names, or a name that
two functions would get, is left alone, and the entry keeps `_start`.
"""
import bisect
import re

_DEBUG_NAME = re.compile(rb"([A-Za-z_][A-Za-z0-9_]*)(?:\(\))?: ")


def _c_string(image, addr, limit=256):
    """The NUL-terminated bytes at addr, or None if no NUL within limit."""
    out = bytearray()
    for a in range(addr, min(addr + limit, image.end_addr())):
        b = image.data[a - image.load_addr]
        if b == 0:
            return bytes(out)
        out.append(b)
    return None


def name_from_strings(image, analysis, pairs):
    """Rename functions of an Analysis in place; pairs are (address of the low
    half, value) of the lui pairs in reached code. Returns how many were named."""
    starts = [f.addr for f in analysis.functions]
    by_addr = {f.addr: f for f in analysis.functions}
    referrers = {}  # string address -> starts of the functions forming it
    for addr, value in pairs:
        if not analysis.text_end <= value < image.end_addr():
            continue
        i = bisect.bisect_right(starts, addr) - 1
        if i >= 0:
            referrers.setdefault(value, set()).add(starts[i])
    candidates = {}  # function start -> names its strings give
    for value, owners in referrers.items():
        text = _c_string(image, value)
        match = _DEBUG_NAME.match(text) if text else None
        if match and len(owners) == 1:
            candidates.setdefault(next(iter(owners)), set()).add(match.group(1).decode("ascii"))
    claims = {}  # name -> functions it would name
    for addr, names in candidates.items():
        if len(names) == 1:
            claims.setdefault(next(iter(names)), []).append(addr)
    named = 0
    for name, addrs in claims.items():
        if len(addrs) == 1 and by_addr[addrs[0]].name != "_start" and not name.startswith("func_"):
            by_addr[addrs[0]].name = name
            named += 1
    return named
