-- The game's cooperative task switch (docs/analysis/task-switch.md).
--
-- Boots to GAME.EXE's entry (0x800C4000), arms the breakpoints and watchpoints below, runs
-- for $CARNEVIL_TASKSW_SECONDS emulated seconds after the entry (default 150) and quits.
-- Every event is one "TS ..." line in debug.log, written by the breakpoint's own printf
-- action (so in execution order); tools/mame/tasksw-report.py turns the log into the
-- numbers. Fields: f = the screen's frame number, c = the CPU's total cycles, the rest
-- hexadecimal (registers sign-extended to 64 bits; the report keeps the low 32 bits).
--
--   TS Y   0x80151AC0 the yield: a0 (sleep argument), cur (word at 0x801A3118), sp, ra, k1
--   TS X   0x80151B50 the exit: cur, sp, k1, ra
--   TS R   0x80151D4C the scheduler's resume: cur, sp (the scheduler's), the task's saved
--          sp (cur + 8) and the ra the restore path will load (saved sp - 0x28)
--   TS PW  0x80151C04 a pass, before its wait: the vblank count 0x801A2FE8, the target
--          0x801A36BC, the timer flag 0x801A36C4
--   TS PD  0x80151C84 the wait is over (the same three words)
--   TS PE  0x80151F9C the walk of the task list is over
--   TS I3  0x80151CAC ioctl(5, 0x7403, 0); TS I7 0x80151FF8 ioctl(5, 0x7407, 0x801A36C0)
--   TS CA  0x80148FA4, TS CB 0x80149200 task creation: a0 (name), a1 (id), a2 (argument
--          words), a3 (function), the fifth argument (sp + 0x10: stack size), ra, sp, cur,
--          and wra, the word at sp + 0x2C: the return address the wrappers 0x8015201C and
--          0x80152098 (the only callers) saved, so the creating call site
--   TS CR  the creation's return (0x801491F8, 0x80149474): the record (v0); for a record,
--          its stack base (+0x10), initial saved sp (+8), function (+0x24), id (+0x18), the
--          name (+0x1C, 8 bytes) and the 4 argument words copied to the stack base
--   TS KF  0x8014947C free a record (Lua): a0, ra, cur; then TS KS, its stack (below)
--   TS KC  0x80149C58 kill a task: a0, a1, ra, cur; TS KM 0x80149D20 kill by id: a0, a1
--   TS EV  0x80000180 general exception: Cause, EPC, sp, k1, cur
--   TS ER  0x80004D64 the dispatcher's eret: EPC, sp, k1; TS EM the monitor's erets
--          (0x80004650, 0x80004804); TS E0 the other vectors
--   TS W   write watchpoint on 0x801A3110-0x801A311B (list head, free list, current task):
--          pc, address, data
--   TS V   write watchpoint on 0x801A2FE8 (the vblank count): pc, data
--   TS TICK  every emulated second after the entry, from Lua: emulated seconds since
--          power-on (f and c are those of the moment the line is written)
-- Lines written from Lua (CR, KF, KS, LS, TICK, IN, SNAP, ENTRY, END) end with f= and c=.
--
-- Stacks: at each creation's return the script copies the new task's stack (base to base +
-- size + 0x800, the block 0x80148FA4 allocates) and, when the record is freed (TS KF) and at
-- the end of the run for the tasks still alive, compares it with the copy (TS KS / TS LS:
-- base, top, size, the number of changed words, the lowest changed address, the lowest one
-- above the 4 argument words at the base, and a map of 256-byte blocks). Not invasive: a
-- write of the value already there is not seen, so the depth is a lower bound.
--
-- $CARNEVIL_PLAY = 1 adds scripted inputs (MAME's :SYSTEM "Coin 1", "1 Player Start" and
-- :FAKE "P1 Trigger" fields, set and released from Lua at fixed emulated times after the
-- entry, listed in PLAY below) and takes snapshots (snap/ in the current folder). Each
-- input and snapshot is logged as "TS IN" / "TS SNAP".
--
-- Run with -nodrc -debug -debugger none -debuglog (watchpoints need -nodrc;
-- tools/mame/README.md).
local SECONDS = tonumber(os.getenv("CARNEVIL_TASKSW_SECONDS") or "150")
local PLAY = os.getenv("CARNEVIL_PLAY") == "1"
local GAME_ENTRY = 0x800C4000

