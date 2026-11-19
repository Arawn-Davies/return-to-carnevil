-- Boot with the Test Switch on (the DIAG.EXE path, boot-handoff.md run 2), then release it after
-- 60 emulated seconds. 8 MB RAM dumps at the DIAG.EXE entry and at the GAME.EXE entry reached
-- through DIAG.EXE go to $CARNEVIL_MAME_OUT (default: the current directory). The dumps are
-- game data: keep them out of git.
local D = (os.getenv("CARNEVIL_MAME_OUT") or ".") .. "/"
local dbg = manager.machine.debugger
local field = manager.machine.ioport.ports[":DIPS"].fields["Test Switch"]
field.user_value = 0
print("DIAGPATH test switch user_value=" .. tostring(field.user_value))

local REGS = 'printf "%s pc=%08X sp=%08X gp=%08X ra=%08X a0=%08X a1=%08X a2=%08X a3=%08X",pc,sp,gp,ra,a0,a1,a2,a3; ' ..
  'printf "%s sr=%08X ccr31=%08X",SR,CCR31; '
local function regs(tag) return (REGS:gsub("%%s", tag)) end

dbg:command('bpset 8000e930,1,{printf "HIT load-exe a0=%08X a1=%08X a2=%08X args=%08X %08X %08X %08X ra=%08X",a0,a1,a2,d@a2,d@(a2+4),d@(a2+8),d@(a2+c),ra; go}')
dbg:command('bpset 8000ebe0,1,{printf "HIT launch s1=%08X",s1; go}')
dbg:command('bpset 80100000,1,{' .. regs("DIAG") .. 'save ' .. D .. 'ram-at-diag-entry.bin,80000000,800000; go}')
dbg:command('bpset 800c4000,1,{' .. regs("GAME") .. 'save ' .. D .. 'ram-at-game-entry-via-diag.bin,80000000,800000; printf "HIT game-entry-via-diag"; quit}')

local flipped = false
local periodic = emu.add_machine_periodic_notifier or emu.register_periodic
DIAGPATH_SUB = periodic(function()
  if not flipped and manager.machine.time.seconds > 60 then
    field.user_value = field.defvalue
    flipped = true
    print("DIAGPATH test switch released at " .. manager.machine.time.seconds)
  end
end)
print("DIAGPATH armed")
