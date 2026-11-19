-- Let GAME.EXE run (boot-handoff.md, run 3); log the handler registrations made through the
-- loader, and the first hit of each probable callback, unresolved jump and registered
-- handler of docs/analysis/functions.md. Everything goes to the debugger log.
local dbg = manager.machine.debugger
local n = 0
local function bp(addr, action)
  n = n + 1
  dbg:command(string.format('bpset %x,1,{%s}', addr, action:gsub("@N", tostring(n))))
end
local function always(addr, tag)
  bp(addr, string.format('printf "REG %s a0=%%08X a1=%%08X a2=%%08X ra=%%08X",a0,a1,a2,ra; go', tag))
end
local function once(addr, tag)
  bp(addr, string.format('printf "ONCE %s %08X ra=%%08X sp=%%08X",ra,sp; bpdisable @N; go', tag, addr))
end

bp(0x800C4000, 'printf "GAME entry"; go')
always(0x800060BC, "gt64010-int(bit,handler)")
always(0x8000619C, "fpu-exc(cause,handler)")
always(0x80006200, "break7-hook(handler)")
always(0x80006210, "break6-hook(handler)")
always(0x80006220, "cpu-int(line,handler)")
always(0x80004730, "break409-exc-handler(code,handler)")
always(0x8000E930, "load-exe(name,unused,args)")
always(0x80005048, "reset-handlers")

local callbacks = {0x800C6030, 0x800C6F28, 0x800FE924, 0x800FEDB0, 0x80101CD8, 0x80102454,
                   0x801150C8, 0x801478C0, 0x8014A930, 0x80151A70}
for _, a in ipairs(callbacks) do once(a, "callback") end
local unresolved = {0x8012DCA8, 0x80150174, 0x80164484, 0x801644EC, 0x80164630, 0x80164698,
                    0x801647D0, 0x80164830, 0x80164938, 0x80164998, 0x80164EB4, 0x80164F1C,
                    0x80165040, 0x801650A8, 0x80165290, 0x801652F8}
for _, a in ipairs(unresolved) do once(a, "unresolved-jr") end
local handlers = {0x800C611C, 0x8014FDF0, 0x800C4CEC, 0x800C4D10, 0x800C4D34, 0x80150860,
                  0x8014FEF0, 0x800C4DA0, 0x800C4E68, 0x8013F678, 0x800C47E0}
for _, a in ipairs(handlers) do once(a, "handler") end
print("GAMERUN armed " .. n .. " breakpoints")
