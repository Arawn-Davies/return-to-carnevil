-- The remaining loader-service facts for M2 (open questions of docs/analysis/loader-services.md).
--
-- Boots to GAME.EXE's entry (0x800C4000), logs the hardware state there, arms the
-- breakpoints and watchpoints below, runs for $CARNEVIL_M2_SECONDS emulated seconds after the
-- entry (default 150) and quits. Every event is one "M2 ..." line in debug.log, written by a
-- breakpoint's or watchpoint's printf action (so in execution order) or from Lua. Fields: f =
-- the screen's frame number, c = the CPU's total cycles (150 MHz, so time_ns = c * 20 / 3),
-- the rest hexadecimal (registers sign-extended to 64 bits; keep the low 32 bits).
--
--   M2 ENTRY  at the entry, debugger reads (no side effects): IOASIC INTSTAT (bus word 7) and
--          INTCTL (8), interrupt enable 0x17300000, interrupt configuration 0x17400000,
--          GT64010 INTR_CAUSE 0x0C000C18, CPU_MASK 0x0C000C1C, TIMER_CONTROL 0x0C000864,
--          TIMER0 to TIMER3 0x0C000850 to 0x0C00085C, Status
--   M2 NV  the CMOS (32 KB, one byte per word at physical 0x16100000) at the entry, from Lua:
--          a histogram of byte values (value:count)
--   M2 IN  0x80006CC8, the loader's ioasic read path after its load: a1 (command - 0x6900,
--          so 0 to 3 for 0x6900 to 0x6903) and v0 (the register; ioctl stores v0 & 0xFFFF)
--   M2 GUN read watchpoint on 0x16800000-0x1680001F (the gun's 8 registers): pc, address,
--          value; M2 ARST read and write watchpoint on 0x17F00000
--   M2 PCI slots 0x0F0, 0x0F8, 0x108, 0x110 (Lua, at the return): slot, a0 to a3, the fifth
--          argument (sp + 0x10), v0, and the word through a2 (0x0F0 after, 0x0F8 before) or
--          through a3 (0x110, the device number)
--   M2 VR  read watchpoint on 0x08000000-0x08FFFFFF (Voodoo registers, frame buffer, texture
--          memory) for code in GAME.EXE, the first $CARNEVIL_M2_VOODOO_SECONDS (15) after the
--          entry: pc (the next pc under -nodrc), address, value
--   M2 GR / M2 GW  the loader's sysctrl ioctl 0x7300 (0x8000FB9C: offset at a2, value v0)
--          and 0x7301 (0x8000FBB4: address v0, value a0)
--   M2 CHAIN  at each 0x7301 of offset 0x830 (Lua; its M2 GW line is written from Lua
--          too): the written value (head), the number of records and where the walk ended
--          (0, a record outside RAM, a record seen before, or $CARNEVIL_M2_CHAIN_MAX records,
--          default 100000); then M2 RECS lines, 8 records each, 4 words per record
--          (addr:byte count,source,destination,next), following next
--   M2 B4  0x8014FDF0, the game's GT64010 cause-bit-4 handler: entry
--   M2 MCB 0x800E7CF8 (movie disk callback), M2 WAIT 0x80149A44: a0, Status, sp, ra
--   M2 WAKE 0x80149B40 (Lua): a0 (the cookie), Status, sp, ra; M2 WAKED at its return: the
--          words of the cookie's record (0x30 bytes) and of 0x801A3110-0x801A3123 that
--          changed (address:old>new)
--   M2 MOV 0x800E7E2C, the movie player's slot-0x158 call: a0, a1, a2, the partition word
--          0x801DE92C, the frame 0x801DE914, sectors per frame 0x801DE938
--   M2 SPIN 0x80151C84, the end of the scheduler's VBLANK wait: the spin counter 0x801A36CC
--   M2 BRK  the break hooks 0x800C4DA0 and 0x800C4E68 (Lua): a0 (Cause), a1 (frame), the
--          resume pc at frame + 0x110 (0x80014438); M2 BRKR at the return, the resume pc
--   M2 CATCH 0x800C47E0, the catch-all: a0 (Cause), a1, the resume pc at 0x80014438, EPC
--   M2 6905 ioctl 0x6905 at the stub slot 0x0C8: a0, a2, ra
--   M2 TICK every emulated second after the entry (Lua): emulated seconds since power-on
-- Lines written from Lua end with f= and c=.
--
-- $CARNEVIL_M2_PLAY = 1: the play run. Only the break hooks, the catch-all, ioctl 0x6905 and
-- the ticks are armed, and the scripted inputs of tasksw.lua's PLAY_STEPS (run B of
-- task-switch.md) are applied at the same emulated times after the entry, each logged as
-- "M2 IN-PLAY" / "M2 SNAP".
--
-- Run with -nodrc -debug -debugger none -debuglog (watchpoints need -nodrc;
-- tools/mame/README.md).
local SECONDS = tonumber(os.getenv("CARNEVIL_M2_SECONDS") or "150")
local VOODOO_SECONDS = tonumber(os.getenv("CARNEVIL_M2_VOODOO_SECONDS") or "15")
local PLAY = os.getenv("CARNEVIL_M2_PLAY") == "1"
local CHAIN_MAX = tonumber(os.getenv("CARNEVIL_M2_CHAIN_MAX") or "100000")
local GAME_ENTRY, STUB = 0x800C4000, 0x800C408C
local PCI_SLOTS = {[0x0F0] = true, [0x0F8] = true, [0x108] = true, [0x110] = true}
local GW_CHAIN = 0x8000FBB4
local WAKE = 0x80149B40
local BREAK_HOOKS = {[0x800C4DA0] = true, [0x800C4E68] = true}
local CATCH = 0x800C47E0
local RESUME_PC = 0x80014438   -- the loader's exception frame 0x80014328 + 0x110

