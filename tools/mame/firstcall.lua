-- GAME.EXE from its entry to its first loader call (test_against_mame, the smoke run).
--
-- Boots normally to GAME.EXE's entry (0x800C4000) and records the registers there, then
-- stops at the first instruction run in the stub block 0x800C408C-0x800C42CC (a breakpoint
-- on each of its 72 slots; `_start` fills the block with the loader's `j` slots before
-- anything calls it). There it records the slot (pc - 0x800C408C) and the registers, saves
-- the 8 MB of RAM and quits. Output, in $CARNEVIL_MAME_OUT (default: the current directory):
--   ram-at-first-loader-call.bin  the RAM (game data: share only, never git)
--   first-loader-call.txt         "entry r R0 .. R31", "entry hi H", "entry lo L",
--                                 "entry fpr F0 .. F31", the same four prefixed "call", then
--                                 "slot S" and "exceptions N" (hexadecimal but N)
--
-- Exceptions: a breakpoint on each exception vector (0x80000000, 0x80000080, 0x80000100,
-- 0xA0000100, 0x80000180, 0x80000200) counts and logs every exception taken between the
-- entry and the call (Cause, EPC). A write watchpoint on 0x800C4000-0x807FFFFF that fires
-- only for code below GAME.EXE (pc below 0x800C4000: before the first loader call that is
-- the loader's exception code) logs each such write to the debugger log, so the report can
-- say whether an exception touched the compared range. A control watchpoint on 0x801A4228
-- (the first byte `_start` clears), with the opposite condition, shows that the watchpoints
-- and their condition work: it must log one write. Count the "FIRSTCALL loader write" and
-- "FIRSTCALL control write" lines of debug.log.
--
-- Run with -nodrc -debug -debugger none -debuglog (tools/mame/README.md).
local OUT = (os.getenv("CARNEVIL_MAME_OUT") or ".") .. "/"
local GAME_ENTRY, STUB, STUB_END = 0x800C4000, 0x800C408C, 0x800C42CC
local VECTORS = {0x80000000, 0x80000080, 0x80000100, 0xA0000100, 0x80000180, 0x80000200}

local machine = manager.machine
local dbg = machine.debugger
local cpu = machine.devices[":maincpu"]
local st = {}
for name, entry in pairs(cpu.state) do st[name] = entry end
local ABI = {"zero", "at", "v0", "v1", "a0", "a1", "a2", "a3", "t0", "t1", "t2", "t3", "t4",
             "t5", "t6", "t7", "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "t8", "t9",
             "k0", "k1", "gp", "sp", "fp", "ra"}
-- 64-bit entries are read through their text (insnvec.lua explains why)
local function low(entry) return tonumber(tostring(entry):sub(-8), 16) end
local function high(entry) return tonumber(tostring(entry):sub(-16, -9), 16) end

local is_vector = {}
for _, a in ipairs(VECTORS) do is_vector[a] = true end
local started, done, exceptions = false, false, 0
local clock0 = os.time()

local function hex_list(values)
  local out = {}
  for i, v in ipairs(values) do out[i] = string.format("%08X", v) end
  return table.concat(out, " ")
end

-- The state as the four lines of first-loader-call.txt, each prefixed tag; and the r list.
local function state_lines(tag)
  local r, fpr = {}, {}
  for i = 0, 31 do r[i + 1] = low(st[ABI[i + 1]]) end
  for n = 0, 30, 2 do
    fpr[n + 1] = low(st["FPR" .. n])
    fpr[n + 2] = high(st["FPR" .. n])
  end
  return string.format("%s r %s\n%s hi %08X\n%s lo %08X\n%s fpr %s\n", tag, hex_list(r), tag,
                       low(st["HI"]), tag, low(st["LO"]), tag, hex_list(fpr)), r
end

local entry_lines

local function finish(pc)
  done = true
  local slot = pc - STUB
  local lines, r = state_lines("call")
  dbg:command("save " .. OUT .. "ram-at-first-loader-call.bin,80000000,800000")
  local f = assert(io.open(OUT .. "first-loader-call.txt", "w"))
  f:write(entry_lines, lines)
  f:write(string.format("slot %03X\nexceptions %d\n", slot, exceptions))
  f:close()
  print(string.format("FIRSTCALL slot %03X a0 %08X a1 %08X a2 %08X a3 %08X ra %08X sp %08X, "
                      .. "%d exceptions, emulated %.6f s, wall %d s", slot, r[5], r[6], r[7],
                      r[8], r[32], r[30], exceptions, machine.time:as_double(),
                      os.time() - clock0))
  machine:exit()
end

local function on_stop()
  local pc = low(st["PC"])
  if not started then
    if pc ~= GAME_ENTRY then
      dbg.execution_state = "run"  -- the debugger's initial stop
      return
    end
    started = true
    entry_lines = state_lines("entry")
    print(string.format("FIRSTCALL at GAME.EXE entry, emulated %.6f s",
                        machine.time:as_double()))
    for a = STUB, STUB_END - 8, 8 do dbg:command(string.format("bpset %X", a)) end
    for _, a in ipairs(VECTORS) do dbg:command(string.format("bpset %X", a)) end
    -- watchpoints take physical addresses; pc is sign-extended to 64 bits
    dbg:command(string.format('wpset C4000,%X,w,(pc & ffffffff) < 800C4000,'
                              .. '{printf "FIRSTCALL loader write pc=%%08X addr=%%08X",pc,'
                              .. 'wpaddr; go}', 0x80800000 - 0x800C4000))
    dbg:command('wpset 1A4228,1,w,(pc & ffffffff) >= 800C4000,'
                .. '{printf "FIRSTCALL control write pc=%08X addr=%08X",pc,wpaddr; go}')
    dbg.execution_state = "run"
    return
  end
  if pc >= STUB and pc < STUB_END then
    finish(pc)
    return
  end
  if is_vector[pc] then
    exceptions = exceptions + 1
    local line = string.format("FIRSTCALL exception %d at vector %08X: Cause %08X EPC %08X",
                               exceptions, pc, low(st["Cause"]), low(st["EPC"]))
    print(line)
    dbg:command('printf "' .. line .. '"')
  end
  dbg.execution_state = "run"
end

FIRSTCALL_SUB = emu.register_periodic(function()
  if done or dbg.execution_state ~= "stop" then return end
  local ok, err = pcall(on_stop)
  if not ok then
    print("FIRSTCALL script error: " .. tostring(err))
    done = true
    machine:exit()
  end
end)
dbg:command(string.format("bpset %X", GAME_ENTRY))
print("FIRSTCALL armed")
