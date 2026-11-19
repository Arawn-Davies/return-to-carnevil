-- What GAME.EXE asks the loader for, in order (docs/analysis/loader-calls.md).
--
-- Boots to GAME.EXE's entry and sets a breakpoint on each of the 72 slots of the stub block
-- 0x800C408C-0x800C42CC (`_start` fills it with the loader's `j` slots; the game calls 29
-- of them, boot-handoff.md). Runs $CARNEVIL_LOADER_SECONDS emulated seconds after the entry
-- (default 150) and quits. Output in $CARNEVIL_MAME_OUT (default: the current directory):
--
--   loader-calls-first.txt  the first $CARNEVIL_LOADER_FIRST calls (default 400), one line
--       each: "n N f F t T slot S a0 .. a3 ra sp v0 V0 v1 V1 [s0 "..."] [buf "..."]",
--       hexadecimal but n, f (the screen's frame number) and t (emulated seconds since
--       power-on);
--       v0/v1 at the return to ra with the same sp ("-" if it had not returned by the end),
--       s0 the string at a0 when a0 points at a short printable NUL-terminated string,
--       buf the bytes written for a write (slot 0x0C0) to fd 0-2
--   debug.log  (with -debuglog), after the first calls: "LC f=F c=C pc=P a0 a1 a2 a3 ra"
--       for every later slot call (printf actions, in order), and "LS ..." lines in the
--       format above for the slots still handled from Lua after the first calls: the opens
--       (0x028, 0x088), the file-system calls 0x038, 0x190, 0x1B8 and the write 0x0C0;
--       (LS lines are written at the return, so they follow the call's LC neighbours);
--       "UART f=F c=C pc=P d=D" for every write to the IOASIC UARTOUT register (physical
--       0x16000028, bus word 10) from power-on; "LT t=T f=F c=C" once an emulated second.
--
-- Run with -nodrc -debug -debugger none -debuglog (tools/mame/README.md).
local OUT = (os.getenv("CARNEVIL_MAME_OUT") or ".") .. "/"
local SECONDS = tonumber(os.getenv("CARNEVIL_LOADER_SECONDS") or "150")
local FIRST = tonumber(os.getenv("CARNEVIL_LOADER_FIRST") or "400")
local GAME_ENTRY, STUB, STUB_END = 0x800C4000, 0x800C408C, 0x800C42CC
local LUA_SLOTS = {[0x028] = true, [0x088] = true, [0x038] = true, [0x190] = true,
                   [0x1B8] = true, [0x0C0] = true}

local machine = manager.machine
local dbg = machine.debugger
local cpu = machine.devices[":maincpu"]
local cdbg = cpu.debug
local prog = cpu.spaces["program"]
local st = {}
for name, entry in pairs(cpu.state) do st[name] = entry end
local function low(entry) return tonumber(tostring(entry):sub(-8), 16) end
local function rd8(va) return prog:read_u8(va & 0x1FFFFFFF) end
local function is_ram(va) return va >= 0x80000000 and va < 0x80800000 end

local started, done, phase2 = false, false, false
local t0, clock0, next_tick = 0, os.time(), 0
local calls = {}          -- the first calls, in order
local pending = {}        -- return breakpoint index -> {entry, phase}
local slot_bp = {}        -- slot address -> breakpoint index
local function cstring(va, max)
  if not is_ram(va) then return nil end
  local out = {}
  for i = 0, max - 1 do
    local ch = rd8(va + i)
    if ch == 0 then return (#out > 0) and table.concat(out) or nil end
    if ch < 0x20 or ch >= 0x7F then return nil end
    out[#out + 1] = string.char(ch)
  end
  return nil
end

local function bytes(va, n)
  local out = {}
  for i = 0, math.min(n, 256) - 1 do
    local ch = rd8(va + i)
    if ch >= 0x20 and ch < 0x7F and ch ~= 0x22 and ch ~= 0x5C then out[#out + 1] = string.char(ch)
    else out[#out + 1] = string.format("\\x%02X", ch) end
  end
  return table.concat(out)
end

local function line_of(e)
  local s = string.format("n %d f %d t %.6f slot %03X a0 %08X a1 %08X a2 %08X a3 %08X ra %08X "
                          .. "sp %08X v0 %s v1 %s", e.n, e.f, e.t, e.slot, e.a[1], e.a[2],
                          e.a[3], e.a[4], e.ra, e.sp, e.v0 or "-", e.v1 or "-")
  if e.s0 then s = s .. ' s0 "' .. e.s0 .. '"' end
  if e.buf then s = s .. ' buf "' .. e.buf .. '"' end
  return s
end

-- debug.log line from Lua, in order with the printf lines; the text must hold no `"` or `%`
local function log(text)
  dbg:command('printf "' .. text:gsub('[%%"]', "?") .. '"')
end

local function write_first()
  local f = assert(io.open(OUT .. "loader-calls-first.txt", "w"))
  for _, e in ipairs(calls) do f:write(line_of(e), "\n") end
  f:close()
end

local ncalls = 0

local function on_slot(pc)
  ncalls = ncalls + 1
  local scr = machine.screens[":screen"]
  local e = {n = ncalls, f = scr and scr:frame_number() or -1, t = machine.time:as_double(),
             phase2 = phase2,
             slot = pc - STUB, a = {low(st["a0"]), low(st["a1"]), low(st["a2"]), low(st["a3"])},
             ra = low(st["ra"]), sp = low(st["sp"])}
  e.s0 = cstring(e.a[1], 64)
  if e.slot == 0x0C0 and e.a[1] <= 2 then e.buf = bytes(e.a[2], e.a[3]) end
  local idx = cdbg:bpset(e.ra, string.format("(sp & ffffffff) == %X", e.sp), "")
  pending[idx] = e
  if not phase2 then calls[#calls + 1] = e end
end

local function on_return(idx)
  local e = pending[idx]
  pending[idx] = nil
  cdbg:bpclear(idx)
  e.v0 = string.format("%08X", low(st["v0"]))
  e.v1 = string.format("%08X", low(st["v1"]))
  if e.phase2 then log("LS " .. line_of(e)) end
end

local function start_phase2()
  phase2 = true
  write_first()
  for addr, idx in pairs(slot_bp) do
    cdbg:bpclear(idx)
    if LUA_SLOTS[addr - STUB] then
      slot_bp[addr] = cdbg:bpset(addr, "1", "")
    else
      slot_bp[addr] = cdbg:bpset(addr, "1", 'printf "LC f=%d c=%d pc=%08X a0=%08X a1=%08X '
                                 .. 'a2=%08X a3=%08X ra=%08X",frame,totalcycles,pc,a0,a1,a2,a3,ra; go')
    end
  end
  print(string.format("LOADERCALLS first %d calls written, emulated %.3f s", #calls,
                      machine.time:as_double()))
end

local function finish()
  done = true
  write_first()
  dbg:command(string.format('printf "LT end t=%.6f f=%%d c=%%d",frame,totalcycles',
                            machine.time:as_double()))
  print(string.format("LOADERCALLS done, %d slot calls handled from Lua, emulated %.3f s, "
                      .. "wall %d s", ncalls, machine.time:as_double(), os.time() - clock0))
  machine:exit()
end

local function on_stop()
  local pc = low(st["PC"])
  if not started then
    if pc ~= GAME_ENTRY then dbg.execution_state = "run"; return end
    started = true
    t0 = machine.time:as_double()
    dbg:command("bpclear")
    for a = STUB, STUB_END - 8, 8 do slot_bp[a] = cdbg:bpset(a, "1", "") end
    dbg:command(string.format('printf "LT entry t=%.6f f=%%d c=%%d",frame,totalcycles', t0))
    print(string.format("LOADERCALLS armed at GAME.EXE entry, emulated %.6f s", t0))
  elseif pc >= STUB and pc < STUB_END then
    on_slot(pc)
    if not phase2 and #calls >= FIRST then start_phase2() end
  else
    local hit
    for idx, e in pairs(pending) do
      if e.ra == pc and e.sp == low(st["sp"]) then hit = idx; break end
    end
    if hit then on_return(hit) end
  end
  dbg.execution_state = "run"
end

LOADERCALLS_SUB = emu.register_periodic(function()
  if done then return end
  local ok, err = pcall(function()
    if dbg.execution_state == "stop" then on_stop() end
    if not started then return end
    local t = machine.time:as_double() - t0
    if t >= next_tick then
      next_tick = next_tick + 1
      dbg:command(string.format('printf "LT t=%.6f f=%%d c=%%d",frame,totalcycles',
                                machine.time:as_double()))
    end
    if t >= SECONDS then finish() end
  end)
  if not ok then
    print("LOADERCALLS script error: " .. tostring(err))
    done = true
    machine:exit()
  end
end)
-- the UART from power-on: a watchpoint on the physical address of UARTOUT
dbg:command('wpset 16000028,4,w,1,{printf "UART f=%d c=%d pc=%08X d=%02X",frame,totalcycles,'
            .. 'pc,wpdata; go}')
dbg:command(string.format("bpset %X", GAME_ENTRY))
print("LOADERCALLS waiting for GAME.EXE entry")
