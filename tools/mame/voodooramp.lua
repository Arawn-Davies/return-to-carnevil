-- Every Voodoo access GAME.EXE's own code makes from its entry until $CARNEVIL_RAMP_SECONDS
-- (default 2.0) emulated seconds after it: the start-up frame-buffer check whose read-back
-- the runtime's Voodoo sink must reproduce (docs/analysis/hardware-usage.md, "Voodoo frame
-- buffer reads"), with the register writes that set it up and draw it.
--
-- Boots to GAME.EXE's entry (0x800C4000), then arms a write and a read watchpoint on physical
-- 0x08000000 to 0x08FFFFFF (Voodoo registers, frame buffer, texture memory) for pcs inside
-- GAME.EXE, and quits at the end. Every event is one line in debug.log, written by the
-- watchpoints' printf actions, so reads and writes are in execution order:
--   VR W pc=.. a=.. d=.. f=.. c=..   a write: next pc (-nodrc), address, value
--   VR R pc=.. a=.. d=.. f=.. c=..   a read: the same, the value read
--   VR ENTRY-T / VR END              the arming at the entry and the end
-- f = the screen's frame number, c = the CPU's total cycles (150 MHz: time_ns = c * 20 / 3).
-- A write the GT64010's DMA makes is not a CPU access and does not appear.
--
-- Run with -nodrc -debug -debugger none -debuglog (watchpoints need -nodrc;
-- tools/mame/README.md).
local SECONDS = tonumber(os.getenv("CARNEVIL_RAMP_SECONDS") or "2.0")
local GAME_ENTRY = 0x800C4000
local IN_GAME = "(pc & 0xffffffff) >= 0x800C4000 && (pc & 0xffffffff) < 0x801A4224"

local machine = manager.machine
local dbg = machine.debugger
local cpu = machine.devices[":maincpu"]
local cdbg = cpu.debug
local prog = cpu.spaces["program"]
local st = {}
for name, entry in pairs(cpu.state) do st[name] = entry end
local function low(entry) return tonumber(tostring(entry):sub(-8), 16) end

local started, done = false, false
local t0, clock0 = 0, os.time()

local function log(line)
  dbg:command('printf "' .. line .. ' f=%d c=%d",frame,totalcycles')
end

local function arm()
  for _, kind in ipairs({{"w", "W"}, {"r", "R"}}) do
    cdbg:wpset(prog, kind[1], 0x08000000, 0x01000000, IN_GAME,
               'printf "VR ' .. kind[2] .. ' pc=%08X a=%08X d=%08X f=%d c=%d",pc,wpaddr,'
               .. 'wpdata,frame,totalcycles; go')
  end
end

local function finish()
  done = true
  log(string.format("VR END t=%.6f", machine.time:as_double()))
  print(string.format("VOODOORAMP done, emulated %.3f s, wall %d s", machine.time:as_double(),
                      os.time() - clock0))
  machine:exit()
end

VOODOORAMP_SUB = emu.register_periodic(function()
  if done then return end
  local ok, err = pcall(function()
    if dbg.execution_state == "stop" then
      if not started and low(st["PC"]) == GAME_ENTRY then
        started = true
        t0 = machine.time:as_double()
        dbg:command("bpclear")
        arm()
        log(string.format("VR ENTRY-T t=%.6f", t0))
        print(string.format("VOODOORAMP armed at GAME.EXE entry, emulated %.6f s", t0))
      end
      dbg.execution_state = "run"
    end
    if started and machine.time:as_double() - t0 >= SECONDS then finish() end
  end)
  if not ok then
    print("VOODOORAMP script error: " .. tostring(err))
    done = true
    machine:exit()
  end
end)
dbg:command(string.format("bpset %X", GAME_ENTRY))
print("VOODOORAMP waiting for GAME.EXE entry")