local machine = manager.machine
local dbg = machine.debugger
local cpu = machine.devices[":maincpu"]
local prog = cpu.spaces["program"]
local st = {}
for name, entry in pairs(cpu.state) do st[name] = entry end
local function low(entry) return tonumber(tostring(entry):sub(-8), 16) end
local function rd32(va) return prog:read_u32(va & 0x1FFFFFFF) end
local function rd8(va) return prog:read_u8(va & 0x1FFFFFFF) end

local started, done = false, false
local t0, clock0, next_tick = 0, os.time(), 0
local CREATE_RET = {[0x801491F8] = true, [0x80149474] = true}
local FREE = 0x8014947C
local stacks = {}   -- record -> {base, top, words}

-- a line in debug.log, in order with the breakpoints' lines, with the frame and cycles
local function log(line)
  dbg:command('printf "' .. line .. ' f=%d c=%d",frame,totalcycles')
end

local function bp(addr, fmt, args)
  dbg:command(string.format('bpset %X,1,{printf "%s"%s; go}', addr, fmt,
                            args and ("," .. args) or ""))
end

local function snapshot_stack(rec)
  local base, top0 = rd32(rec + 0x10), rd32(rec + 8)
  local size = top0 - base + 8          -- base + size + 0x7F8 is the initial sp
  local words = {}
  for a = base, base + size - 4, 4 do words[#words + 1] = rd32(a) end
  stacks[rec] = {base = base, top = top0, size = size, words = words}
  return base, top0
end

-- the words that changed since the copy: the lowest changed address, the lowest above the
-- 4 argument words at the base, how many words changed, and a map of the stack in 256-byte
-- blocks from the base up ("x" a block with a change, "." one without)
local function diff_stack(rec, tag)
  local s = stacks[rec]
  if not s then return end
  local lowest, lowest_above, n, map = 0, 0, 0, {}
  for i, old in ipairs(s.words) do
    local a = s.base + (i - 1) * 4
    local block = (a - s.base) // 256 + 1
    map[block] = map[block] or "."
    if rd32(a) ~= old then
      n = n + 1
      map[block] = "x"
      if lowest == 0 then lowest = a end
      if lowest_above == 0 and a >= s.base + 16 then lowest_above = a end
    end
  end
  log(string.format("TS %s rec=%08X base=%08X top=%08X size=%X changed=%d lowest=%08X "
                    .. "lowest16=%08X map=%s", tag, rec, s.base, s.top, s.size, n, lowest,
                    lowest_above, table.concat(map)))
  stacks[rec] = nil
end

local function arm()
  local fc = "f=%d c=%d"
  local fa = "frame,totalcycles"
  bp(0x80151AC0, "TS Y " .. fc .. " a0=%08X cur=%08X sp=%08X ra=%08X k1=%08X",
     fa .. ",a0,d@801A3118,sp,ra,k1")
  bp(0x80151B50, "TS X " .. fc .. " cur=%08X sp=%08X k1=%08X ra=%08X",
     fa .. ",d@801A3118,sp,k1,ra")
  bp(0x80151D4C, "TS R " .. fc .. " cur=%08X sp=%08X tsp=%08X tra=%08X",
     fa .. ",d@801A3118,sp,d@(d@801A3118+8),d@(d@(d@801A3118+8)-28)")
  bp(0x80151C04, "TS PW " .. fc .. " v=%X tgt=%X tf=%X", fa .. ",d@801A2FE8,d@801A36BC,d@801A36C4")
  bp(0x80151C84, "TS PD " .. fc .. " v=%X tgt=%X tf=%X", fa .. ",d@801A2FE8,d@801A36BC,d@801A36C4")
  bp(0x80151F9C, "TS PE " .. fc, fa)
  bp(0x80151CAC, "TS I3 " .. fc, fa)
  bp(0x80151FF8, "TS I7 " .. fc .. " a2=%08X", fa .. ",a2")
  for tag, addr in pairs({CA = 0x80148FA4, CB = 0x80149200}) do
    bp(addr, "TS " .. tag .. " " .. fc .. " a0=%08X a1=%08X a2=%08X a3=%08X a4=%08X ra=%08X sp=%08X cur=%08X wra=%08X",
       fa .. ",a0,a1,a2,a3,d@(sp+10),ra,sp,d@801A3118,d@(sp+2C)")
  end
  for addr in pairs(CREATE_RET) do dbg:command(string.format("bpset %X", addr)) end
  dbg:command(string.format("bpset %X", FREE))
  bp(0x80149C58, "TS KC " .. fc .. " a0=%08X a1=%08X ra=%08X cur=%08X", fa .. ",a0,a1,ra,d@801A3118")
  bp(0x80149D20, "TS KM " .. fc .. " a0=%08X a1=%08X ra=%08X cur=%08X", fa .. ",a0,a1,ra,d@801A3118")
  bp(0x80000180, "TS EV " .. fc .. " cause=%08X epc=%08X sp=%08X k1=%08X cur=%08X",
     fa .. ",cause,epc,sp,k1,d@801A3118")
  bp(0x80004D64, "TS ER " .. fc .. " epc=%08X sp=%08X k1=%08X", fa .. ",epc,sp,k1")
  for _, a in ipairs({0x80004650, 0x80004804}) do
    bp(a, "TS EM " .. fc .. " pc=%08X epc=%08X sp=%08X k1=%08X", fa .. ",pc,epc,sp,k1")
  end
  for _, a in ipairs({0x80000000, 0x80000080, 0x80000100}) do
    bp(a, "TS E0 " .. fc .. " pc=%08X cause=%08X epc=%08X", fa .. ",pc,cause,epc")
  end
  -- watchpoints take physical addresses; pc is sign-extended to 64 bits
  dbg:command('wpset 1A3110,C,w,1,{printf "TS W f=%d c=%d pc=%08X a=%08X d=%08X",'
              .. 'frame,totalcycles,pc,wpaddr,wpdata; go}')
  dbg:command('wpset 1A2FE8,4,w,1,{printf "TS V f=%d c=%d pc=%08X d=%08X",'
              .. 'frame,totalcycles,pc,wpdata; go}')
end

-- scripted play: {seconds after the entry, port, field, value} or a snapshot
local PLAY_STEPS = {
  {20.0, "snap"},
  -- the attract screen shows "CREDITS: 0 OF 2": four coins
  {24.0, ":SYSTEM", "Coin 1", 1}, {24.3, ":SYSTEM", "Coin 1", 0},
  {25.0, ":SYSTEM", "Coin 1", 1}, {25.3, ":SYSTEM", "Coin 1", 0},
  {26.0, ":SYSTEM", "Coin 1", 1}, {26.3, ":SYSTEM", "Coin 1", 0},
  {27.0, ":SYSTEM", "Coin 1", 1}, {27.3, ":SYSTEM", "Coin 1", 0},
  {28.0, "snap"},
  {29.0, ":SYSTEM", "1 Player Start", 1}, {29.3, ":SYSTEM", "1 Player Start", 0},
  {31.0, ":FAKE", "P1 Trigger", 1}, {31.2, ":FAKE", "P1 Trigger", 0},
  {33.0, ":SYSTEM", "1 Player Start", 1}, {33.3, ":SYSTEM", "1 Player Start", 0},
  {36.0, "snap"},
}
for k = 0, 400 do   -- the trigger every second from 38 s on, snapshots every 10 s
  local t = 38 + k
  PLAY_STEPS[#PLAY_STEPS + 1] = {t, ":FAKE", "P1 Trigger", 1}
  PLAY_STEPS[#PLAY_STEPS + 1] = {t + 0.2, ":FAKE", "P1 Trigger", 0}
  if k % 10 == 2 then PLAY_STEPS[#PLAY_STEPS + 1] = {t + 0.5, "snap"} end
end
-- from 75 s on (without aiming, the first life ended at about 78 s): "1 Player Start" every
-- 3 s, to take the "Continue?" offers, and two coins every 10 s to keep credits
for t = 75, 400, 3 do
  PLAY_STEPS[#PLAY_STEPS + 1] = {t + 0.6, ":SYSTEM", "1 Player Start", 1}
  PLAY_STEPS[#PLAY_STEPS + 1] = {t + 0.9, ":SYSTEM", "1 Player Start", 0}
end
for t = 75, 400, 10 do
  PLAY_STEPS[#PLAY_STEPS + 1] = {t + 0.3, ":SYSTEM", "Coin 1", 1}
  PLAY_STEPS[#PLAY_STEPS + 1] = {t + 0.5, ":SYSTEM", "Coin 1", 0}
  PLAY_STEPS[#PLAY_STEPS + 1] = {t + 1.3, ":SYSTEM", "Coin 1", 1}
  PLAY_STEPS[#PLAY_STEPS + 1] = {t + 1.5, ":SYSTEM", "Coin 1", 0}
end
table.sort(PLAY_STEPS, function(x, y) return x[1] < y[1] end)
local next_step = 1

local function play(t)
  while next_step <= #PLAY_STEPS and PLAY_STEPS[next_step][1] <= t do
    local s = PLAY_STEPS[next_step]
    next_step = next_step + 1
    if s[2] == "snap" then
      machine.video:snapshot()
      log(string.format("TS SNAP t=%.3f", s[1]))
    else
      machine.ioport.ports[s[2]].fields[s[3]]:set_value(s[4])
      log(string.format("TS IN t=%.3f %s %s=%d", s[1], s[2],
                        s[3], s[4]))
    end
  end
end

local function finish()
  done = true
  for rec in pairs(stacks) do diff_stack(rec, "LS") end
  log(string.format("TS END t=%.6f", machine.time:as_double()))
  print(string.format("TASKSW done, emulated %.3f s, wall %d s", machine.time:as_double(),
                      os.time() - clock0))
  machine:exit()
end

local function on_stop()
  local pc = low(st["PC"])
  if not started then
    if pc ~= GAME_ENTRY then dbg.execution_state = "run"; return end
    started = true
    t0 = machine.time:as_double()
    dbg:command(string.format("bpclear"))
    arm()
    log(string.format("TS ENTRY t=%.6f", t0))
    print(string.format("TASKSW armed at GAME.EXE entry, emulated %.6f s", t0))
  elseif CREATE_RET[pc] then
    local rec = low(st["v0"])
    if rec ~= 0 then
      local base, top0 = snapshot_stack(rec)
      local name = {}
      for i = 0, 7 do
        local ch = rd8(rec + 0x1C + i)
        if ch == 0 then break end
        name[#name + 1] = (ch > 0x20 and ch < 0x7F and not string.find('"%;{}\\,', string.char(ch), 1, true)) and string.char(ch) or "."
      end
      log(string.format("TS CR at=%08X rec=%08X base=%08X tsp=%08X func=%08X id=%08X "
                        .. "name=%s args=%08X,%08X,%08X,%08X", pc, rec,
                        base, top0, rd32(rec + 0x24), rd32(rec + 0x18), table.concat(name),
                        rd32(base), rd32(base + 4), rd32(base + 8), rd32(base + 12)))
    else
      log(string.format("TS CR at=%08X rec=00000000", pc))
    end
  elseif pc == FREE then
    local rec = low(st["a0"])
    log(string.format("TS KF a0=%08X ra=%08X cur=%08X", rec,
                      low(st["ra"]), rd32(0x801A3118)))
    diff_stack(rec, "KS")
  end
  dbg.execution_state = "run"
end

TASKSW_SUB = emu.register_periodic(function()
  if done then return end
  local ok, err = pcall(function()
    if dbg.execution_state == "stop" then on_stop() end
    if not started then return end
    local t = machine.time:as_double() - t0
    if PLAY then play(t) end
    if t >= next_tick then
      next_tick = next_tick + 1
      log(string.format("TS TICK t=%.6f", machine.time:as_double()))
    end
    if t >= SECONDS then finish() end
  end)
  if not ok then
    print("TASKSW script error: " .. tostring(err))
    done = true
    machine:exit()
  end
end)
dbg:command(string.format("bpset %X", GAME_ENTRY))
print("TASKSW waiting for GAME.EXE entry" .. (PLAY and " (scripted play)" or ""))
