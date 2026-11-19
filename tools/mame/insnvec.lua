-- Run the instruction test cases of carnevil_tools/vectors.py on MAME's R5000 core and
-- record the state after each (tests/vectors/insn.json is merged from the output).
--
-- Input: $CARNEVIL_VECTORS, the file `python3 -m carnevil_tools vectors --write-cases`
-- writes. Output: $CARNEVIL_MAME_OUT/insnvec.jsonl (default: the current directory), one JSON
-- line per case. Run with -nodrc (the interpreter: the script sets the PC from inside the
-- debugger's instruction hook, which the recompiler would not see).
--
-- The machine boots normally to GAME.EXE's entry (0x800C4000). There the script clears
-- Status IE (and EXL, ERL), so no interrupt runs, and from then on, for each case:
--   * zero the code page 0x80100000-0x80100FFF and the scratch page 0x80200000-0x80200FFF,
--     write the case's words at 0x80100000 and its memory words;
--   * at RA: cfc1 k0,$31; nop (FCSR is read back through k0 at RA + 8);
--   * at RA + 0x80: ctc1 k0,$31; lui k0,hi; j 0x80100000; ori k0,k0,lo -- FCSR's condition
--     bits live outside the CCR31 state entry in MAME, so they are set with ctc1, and the
--     case's own k0 is reloaded on the way into the case;
--   * set the general registers (sign-extended), HI, LO and the FP registers, k0 = FCSR,
--     PC = RA + 0x80, and run;
--   * at the breakpoint at RA, record the registers and the non-zero words of the scratch
--     page; at RA + 8, record FCSR and write the line.
-- A breakpoint on each exception vector (0x80000000, 0x80000080, 0x80000180, 0x80000200 and
-- the BEV ones) fails the case with Cause and EPC. A case that has not finished after 0.05
-- emulated seconds is stopped and failed. Only the scratch page is read back: nothing else of
-- the game's RAM, and no code, goes into the output.
--
-- Under FR = 0, MAME keeps FP half n (even) in the low 32 bits of state entry FPR<n> and half
-- n + 1 in its high 32 bits; FPR<odd> is unused (mips3.cpp get_cop1_reg32/set_cop1_reg32).
local IN = assert(os.getenv("CARNEVIL_VECTORS"), "set CARNEVIL_VECTORS to the cases file")
local OUT = (os.getenv("CARNEVIL_MAME_OUT") or ".") .. "/insnvec.jsonl"

-- A JSON reader for the input's subset: objects, arrays, integers, strings without escapes.
local function json_decode(s)
  local pos = 1
  local value
  local function ws() pos = s:find("[^ \t\r\n]", pos) or #s + 1 end
  local function str()
    local e = s:find('"', pos + 1, true)
    local v = s:sub(pos + 1, e - 1)
    pos = e + 1
    return v
  end
  local function seq(close, item)
    pos = pos + 1
    ws()
    if s:sub(pos, pos) == close then pos = pos + 1 return end
    while true do
      item()
      ws()
      local c = s:sub(pos, pos)
      pos = pos + 1
      if c == close then return end
      assert(c == ",", "bad JSON at " .. pos)
    end
  end
  function value()
    ws()
    local c = s:sub(pos, pos)
    if c == "{" then
      local t = {}
      seq("}", function() ws() local k = str() ws() pos = pos + 1 t[k] = value() end)
      return t
    elseif c == "[" then
      local t = {}
      seq("]", function() t[#t + 1] = value() end)
      return t
    elseif c == '"' then
      return str()
    end
    local num = assert(s:match("^-?%d+", pos), "bad JSON at " .. pos)
    pos = pos + #num
    return math.tointeger(tonumber(num))
  end
  return value()
end

local f = assert(io.open(IN, "r"))
local data = json_decode(f:read("a"))
f:close()
local stub, cases = data.stub, data.cases
local CODE, RA, SCRATCH, SCRATCH_SIZE = stub.code_base, stub.ra, stub.scratch, stub.scratch_size
local SETUP = RA + 0x80
local GAME_ENTRY = 0x800C4000
local VECTORS = {0x80000000, 0x80000080, 0x80000180, 0x80000200, 0xBFC00200, 0xBFC00280,
                 0xBFC00380, 0xBFC00400}
local TIMEOUT = 0.05

local machine = manager.machine
local dbg = machine.debugger
local cpu = machine.devices[":maincpu"]
local mem = cpu.spaces["program"]
-- Copy the state entries into a plain table: indexing cpu.state directly can return one of
-- the container's own methods instead (cpu.state["at"] is the container's `at`).
local st = {}
for name, entry in pairs(cpu.state) do st[name] = entry end
local ABI = {"zero", "at", "v0", "v1", "a0", "a1", "a2", "a3", "t0", "t1", "t2", "t3", "t4",
             "t5", "t6", "t7", "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "t8", "t9",
             "k0", "k1", "gp", "sp", "fp", "ra"}
local R, FPR = {}, {}
for i = 0, 31 do
  R[i] = assert(st[ABI[i + 1]] or st["R" .. i], "no state entry for r" .. i)
  FPR[i] = assert(st["FPR" .. i], "no state entry FPR" .. i)
end
local HI, LO, PC, SR, CAUSE, EPC = st["HI"], st["LO"], st["PC"], st["SR"], st["Cause"], st["EPC"]

-- Reading entry.value of a 64-bit register with bit 63 set gives a Lua float (sol pushes a
-- u64 above INT64_MAX as a number) and loses the low bits, so registers are read through the
-- entry's text, which is the full hex value ("%016X", "%08X").
local function low(entry) return tonumber(tostring(entry):sub(-8), 16) end
local function high(entry) return tonumber(tostring(entry):sub(-16, -9), 16) end
local function sx(v)
  v = v & 0xFFFFFFFF
  if v >= 0x80000000 then v = v - 0x100000000 end
  return v
end
local function phys(a) return a & 0x1FFFFFFF end

local out = assert(io.open(OUT, "w"))
local started, base_sr = false, nil
local idx, cur, rec, t0, timed_out = 0, nil, nil, 0, false
local failures, clock0 = 0, os.time()

local function emit(line)
  out:write(line, "\n")
  out:flush()
end

local function fail(msg)
  failures = failures + 1
  emit(string.format('{"name":"%s","error":"%s"}', cur.name, msg))
  print("INSNVEC FAIL " .. cur.name .. " " .. msg)
end

local done = false

local function finish()
  done = true
  out:close()
  print(string.format("INSNVEC done: %d cases, %d failed, %d s", #cases, failures,
                      os.time() - clock0))
  machine:exit()
end

local function start_case()
  idx = idx + 1
  if idx > #cases then
    finish()
    return
  end
  cur = cases[idx]
  for a = CODE, CODE + 0xFFC, 4 do mem:write_u32(phys(a), 0) end
  for a = SCRATCH, SCRATCH + SCRATCH_SIZE - 4, 4 do mem:write_u32(phys(a), 0) end
  for i, w in ipairs(cur.words) do mem:write_u32(phys(CODE + 4 * (i - 1)), w) end
  for _, aw in ipairs(cur.mem) do mem:write_u32(phys(aw[1]), aw[2]) end
  mem:write_u32(phys(RA), stub.cfc1_k0)
  local k0 = cur.r[27]
  mem:write_u32(phys(SETUP), stub.ctc1_k0)
  mem:write_u32(phys(SETUP + 4), stub.lui_k0 | (k0 >> 16))
  mem:write_u32(phys(SETUP + 8), stub.j_code)
  mem:write_u32(phys(SETUP + 12), stub.ori_k0 | (k0 & 0xFFFF))
  for i = 1, 31 do R[i].value = sx(cur.r[i + 1]) end
  R[26].value = sx(cur.fcsr)
  HI.value = sx(cur.hi)
  LO.value = sx(cur.lo)
  for n = 0, 30, 2 do FPR[n].value = (cur.fpr[n + 2] << 32) | cur.fpr[n + 1] end
  SR.value = base_sr
  PC.value = sx(SETUP)
  -- check the writes took: every register reads back as written (sign-extended)
  for i = 1, 31 do
    local want = i == 26 and cur.fcsr or cur.r[i + 1]
    local hi = want >= 0x80000000 and 0xFFFFFFFF or 0
    if low(R[i]) ~= want or high(R[i]) ~= hi then
      error(string.format("r%d reads back %s, wrote %08X", i, tostring(R[i]), want))
    end
  end
  for n = 0, 30, 2 do
    if low(FPR[n]) ~= cur.fpr[n + 1] or high(FPR[n]) ~= cur.fpr[n + 2] then
      error(string.format("FPR%d reads back %s", n, tostring(FPR[n])))
    end
  end
  if low(HI) ~= cur.hi or low(LO) ~= cur.lo or low(SR) ~= base_sr or low(PC) ~= SETUP then
    error("HI, LO, SR or PC did not take")
  end
  timed_out = false
  t0 = machine.time:as_double()
  dbg.execution_state = "run"
end

local function record()
  rec = {r = {}, fpr = {}, mem = {}}
  for i = 0, 31 do rec.r[i + 1] = low(R[i]) end
  for n = 0, 30, 2 do
    rec.fpr[n + 1] = low(FPR[n])
    rec.fpr[n + 2] = high(FPR[n])
  end
  rec.hi, rec.lo = low(HI), low(LO)
  for a = SCRATCH, SCRATCH + SCRATCH_SIZE - 4, 4 do
    local w = mem:read_u32(phys(a))
    if w ~= 0 then rec.mem[#rec.mem + 1] = string.format("[%d,%d]", a, w) end
  end
end

local function write_record(fcsr)
  emit(string.format('{"name":"%s","r":[%s],"hi":%d,"lo":%d,"fpr":[%s],"fcsr":%d,"mem":[%s]}',
                     cur.name, table.concat(rec.r, ","), rec.hi, rec.lo,
                     table.concat(rec.fpr, ","), fcsr, table.concat(rec.mem, ",")))
  if idx % 100 == 0 then print(string.format("INSNVEC %d/%d", idx, #cases)) end
end

local function on_stop()
  local pc = low(PC)
  if not started then
    if pc ~= GAME_ENTRY then return end  -- the boot's initial stop; the debugger resumes
    started = true
    base_sr = low(SR) & ~0x7  -- clear IE, EXL, ERL
    print(string.format("INSNVEC at GAME.EXE entry: SR %08X -> %08X, %d cases", low(SR),
                        base_sr, #cases))
    dbg:command(string.format("bpclear"))
    for _, a in ipairs({RA, RA + 8}) do dbg:command(string.format("bpset %X", a)) end
    for _, a in ipairs(VECTORS) do dbg:command(string.format("bpset %X", a)) end
    start_case()
    return
  end
  if pc == RA and not timed_out then
    record()
    dbg.execution_state = "run"
  elseif pc == RA + 8 and rec ~= nil and not timed_out then
    write_record(low(R[26]))
    rec = nil
    start_case()
  else
    local why = timed_out and "timeout" or "exception"
    fail(string.format("%s: stopped at %08X, Cause %08X, EPC %08X, SR %08X", why, pc,
                       low(CAUSE), low(EPC), low(SR)))
    rec = nil
    start_case()
  end
end

INSNVEC_SUB = emu.register_periodic(function()
  if done then
    return
  elseif dbg.execution_state == "stop" then
    local ok, err = pcall(on_stop)
    if not ok then  -- a script error: stop the run rather than record anything after it
      print("INSNVEC script error: " .. tostring(err))
      if cur ~= nil then fail("script error") end
      finish()
    end
  elseif started and not timed_out and cur ~= nil and machine.time:as_double() - t0 > TIMEOUT then
    timed_out = true
    dbg.execution_state = "stop"
  end
end)
dbg:command(string.format("bpset %X", GAME_ENTRY))
print("INSNVEC armed: " .. #cases .. " cases from " .. IN)
