-- Count Voodoo accesses (physical 0x08000000-0x08FFFFFF) by offset, read/write, and whether
-- the PC is inside GAME.EXE (0x800C4000-0x801A4224) (hardware-usage.md, "Dynamic check:
-- Voodoo accesses"). Writes voodoo-tap.txt in $CARNEVIL_MAME_OUT (default: the current
-- directory) when the machine stops. The recorded run did not use -nodrc; the PC serves only
-- the game/not-game split here (hwtap.lua is the one with exact attribution).
local OUT = (os.getenv("CARNEVIL_MAME_OUT") or ".") .. "/voodoo-tap.txt"
local cpu = manager.machine.devices[":maincpu"]
local space = cpu.spaces["program"]
local pcstate = cpu.state["PC"]
local GAME_LO, GAME_HI = 0x800C4000, 0x801A4224

local regs = {}      -- key "w|off|game" -> count
local pcs = {}       -- key "w|off" -> {pc=true} (game PCs only, capped)
local npcs = {}
local bulk = {}      -- key "kind|w|game" -> count
local first_game = nil
local taps = {}

local function pc_of()
  local pc = pcstate.value & 0xFFFFFFFF
  return pc
end

local function note(kind, offset, mask)
  local pc = pc_of()
  local game = (pc >= GAME_LO and pc < GAME_HI) and 1 or 0
  if game == 1 and first_game == nil then first_game = manager.machine.time.seconds end
  local off = offset - 0x08000000
  if off < 0x400000 then
    local k = kind .. "|" .. off .. "|" .. game
    regs[k] = (regs[k] or 0) + 1
    if game == 1 then
      local pk = kind .. "|" .. off
      local set = pcs[pk]
      if set == nil then set = {}; pcs[pk] = set; npcs[pk] = 0 end
      if not set[pc] and npcs[pk] < 12 then set[pc] = true; npcs[pk] = npcs[pk] + 1 end
    end
  else
    local region = off < 0x800000 and "lfb" or "tex"
    local k = region .. "|" .. kind .. "|" .. game
    bulk[k] = (bulk[k] or 0) + 1
    if game == 1 then
      local pk = region .. "|" .. kind
      local set = pcs[pk]
      if set == nil then set = {}; pcs[pk] = set; npcs[pk] = 0 end
      if not set[pc] and npcs[pk] < 24 then set[pc] = true; npcs[pk] = npcs[pk] + 1 end
    end
  end
end

local function install()
  for _, t in ipairs(taps) do pcall(function() t:remove() end) end
  taps = {}
  taps[1] = space:install_write_tap(0x08000000, 0x08FFFFFF, "vwtap",
    function(offset, data, mask) note("w", offset, mask) end)
  taps[2] = space:install_read_tap(0x08000000, 0x08FFFFFF, "vrtap",
    function(offset, data, mask) note("r", offset, mask) end)
end

local reinstalls = 0
local frames = 0
frame_sub = emu.add_machine_frame_notifier(function()
  frames = frames + 1
  if frames % 30 == 0 then install(); reinstalls = reinstalls + 1 end
end)

stop_sub = emu.add_machine_stop_notifier(function()
  local f = io.open(OUT, "w")
  f:write(string.format("emulated_seconds %.3f frames %d reinstalls %d first_game_access %s\n",
    manager.machine.time.seconds, frames, reinstalls, tostring(first_game)))
  local keys = {}
  for k in pairs(regs) do keys[#keys + 1] = k end
  table.sort(keys)
  for _, k in ipairs(keys) do f:write("reg " .. k .. " " .. regs[k] .. "\n") end
  keys = {}
  for k in pairs(bulk) do keys[#keys + 1] = k end
  table.sort(keys)
  for _, k in ipairs(keys) do f:write("bulk " .. k .. " " .. bulk[k] .. "\n") end
  keys = {}
  for k in pairs(pcs) do keys[#keys + 1] = k end
  table.sort(keys)
  for _, k in ipairs(keys) do
    local l = {}
    for pc in pairs(pcs[k]) do l[#l + 1] = string.format("%08X", pc) end
    table.sort(l)
    f:write("pcs " .. k .. " " .. table.concat(l, " ") .. "\n")
  end
  f:close()
end)

install()
print("VOODOOTAP armed")
