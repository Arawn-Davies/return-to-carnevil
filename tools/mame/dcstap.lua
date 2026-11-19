-- The game's exchange with the DCS sound board (docs/analysis/dcs-handshake.md).
--
-- Boots to GAME.EXE's entry (0x800C4000), arms the breakpoints below, runs for
-- $CARNEVIL_DCS_SECONDS emulated seconds after the entry (default 150) and quits. Every event
-- is one "DT ..." line in debug.log, written by a breakpoint's printf action (so in execution
-- order). Fields: f = the screen's frame number, c = the CPU's total cycles (150 MHz, so
-- time_ns = c * 20 / 3), the rest hexadecimal (registers sign-extended to 64 bits; keep the
-- low 32 bits).
--
-- The game reaches the DCS only through ioctls on fd 4 (`ioasic:`) and the sound FIFO
-- (loader-services.md, section 5); the loader's ioasic ioctl 0x80006BE0 subtracts 0x6900
-- from the command (a1) and goes through one read path and one write path, where the
-- breakpoints sit:
--
--   DT R  0x80006CC8, just after the read path's load: k = a1 (7 SOUNDCTL 0x6907, 9
--         SOUNDSTAT 0x6909, A SOUNDIN 0x690A, E INTSTAT 0x690E, 10 INTCTL 0x6910) and v0
--         (the register as read; the ioctl stores v0 & 0xFFFF)
--   DT W  0x80006CDC, the write path: k = a1 (6 SOUNDCTL 0x6906, 8 SOUNDOUT 0x6908, 11
--         SOUNDIN 0x6911, F INTCTL 0x690F) and a2 (the argument; the register gets a2 & 0xFFFF)
--   DT E  every return of 0xEEEE (v0 & 0xFFFF) from the game's DCS routines, at their `jr ra`
--         (v0 is final there: every delay slot is a nop): the jr's address, ra, sp
--   DT I3 the general exception vector 0x80000180 when Cause has IP3 (0x800): Cause, EPC,
--         and debugger reads (no side effects) of IOASIC INTSTAT (bus word 7) and INTCTL (8)
--   DT H3 0x8013F678, the game's IP3 (line 3) handler: a0
--   DT T  0x801491F8, the return of the task creation 0x80148FA4: the record (v0) and its
--         function (record + 0x24), to show which tasks start (task-switch.md)
--   DT U  write watchpoint on UARTOUT (physical 0x16000028): pc, the byte (console output)
--   DT TICK every emulated second after the entry, from Lua: emulated seconds since power-on
--
-- $CARNEVIL_DCS_FORCE_CLEAR = 1 clears bit 0x40 of every SOUNDSTAT read (ioctl 0x6909) before
-- the loader stores it (an action that sets v0 at 0x80006CC8), so the game never sees data
-- waiting from the DCS; the logged value is the forced one. $CARNEVIL_DCS_SNAP = 1 takes a
-- snapshot (snap/ in the current folder) every 10 emulated seconds from 20 s after the entry,
-- logged as "DT SNAP".
--
-- Run with -nodrc -debug -debugger none -debuglog (tools/mame/README.md).
local SECONDS = tonumber(os.getenv("CARNEVIL_DCS_SECONDS") or "150")
local FORCE = os.getenv("CARNEVIL_DCS_FORCE_CLEAR") == "1"
local SNAP = os.getenv("CARNEVIL_DCS_SNAP") == "1"
local GAME_ENTRY = 0x800C4000
-- the `jr ra` of each DCS routine that can return 0xEEEE (read), by routine entry
local RETURNS = {
  [0x8016A42C] = 0x8016A508, [0x8016A510] = 0x8016A60C, [0x8016A614] = 0x8016A6E0,
  [0x8016A6E8] = 0x8016A7C4, [0x8016A82C] = 0x8016A928, [0x8016A930] = 0x8016A9E0,
  [0x8016AA68] = 0x8016AAA4,
}

