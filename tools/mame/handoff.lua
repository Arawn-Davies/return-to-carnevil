-- Boot hand-off capture (boot-handoff.md, run 1). Breakpoints log state to the debugger log;
-- 8 MB RAM dumps at the DIAG.EXE and GAME.EXE entries go to $CARNEVIL_MAME_OUT (default: the
-- current directory). The dumps are game data: keep them out of git.
local D = (os.getenv("CARNEVIL_MAME_OUT") or ".") .. "/"
local dbg = manager.machine.debugger
local REGS = 'printf "%s pc=%08X sp=%08X gp=%08X ra=%08X fp=%08X",pc,sp,gp,ra,fp; ' ..
  'printf "%s a0=%08X a1=%08X a2=%08X a3=%08X v0=%08X v1=%08X",a0,a1,a2,a3,v0,v1; ' ..
  'printf "%s t0=%08X t1=%08X t2=%08X t3=%08X t8=%08X t9=%08X k0=%08X k1=%08X",t0,t1,t2,t3,t8,t9,k0,k1; ' ..
  'printf "%s s0=%08X s1=%08X s2=%08X s3=%08X s4=%08X s5=%08X s6=%08X s7=%08X",s0,s1,s2,s3,s4,s5,s6,s7; ' ..
  'printf "%s sr=%08X cause=%08X epc=%08X ccr31=%08X count=%08X compare=%08X",SR,Cause,EPC,CCR31,Count,Compare; '

local function regs(tag)
  return (REGS:gsub("%%s", tag))
end

-- segment B -> boot ROM entry +8 (segment A loader) and the RAM entry
dbg:command('bpset bfc00008,1,{printf "HIT segA-entry ra=%08X",ra; go}')
dbg:command('bpset 80004000,1,{printf "HIT ram-entry-80004000 word=%08X",d@80004000; go}')
-- segment A loader: checksum compare and launch
dbg:command('bpset 8000ebb8,1,{printf "HIT cksum-compare header=%08X computed=%08X",v1,v0; go}')
dbg:command('bpset 8000ebe0,1,{printf "HIT launch s1=%08X",s1; ' .. regs("LAUNCH") .. 'go}')
-- DIAG.EXE entry
dbg:command('bpset 80100000,1,{' .. regs("DIAG") .. 'save ' .. D .. 'ram-at-diag-entry.bin,80000000,800000; go}')
-- GAME.EXE entry
dbg:command('bpset 800c4000,1,{' .. regs("GAME") ..
  'save ' .. D .. 'ram-at-game-entry.bin,80000000,800000; ' ..
  'printf "HIT game-entry dumped"; quit}')
dbg:command('bplist')
print("HANDOFF script armed")
