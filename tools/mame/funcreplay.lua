-- Capture pure functions of GAME.EXE in MAME during attract mode, for replay against the
-- recompiled code (runtime/stub/replay.c, test_against_mame). A capture is the machine state
-- before one call of a function and after it returns.
--
-- Input: $CARNEVIL_PURE, the output of `python3 -m carnevil_tools pure-functions` (one
-- address per line). Output: $CARNEVIL_MAME_OUT (default: the current directory), with the
-- layout of tools/mame/README.md: base-NNN.bin (8 MB RAM images the diffs are against),
-- <ADDR>/capture.txt, <ADDR>/before.diff and <ADDR>/after.diff, and funcreplay-log.txt (one
-- line per capture or dropped attempt, then a summary). Everything but the log is game data:
-- share only. $CARNEVIL_TMP (default /tmp/funcreplay) holds the RAM saves while a capture is
-- written. $CARNEVIL_REPLAY_SECONDS (default 90): emulated seconds after GAME.EXE's entry;
-- $CARNEVIL_REPLAY_MAX (default 200): captures (after that the run goes on to the time limit,
-- counting first hits only); $CARNEVIL_REPLAY_TRIES (default 3): attempts per function.
--
-- From GAME.EXE's entry on, a breakpoint on every listed function. At a hit, with no capture
-- in flight:
--   1. FCSR's condition codes are not in MAME's state entries (CCR31 holds FCSR without
--      them), so FCSR is read by running one injected `cfc1 k0,$31` at INJECT, with Status IE
--      cleared for those two instructions; k0, Status, the two words at INJECT and the PC are
--      put back before anything is recorded;
--   2. the registers (general, hi, lo, FP halves, FCSR) and the 8 MB of RAM are saved, a
--      breakpoint is set on ra, and the exception vectors' breakpoints are enabled;
--   3. at ra with sp back at its entry value (a stop at ra with another sp is a recursive
--      call's return, and is passed), FCSR is read the same way and the state saved again.
-- An attempt is dropped, and counted, if an exception vector is reached in step 3 (the
-- handlers write RAM, and interrupts are not part of a function) or the function has not
-- returned after TIMEOUT emulated seconds; the function is tried again at a later hit, up to
-- TRIES attempts. Hits of other functions while a capture is in flight are passed.
--
-- Run with -nodrc (the script sets the PC from the debugger's instruction hook) and -debug
-- -debugger none (tools/mame/README.md).
local LIST = assert(os.getenv("CARNEVIL_PURE"), "set CARNEVIL_PURE to the pure-functions list")
local OUT = (os.getenv("CARNEVIL_MAME_OUT") or ".") .. "/"
local TMP = (os.getenv("CARNEVIL_TMP") or "/tmp/funcreplay") .. "/"
local SECONDS = tonumber(os.getenv("CARNEVIL_REPLAY_SECONDS") or "90")
local MAX = tonumber(os.getenv("CARNEVIL_REPLAY_MAX") or "200")
local TRIES = tonumber(os.getenv("CARNEVIL_REPLAY_TRIES") or "3")
local TIMEOUT = 1.0
local GAME_ENTRY = 0x800C4000
local INJECT = 0x807FFFF8           -- two words at the top of RAM, never run as code
local CFC1_K0 = 0x445AF800          -- cfc1 k0,$31
local VECTORS = {0x80000000, 0x80000080, 0x80000100, 0xA0000100, 0x80000180, 0x80000200}
local BLOCK = 256                   -- diff granularity (bytes)
local REBASE = 1024 * 1024          -- a before-diff larger than this starts a new base

local machine = manager.machine
local dbg = machine.debugger
local cpu = machine.devices[":maincpu"]
local mem = cpu.spaces["program"]
local st = {}
for name, entry in pairs(cpu.state) do st[name] = entry end
local ABI = {"zero", "at", "v0", "v1", "a0", "a1", "a2", "a3", "t0", "t1", "t2", "t3", "t4",
             "t5", "t6", "t7", "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "t8", "t9",
             "k0", "k1", "gp", "sp", "fp", "ra"}
local R = {}
for i = 0, 31 do R[i] = assert(st[ABI[i + 1]], "no state entry " .. ABI[i + 1]) end
local PC, SR, K0 = st["PC"], st["SR"], R[26]

-- 64-bit entries are read through their text (insnvec.lua explains why)
local function low(entry) return tonumber(tostring(entry):sub(-8), 16) end
local function high(entry) return tonumber(tostring(entry):sub(-16, -9), 16) end
local function sx(v)
  v = v & 0xFFFFFFFF
  if v >= 0x80000000 then v = v - 0x100000000 end
  return v
end
local function phys(a) return a & 0x1FFFFFFF end
local function now() return machine.time:as_double() end

-- --- input ----------------------------------------------------------------------------------

local targets, order = {}, {}
for line in io.lines(LIST) do
  local a = line:match("^%s*0x(%x+)%s*$")
  if a then
    a = tonumber(a, 16)
    targets[a] = {tries = 0, hits = 0, done = false}
    order[#order + 1] = a
  end
end

-- --- files ----------------------------------------------------------------------------------

local function read_file(path)
  local f = assert(io.open(path, "rb"))
  local s = f:read("a")
  f:close()
  return s
end

local function write_file(path, data)
  local f = assert(io.open(path, "wb"))
  f:write(data)
  f:close()
end

local function save_ram(path)
  dbg:command("save " .. path .. ",80000000,800000")
  local s = read_file(path)
  assert(#s == 0x800000, "RAM save " .. path .. " is " .. #s .. " bytes")
  return s
end

-- Records (u32 offset, u32 length, then the bytes of new) for each run of BLOCK-sized blocks
-- where new differs from old; little-endian.
local function diff(old, new)
  local out, i, n = {}, 1, #new
  while i <= n do
    if old:sub(i, i + BLOCK - 1) ~= new:sub(i, i + BLOCK - 1) then
      local j = i + BLOCK
      while j <= n and old:sub(j, j + BLOCK - 1) ~= new:sub(j, j + BLOCK - 1) do
        j = j + BLOCK
      end
      out[#out + 1] = string.pack("<I4I4", i - 1, j - i) .. new:sub(i, j - 1)
      i = j
    else
      i = i + BLOCK
    end
  end
  return table.concat(out)
end

local log = assert(io.open(OUT .. "funcreplay-log.txt", "w"))
local function note(line)
  log:write(line, "\n")
  log:flush()
end

-- --- breakpoints ----------------------------------------------------------------------------

-- Breakpoint commands take numbers in hexadecimal, indices included.
local function bp_command(verb, k) dbg:command(string.format("%s %X", verb, k)) end

-- Address -> the index of the newest breakpoint there.
local function bp_indices()
  local out = {}
  for k, bp in pairs(cpu.debug:bplist()) do
    local a = bp.address & 0xFFFFFFFF
    if out[a] == nil or k > out[a] then out[a] = k end
  end
  return out
end

local function bpset(addr) dbg:command(string.format("bpset %X", addr)) end

local vector_bps, is_vector = {}, {}
local function vectors(enable)
  for _, k in ipairs(vector_bps) do bp_command(enable and "bpenable" or "bpdisable", k) end
end

-- True when the word before addr is a branch or jump: then addr is also a delay slot, and a
-- stop there may be one with a branch pending, where setting the PC is not safe.
local function after_transfer(addr)
  local w = mem:read_u32(phys(addr - 4))
  local op = w >> 26
  if op == 0 then return (w & 0x3F) == 8 or (w & 0x3F) == 9 end      -- jr, jalr
  if op == 17 then return (w >> 21 & 31) == 8 end                    -- bc1f/bc1t(l)
  return op == 1 or (op >= 2 and op <= 7) or (op >= 20 and op <= 23)  -- regimm, j, b*
end

-- --- the capture ----------------------------------------------------------------------------

local function hex_list(values)
  local out = {}
  for i, v in ipairs(values) do out[i] = string.format("%08X", v) end
  return table.concat(out, " ")
end

-- The registers as capture.txt lines, prefixed tag; fcsr as the injected cfc1 read it.
local function state_lines(tag, fcsr)
  local r, fpr = {}, {}
  for i = 0, 31 do r[i + 1] = low(R[i]) end
  for n = 0, 30, 2 do
    fpr[n + 1] = low(st["FPR" .. n])
    fpr[n + 2] = high(st["FPR" .. n])
  end
  return string.format("%s r %s\n%s hi %08X\n%s lo %08X\n%s fpr %s\n%s fcsr %08X\n", tag,
                       hex_list(r), tag, low(st["HI"]), tag, low(st["LO"]), tag,
                       hex_list(fpr), tag, fcsr)
end

-- state: "idle", "inject" (the cfc1 is running), "running" (the function is)
local state, cap = "idle", nil
local base, base_name, bases = nil, nil, 0
local captured, dropped_exception, dropped_timeout = 0, 0, 0
local started, done, entry_time = false, false, 0
local clock0 = os.time()

-- Run `cfc1 k0,$31` at INJECT with interrupts off; on_inject(fcsr) runs at INJECT + 4 with
-- the machine as it was before (PC = resume).
local function inject(resume, then_do)
  cap.saved = {k0 = low(K0), k0_high = high(K0), sr = low(SR), w0 = mem:read_u32(phys(INJECT)),
               w1 = mem:read_u32(phys(INJECT + 4)), resume = resume, then_do = then_do}
  mem:write_u32(phys(INJECT), CFC1_K0)
  mem:write_u32(phys(INJECT + 4), 0)
  SR.value = cap.saved.sr & ~1
  PC.value = sx(INJECT)
  state = "inject"
end

local function uninject()
  local s = cap.saved
  local fcsr = low(K0)
  K0.value = (s.k0_high << 32) | s.k0
  SR.value = s.sr
  mem:write_u32(phys(INJECT), s.w0)
  mem:write_u32(phys(INJECT + 4), s.w1)
  PC.value = sx(s.resume)
  assert(low(K0) == s.k0 and high(K0) == s.k0_high and low(SR) == s.sr and low(PC) == s.resume,
         "the injection was not undone")
  return fcsr
end

local function finish()
  done = true
  local hit, uncaptured = 0, {}
  for _, a in ipairs(order) do
    local t = targets[a]
    if t.hits > 0 then hit = hit + 1 end
    if t.hits > 0 and not t.done then uncaptured[#uncaptured + 1] = string.format("%08X", a) end
  end
  note("hit, not captured: " .. table.concat(uncaptured, " "))
  note(string.format("summary: %d candidates, %d hit, %d captured, %d attempts dropped for an "
                     .. "exception, %d for a timeout, %d bases; emulated %.1f s after the "
                     .. "entry, wall %d s", #order, hit, captured, dropped_exception,
                     dropped_timeout, bases, now() - entry_time, os.time() - clock0))
  log:close()
  print(string.format("FUNCREPLAY done: %d candidates, %d hit, %d captured, %d dropped "
                      .. "(exception), %d dropped (timeout), wall %d s", #order, hit, captured,
                      dropped_exception, dropped_timeout, os.time() - clock0))
  machine:exit()
end

local function drop(why)
  local t = targets[cap.addr]
  if why == "exception" then dropped_exception = dropped_exception + 1
  else dropped_timeout = dropped_timeout + 1 end
  note(string.format("dropped %08X attempt %d: %s, emulated %.3f s", cap.addr, t.tries, why,
                     now() - entry_time))
  bp_command("bpclear", cap.ra_bp)
  vectors(false)
  if t.tries >= TRIES then bp_command("bpdisable", t.bp) end
  state, cap = "idle", nil
end

local function write_capture(after_fcsr)
  local addr = cap.addr
  local after = save_ram(TMP .. "after.bin")
  local dir = string.format("%s%08X/", OUT, addr)
  assert(os.execute('mkdir -p "' .. dir .. '"'), "mkdir " .. dir)
  local before_diff = base and diff(base, cap.before_ram)
  if not base or #before_diff > REBASE then
    bases = bases + 1
    base, base_name = cap.before_ram, string.format("base-%03d.bin", bases)
    write_file(OUT .. base_name, base)
    before_diff = ""
  end
  write_file(dir .. "before.diff", before_diff)
  write_file(dir .. "after.diff", diff(cap.before_ram, after))
  write_file(dir .. "capture.txt", string.format(
    "# tools/mame/funcreplay.lua: one call of %08X, MAME 0.276 -nodrc\naddr %08X\nbase ../%s\n"
    .. "attempt %d\ntime %.6f\n", addr, addr, base_name, targets[addr].tries, cap.time)
    .. cap.before_lines .. state_lines("after", after_fcsr))
  captured = captured + 1
  targets[addr].done = true
  note(string.format("captured %08X attempt %d, emulated %.3f s", addr, targets[addr].tries,
                     cap.time))
end

local function on_stop()
  local pc = low(PC)
  if not started then
    if pc ~= GAME_ENTRY then
      dbg.execution_state = "run"  -- the debugger's initial stop
      return
    end
    started, entry_time = true, now()
    os.execute('mkdir -p "' .. TMP .. '"')
    for _, a in ipairs(VECTORS) do
      bpset(a)
      is_vector[a] = true
    end
    bpset(INJECT + 4)
    local armed, skipped = 0, {}
    for _, a in ipairs(order) do
      if after_transfer(a) then
        targets[a].done = true
        skipped[#skipped + 1] = string.format("%08X", a)
      else
        bpset(a)
        armed = armed + 1
      end
    end
    local index = bp_indices()
    for _, a in ipairs(VECTORS) do vector_bps[#vector_bps + 1] = index[a] end
    vectors(false)
    for _, a in ipairs(order) do targets[a].bp = index[a] end
    note(string.format("armed %d of %d functions at GAME.EXE entry, emulated %.3f s; not armed "
                       .. "(the word before is a branch or jump): %s", armed, #order,
                       entry_time, table.concat(skipped, " ")))
    dbg.execution_state = "run"
    return
  end
  if state == "inject" and pc == INJECT + 4 then
    local fcsr = uninject()
    cap.saved.then_do(fcsr)
  elseif state == "running" and pc == cap.ra and low(R[29]) == cap.sp then
    if cap.exception then
      drop("exception")
    else
      inject(cap.ra, function(fcsr)
        write_capture(fcsr)
        bp_command("bpclear", cap.ra_bp)
        vectors(false)
        bp_command("bpdisable", targets[cap.addr].bp)
        state, cap = "idle", nil
      end)
    end
  elseif state == "running" and is_vector[pc] then
    cap.exception = true
    vectors(false)
  elseif state == "idle" and targets[pc] and captured < MAX and not targets[pc].done
         and targets[pc].tries < TRIES then
    local t = targets[pc]
    t.tries, t.hits = t.tries + 1, t.hits + 1
    cap = {addr = pc, time = now() - entry_time}
    inject(pc, function(fcsr)
      cap.before_lines = state_lines("before", fcsr)
      cap.before_ram = save_ram(TMP .. "before.bin")
      cap.ra, cap.sp = low(R[31]), low(R[29])
      bpset(cap.ra)
      cap.ra_bp = assert(bp_indices()[cap.ra], "no breakpoint at ra")
      cap.t0 = now()
      vectors(true)
      state = "running"
    end)
  elseif targets[pc] then  -- while another capture is in flight, or past MAX captures
    targets[pc].hits = targets[pc].hits + 1
    if state == "idle" and captured >= MAX then bp_command("bpdisable", targets[pc].bp) end
  end
  if not done then dbg.execution_state = "run" end
end

FUNCREPLAY_SUB = emu.register_periodic(function()
  if done then return end
  if dbg.execution_state == "stop" then
    local ok, err = pcall(on_stop)
    if not ok then
      print("FUNCREPLAY script error: " .. tostring(err))
      note("script error: " .. tostring(err))
      finish()
    end
  elseif state == "running" and now() - cap.t0 > TIMEOUT then
    dbg.execution_state = "stop"
    local ok, err = pcall(drop, "timeout")
    if not ok then note("script error: " .. tostring(err)) finish() return end
    dbg.execution_state = "run"
  elseif started and state == "idle" and now() - entry_time > SECONDS then
    finish()
  end
end)
dbg:command(string.format("bpset %X", GAME_ENTRY))
print(string.format("FUNCREPLAY armed: %d functions from %s", #order, LIST))