local machine = manager.machine
local dbg = machine.debugger
local cpu = machine.devices[":maincpu"]
local cdbg = cpu.debug
local prog = cpu.spaces["program"]
local st = {}
for name, entry in pairs(cpu.state) do st[name] = entry end
local function low(entry) return tonumber(tostring(entry):sub(-8), 16) end
local function rd32(va) return prog:read_u32(va & 0x1FFFFFFF) end
local function is_ram(va) return va >= 0x80000000 and va < 0x80800000 end

local started, done = false, false
local t0, clock0, next_tick = 0, os.time(), 0
local voodoo_wp = nil
local pending = {}        -- return breakpoint index -> {kind, ...}
local special = {}        -- breakpoint address -> handler for Lua stops

-- a line in debug.log, in order with the breakpoints' lines, with the frame and cycles
local function log(line)
  dbg:command('printf "' .. line .. ' f=%d c=%d",frame,totalcycles')
end

local function bp(addr, cond, fmt, args)
  dbg:command(string.format('bpset %X,%s,{printf "%s f=%%d c=%%d"%s,frame,totalcycles; go}',
                            addr, cond, fmt, args and ("," .. args) or ""))
end

local function at_return(e)
  local idx = cdbg:bpset(low(st["ra"]), string.format("(sp & 0xffffffff) == 0x%X", low(st["sp"])), "")
  e.ra, e.sp = low(st["ra"]), low(st["sp"])
  pending[idx] = e
end

local function on_pci(slot)
  local a = {low(st["a0"]), low(st["a1"]), low(st["a2"]), low(st["a3"])}
  local e = {kind = "pci", slot = slot, a = a, a4 = rd32(low(st["sp"]) + 0x10)}
  if slot == 0x0F8 and is_ram(a[3]) then e.before = rd32(a[3]) end
  at_return(e)
end

