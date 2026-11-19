-- hwtap.lua with one more tap: loader and other accesses after the hand-off (the first
-- non-zero write to the stub block, below) are tagged @after-game-start. Counts accesses to
-- PCI I/O + GT64010 (0x0A000000-0x0C000FFF) and the chip-select space (0x10000000-0x17FFFFFF)
-- by address, read/write and who made them (by PC): game (GAME.EXE image
-- 0x800C4000-0x801A4224, stub block excluded), stub ([0x800C408C, 0x800C42CC)), loader
-- (0x80000000-0x80015C40), other (hardware-usage.md, "Split run"). Writes hw-tap-split.txt in
-- $CARNEVIL_MAME_OUT (default: the current directory) when the machine stops. Run with
-- -nodrc: only the interpreter keeps CURPC exact.
local OUT = (os.getenv("CARNEVIL_MAME_OUT") or ".") .. "/hw-tap-split.txt"
local cpu = manager.machine.devices[":maincpu"]
local space = cpu.spaces["program"]
-- Under -nodrc, CURPC is the next PC to run, not the accessing instruction: the instruction
-- after the access, or for an access in a branch delay slot the branch target. An access in
-- the delay slot of a transfer between groups is therefore counted in the target's group.
local pcstate = cpu.state["CURPC"] or cpu.state["PC"]
print("HWTAP pc register: " .. (cpu.state["CURPC"] and "CURPC" or "PC"))

local counts = {}   -- "who|kind|addr" -> n
local pcs = {}      -- "kind|addr" -> set of game PCs (capped)
local npcs = {}
local first = {}    -- who -> emulated time of first access
local taps = {}
-- GAME.EXE's _start copies the loader's slot table to physical 0x000C408C (stub block):
-- the first non-zero write there (a `j`) marks the hand-off; the RAM clear and the image
-- load write zeros.
local started = nil
local function start_cb(offset, data, mask)
  if started == nil and data ~= 0 then started = manager.machine.time.seconds end
end

local function who_of(pc)
  if pc >= 0x800C408C and pc < 0x800C42CC then return "stub" end
  if pc >= 0x800C4000 and pc < 0x801A4224 then return "game" end
  if pc >= 0x80000000 and pc < 0x80015C40 then return "loader" end
  return "other"
end

local function note(kind, offset)
  local pc = pcstate.value & 0xFFFFFFFF
  local who = who_of(pc)
  if who ~= "game" and who ~= "stub" and started ~= nil then who = who .. "@after-game-start" end
  if first[who] == nil then first[who] = manager.machine.time.seconds end
  local k = who .. "|" .. kind .. "|" .. string.format("%08X", offset)
  counts[k] = (counts[k] or 0) + 1
  if who == "game" or who == "stub" then
    local pk = kind .. "|" .. string.format("%08X", offset)
    local set = pcs[pk]
    if set == nil then set = {}; pcs[pk] = set; npcs[pk] = 0 end
    if not set[pc] and npcs[pk] < 16 then set[pc] = true; npcs[pk] = npcs[pk] + 1 end
  end
end

local RANGES = {{0x0A000000, 0x0C000FFF}, {0x10000000, 0x17FFFFFF}}

local function install()
  for _, t in ipairs(taps) do pcall(function() t:remove() end) end
  taps = {}
  taps[1] = space:install_write_tap(0x000C408C, 0x000C408F, "starttap", start_cb)
  for i, r in ipairs(RANGES) do
    taps[#taps + 1] = space:install_write_tap(r[1], r[2], "hwwtap" .. i,
      function(offset, data, mask) note("w", offset) end)
    taps[#taps + 1] = space:install_read_tap(r[1], r[2], "hwrtap" .. i,
      function(offset, data, mask) note("r", offset) end)
  end
end

local frames, reinstalls = 0, 0
frame_sub = emu.add_machine_frame_notifier(function()
  frames = frames + 1
  if frames % 30 == 0 then install(); reinstalls = reinstalls + 1 end
end)

stop_sub = emu.add_machine_stop_notifier(function()
  local f = io.open(OUT, "w")
  f:write(string.format("emulated_seconds %.3f frames %d reinstalls %d game_start %s\n",
    manager.machine.time.seconds, frames, reinstalls, tostring(started)))
  for w, t in pairs(first) do f:write(string.format("first %s %.3f\n", w, t)) end
  local keys = {}
  for k in pairs(counts) do keys[#keys + 1] = k end
  table.sort(keys)
  for _, k in ipairs(keys) do f:write("count " .. k .. " " .. counts[k] .. "\n") end
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
print("HWTAP armed")