local machine = manager.machine
local dbg = machine.debugger
local cpu = machine.devices[":maincpu"]
local st = {}
for name, entry in pairs(cpu.state) do st[name] = entry end
local function low(entry) return tonumber(tostring(entry):sub(-8), 16) end

local started, done = false, false
local t0, clock0, next_tick = 0, os.time(), 0

local function log(line)
  dbg:command('printf "' .. line .. ' f=%d c=%d",frame,totalcycles')
end

local function bp(addr, cond, fmt, args, pre)
  dbg:command(string.format('bpset %X,%s,{%sprintf "%s f=%%d c=%%d"%s,frame,totalcycles; go}',
                            addr, cond, pre or "", fmt, args and ("," .. args) or ""))
end

local function arm()
  -- one breakpoint per address acts, so the forced SOUNDSTAT read has its own condition
  if FORCE then
    bp(0x80006CC8, "a1 == 9", "DT R k=%X v=%08X", "a1,v0", "v0 = v0 & 0xffffffbf; ")
    bp(0x80006CC8, "a1 == 7 || a1 == 0xA || a1 == 0xE || a1 == 0x10", "DT R k=%X v=%08X", "a1,v0")
  else
    bp(0x80006CC8, "a1 == 7 || a1 == 9 || a1 == 0xA || a1 == 0xE || a1 == 0x10", "DT R k=%X v=%08X",
       "a1,v0")
  end
  bp(0x80006CDC, "a1 == 6 || a1 == 8 || a1 == 0x11 || a1 == 0xF", "DT W k=%X a2=%08X", "a1,a2")
  for _, jr in pairs(RETURNS) do
    bp(jr, "(v0 & 0xffff) == 0xEEEE", "DT E pc=%08X ra=%08X sp=%08X", "pc,ra,sp")
  end
  bp(0x80000180, "(cause & 0x800) != 0", "DT I3 cause=%08X epc=%08X intstat=%08X intctl=%08X",
     "cause,epc,d@0xB600001C,d@0xB6000020")
  bp(0x8013F678, "1", "DT H3 a0=%08X", "a0")
  bp(0x801491F8, "1", "DT T rec=%08X func=%08X", "v0,d@(v0+24)")
end

local function finish()
  done = true
  log(string.format("DT END t=%.6f", machine.time:as_double()))
  print(string.format("DCSTAP done, emulated %.3f s, wall %d s", machine.time:as_double(),
                      os.time() - clock0))
  machine:exit()
end

DCSTAP_SUB = emu.register_periodic(function()
  if done then return end
  local ok, err = pcall(function()
    if dbg.execution_state == "stop" then
      if not started and low(st["PC"]) == GAME_ENTRY then
        started = true
        t0 = machine.time:as_double()
        dbg:command("bpclear")
        arm()
        log(string.format("DT ENTRY t=%.6f force=%d", t0, FORCE and 1 or 0))
        print(string.format("DCSTAP armed at GAME.EXE entry, emulated %.6f s", t0))
      end
      dbg.execution_state = "run"
    end
    if not started then return end
    local t = machine.time:as_double() - t0
    if t >= next_tick then
      if SNAP and next_tick >= 20 and next_tick % 10 == 0 then
        machine.video:snapshot()
        log(string.format("DT SNAP t=%d", next_tick))
      end
      next_tick = next_tick + 1
      log(string.format("DT TICK t=%.6f", machine.time:as_double()))
    end
    if t >= SECONDS then finish() end
  end)
  if not ok then
    print("DCSTAP script error: " .. tostring(err))
    done = true
    machine:exit()
  end
end)
-- the console from power-on: a watchpoint on the physical address of UARTOUT
dbg:command('wpset 16000028,4,w,1,{printf "DT U f=%d c=%d pc=%08X d=%02X",frame,totalcycles,'
            .. 'pc,wpdata; go}')
dbg:command(string.format("bpset %X", GAME_ENTRY))
print("DCSTAP waiting for GAME.EXE entry" .. (FORCE and " (SOUNDSTAT bit 0x40 forced clear)" or ""))