local function on_chain()
  local head = low(st["a0"])
  log(string.format("M2 GW addr=%08X a0=%08X", low(st["v0"]), head))
  local rec, n, words, seen = head, 0, {}, {}
  while rec ~= 0 and n < CHAIN_MAX and not seen[rec] do
    local va = (rec & 0x1FFFFFFF) | 0x80000000
    if not is_ram(va) then break end
    seen[rec] = true
    local w = {rd32(va), rd32(va + 4), rd32(va + 8), rd32(va + 12)}
    words[#words + 1] = string.format("%08X:%08X,%08X,%08X,%08X", rec, w[1], w[2], w[3], w[4])
    rec, n = w[4], n + 1
  end
  -- the end: next 0, a record outside RAM, a loop, or CHAIN_MAX records
  log(string.format("M2 CHAIN head=%08X n=%d end=%08X", head, n, rec))
  for i = 1, #words, 8 do
    log("M2 RECS " .. table.concat(words, " ", i, math.min(i + 7, #words)))
  end
end

local WAKE_WORDS = {}
local function wake_words(cookie)
  local list = {}
  if is_ram(cookie) then
    for i = 0, 0x2C, 4 do list[#list + 1] = cookie + i end
  end
  for a = 0x801A3110, 0x801A3120, 4 do list[#list + 1] = a end
  return list
end

local function on_wake()
  local cookie = low(st["a0"])
  local e = {kind = "wake", cookie = cookie, words = {}}
  for _, a in ipairs(wake_words(cookie)) do e.words[a] = rd32(a) end
  log(string.format("M2 WAKE a0=%08X sr=%08X sp=%08X ra=%08X", cookie, low(st["SR"]),
                    low(st["sp"]), low(st["ra"])))
  at_return(e)
end

local function on_break(pc)
  log(string.format("M2 BRK pc=%08X a0=%08X a1=%08X resume=%08X sr=%08X", pc, low(st["a0"]),
                    low(st["a1"]), rd32(RESUME_PC), low(st["SR"])))
  at_return({kind = "brk", hook = pc})
end

local function on_return(idx)
  local e = pending[idx]
  pending[idx] = nil
  cdbg:bpclear(idx)
  if e.kind == "pci" then
    local thru = "-"
    if e.slot == 0x0F0 and is_ram(e.a[3]) then thru = string.format("%08X", rd32(e.a[3]))
    elseif e.slot == 0x0F8 and e.before then thru = string.format("%08X", e.before)
    elseif e.slot == 0x110 and is_ram(e.a[4]) then thru = string.format("%08X", rd32(e.a[4])) end
    log(string.format("M2 PCI slot=%03X a0=%08X a1=%08X a2=%08X a3=%08X a4=%08X ra=%08X "
                      .. "v0=%08X thru=%s", e.slot, e.a[1], e.a[2], e.a[3], e.a[4], e.a4, e.ra,
                      low(st["v0"]), thru))
  elseif e.kind == "wake" then
    local ch = {}
    for _, a in ipairs(wake_words(e.cookie)) do
      local now = rd32(a)
      if now ~= e.words[a] then ch[#ch + 1] = string.format("%08X:%08X>%08X", a, e.words[a], now) end
    end
    log(string.format("M2 WAKED a0=%08X sr=%08X changed=%d %s", e.cookie, low(st["SR"]), #ch,
                      table.concat(ch, " ")))
  elseif e.kind == "brk" then
    log(string.format("M2 BRKR hook=%08X resume=%08X", e.hook, rd32(RESUME_PC)))
  end
end

local function arm_breaks()
  for addr in pairs(BREAK_HOOKS) do special[addr] = on_break; dbg:command(string.format("bpset %X", addr)) end
  bp(CATCH, "1", "M2 CATCH a0=%08X a1=%08X resume=%08X epc=%08X sr=%08X",
     "a0,a1,d@80014438,epc,sr")
  bp(STUB + 0x0C8, "a1 == 0x6905", "M2 6905 a0=%08X a2=%08X ra=%08X", "a0,a2,ra")
end

local function arm()
  arm_breaks()
  if PLAY then return end
  bp(0x80006CC8, "a1 < 4", "M2 IN a1=%X v0=%08X", "a1,v0")
  dbg:command('wpset 16800000,20,r,1,{printf "M2 GUN pc=%08X a=%08X d=%08X f=%d c=%d",'
              .. 'pc,wpaddr,wpdata,frame,totalcycles; go}')
  dbg:command('wpset 17F00000,4,rw,1,{printf "M2 ARST pc=%08X a=%08X d=%08X f=%d c=%d",'
              .. 'pc,wpaddr,wpdata,frame,totalcycles; go}')
  for slot in pairs(PCI_SLOTS) do
    special[STUB + slot] = function() on_pci(slot) end
    dbg:command(string.format("bpset %X", STUB + slot))
  end
  voodoo_wp = cdbg:wpset(prog, "r", 0x08000000, 0x01000000,
                         "(pc & 0xffffffff) >= 0x800C4000 && (pc & 0xffffffff) < 0x801A4224",
                         'printf "M2 VR pc=%08X a=%08X d=%08X f=%d c=%d",pc,wpaddr,wpdata,'
                         .. 'frame,totalcycles; go')
  bp(0x8000FB9C, "1", "M2 GR off=%08X v0=%08X", "d@a2,v0")
  -- one breakpoint per address acts (MAME stops at the first that hits), so the chain
  -- writes are a separate, Lua-handled breakpoint with the complementary condition
  bp(GW_CHAIN, "(v0 & 0xfff) != 0x830", "M2 GW addr=%08X a0=%08X", "v0,a0")
  special[GW_CHAIN] = on_chain
  dbg:command(string.format("bpset %X,(v0 & 0xfff) == 0x830", GW_CHAIN))
  bp(0x8014FDF0, "1", "M2 B4 sr=%08X", "sr")
  bp(0x800E7CF8, "1", "M2 MCB a0=%08X sr=%08X sp=%08X ra=%08X", "a0,sr,sp,ra")
  bp(0x80149A44, "1", "M2 WAIT a0=%08X sr=%08X sp=%08X ra=%08X", "a0,sr,sp,ra")
  special[WAKE] = on_wake
  dbg:command(string.format("bpset %X", WAKE))
  bp(0x800E7E2C, "1", "M2 MOV a0=%08X a1=%08X a2=%08X part=%08X frame=%08X spf=%08X",
     "a0,a1,a2,d@801DE92C,d@801DE914,d@801DE938")
  bp(0x80151C84, "1", "M2 SPIN n=%08X v=%X tgt=%X", "d@801A36CC,d@801A2FE8,d@801A36BC")
end

local function entry_state()
  dbg:command('printf "M2 ENTRY intstat=%08X intctl=%08X inten=%08X intcfg=%08X '
              .. 'cause=%08X cpumask=%08X tctl=%08X t0=%08X t1=%08X t2=%08X t3=%08X sr=%08X '
              .. 'f=%d c=%d",d@0xB600001C,d@0xB6000020,d@0xB7300000,d@0xB7400000,d@0xAC000C18,'
              .. 'd@0xAC000C1C,d@0xAC000864,d@0xAC000850,d@0xAC000854,d@0xAC000858,d@0xAC00085C,sr,'
              .. 'frame,totalcycles')
  local hist = {}
  for i = 0, 0x7FFF do
    local b = prog:read_u32(0x16100000 + 4 * i) & 0xFF
    hist[b] = (hist[b] or 0) + 1
  end
  local out = {}
  for v = 0, 255 do if hist[v] then out[#out + 1] = string.format("%02X:%d", v, hist[v]) end end
  log("M2 NV " .. table.concat(out, " "))
end

-- scripted play, identical to tasksw.lua's PLAY_STEPS (run B of task-switch.md)
local PLAY_STEPS = {
  {20.0, "snap"},
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
for k = 0, 400 do
  local t = 38 + k
  PLAY_STEPS[#PLAY_STEPS + 1] = {t, ":FAKE", "P1 Trigger", 1}
  PLAY_STEPS[#PLAY_STEPS + 1] = {t + 0.2, ":FAKE", "P1 Trigger", 0}
  if k % 10 == 2 then PLAY_STEPS[#PLAY_STEPS + 1] = {t + 0.5, "snap"} end
end
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
      log(string.format("M2 SNAP t=%.3f", s[1]))
    else
      machine.ioport.ports[s[2]].fields[s[3]]:set_value(s[4])
      log(string.format("M2 IN-PLAY t=%.3f %s %s=%d", s[1], s[2], s[3], s[4]))
    end
  end
end

local function finish()
  done = true
  log(string.format("M2 END t=%.6f", machine.time:as_double()))
  print(string.format("M2FACTS done, emulated %.3f s, wall %d s", machine.time:as_double(),
                      os.time() - clock0))
  machine:exit()
end

local function on_stop()
  local pc = low(st["PC"])
  if not started then
    if pc ~= GAME_ENTRY then dbg.execution_state = "run"; return end
    started = true
    t0 = machine.time:as_double()
    dbg:command("bpclear")
    if not PLAY then entry_state() end
    arm()
    log(string.format("M2 ENTRY-T t=%.6f", t0))
    print(string.format("M2FACTS armed at GAME.EXE entry, emulated %.6f s", t0))
  else
    local hit
    for idx, e in pairs(pending) do
      if e.ra == pc and e.sp == low(st["sp"]) then hit = idx; break end
    end
    if hit then on_return(hit)
    elseif special[pc] then special[pc](pc) end
  end
  dbg.execution_state = "run"
end

M2FACTS_SUB = emu.register_periodic(function()
  if done then return end
  local ok, err = pcall(function()
    if dbg.execution_state == "stop" then on_stop() end
    if not started then return end
    local t = machine.time:as_double() - t0
    if PLAY then play(t) end
    if voodoo_wp and t >= VOODOO_SECONDS then
      cdbg:wpclear(voodoo_wp)
      voodoo_wp = nil
      log(string.format("M2 VR-OFF t=%.6f", machine.time:as_double()))
    end
    if t >= next_tick then
      next_tick = next_tick + 1
      log(string.format("M2 TICK t=%.6f", machine.time:as_double()))
    end
    if t >= SECONDS then finish() end
  end)
  if not ok then
    print("M2FACTS script error: " .. tostring(err))
    done = true
    machine:exit()
  end
end)
dbg:command(string.format("bpset %X", GAME_ENTRY))
print("M2FACTS waiting for GAME.EXE entry" .. (PLAY and " (scripted play)" or ""))
