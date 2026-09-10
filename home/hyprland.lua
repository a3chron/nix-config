-- Hyprland Lua config.
-- Refer to the wiki for more information.
-- https://wiki.hypr.land/Configuring/Start/
--
-- Ported from the old hyprland.conf on 2026-09-03. Hyprland 0.56 dropped the
-- legacy hyprland.conf format entirely (it is Lua-only), and 0.55 already
-- prefers hyprland.lua whenever one exists -- so this file is now the config on
-- both versions. Verified with `Hyprland --verify-config -c` against the 0.55
-- and 0.56 binaries.


------------------
---- MONITORS ----
------------------

-- See https://wiki.hypr.land/Configuring/Basics/Monitors/
hl.monitor({
    output   = "",
    mode     = "preferred",
    position = "auto",
    scale    = "auto",
})


---------------------
---- MY PROGRAMS ----
---------------------

local terminal    = "ghostty"
local fileManager = "nautilus"
-- local menu     = "wofi --show drun"   -- see the SUPER+R note under KEYBINDINGS


-------------------
---- AUTOSTART ----
-------------------

-- See https://wiki.hypr.land/Configuring/Basics/Autostart/
--
-- ambxst is NOT exec'd here any more. Since 2026-09-03 it runs as the
-- `ambxst.service` systemd user unit (home/a3chron.nix), pulled in by
-- graphical-session.target, so it gets restarted when it dies. It used to die
-- on monitor power-off: the Fujitsu drops the HDMI link, Hyprland removes the
-- output, and the resulting Wayland protocol error is fatal for quickshell --
-- leaving the bare grey "hypr just better" screen with no bar and no launcher.
-- (The ambxst-generated startup hook is filtered out in the AMBXST section.)
hl.on("hyprland.start", function()
end)


-------------------------------
---- ENVIRONMENT VARIABLES ----
-------------------------------

-- See https://wiki.hypr.land/Configuring/Advanced-and-Cool/Environment-variables/
hl.env("XCURSOR_SIZE", "24")
hl.env("HYPRCURSOR_SIZE", "24")
hl.env("XDG_CURRENT_DESKTOP", "Hyprland")
hl.env("XDG_SESSION_TYPE", "wayland")
hl.env("XDG_SESSION_DESKTOP", "Hyprland")


-----------------------
---- LOOK AND FEEL ----
-----------------------

-- Refer to https://wiki.hypr.land/Configuring/Basics/Variables/
hl.config({
    general = {
        gaps_in  = 5,
        gaps_out = 20,

        border_size = 2,

        col = {
            active_border   = { colors = { "rgba(89b4faee)", "rgba(74c7ecee)" }, angle = 45 },
            inactive_border = "rgba(a6adc8aa)",
        },

        -- Set to true to enable resizing windows by clicking and dragging on borders and gaps
        resize_on_border = false,

        -- Please see https://wiki.hypr.land/Configuring/Advanced-and-Cool/Tearing/ before you turn this on
        allow_tearing = false,

        layout = "dwindle",
    },

    decoration = {
        rounding       = 10,
        rounding_power = 2,

        -- Change transparency of focused and unfocused windows
        active_opacity   = 1.0,
        inactive_opacity = 1.0,

        shadow = {
            enabled      = true,
            range        = 4,
            render_power = 3,
            color        = "rgba(1a1a1aee)",
        },

        blur = {
            enabled  = true,
            size     = 3,
            passes   = 1,
            vibrancy = 0.1696,
        },
    },

    animations = {
        enabled = true,
    },
})

-- Default curves, see https://wiki.hypr.land/Configuring/Advanced-and-Cool/Animations/
hl.curve("easeOutQuint",   { type = "bezier", points = { {0.23, 1},    {0.32, 1} } })
hl.curve("easeInOutCubic", { type = "bezier", points = { {0.65, 0.05}, {0.36, 1} } })
hl.curve("linear",         { type = "bezier", points = { {0, 0},       {1, 1}    } })
hl.curve("almostLinear",   { type = "bezier", points = { {0.5, 0.5},   {0.75, 1} } })
hl.curve("quick",          { type = "bezier", points = { {0.15, 0},    {0.1, 1}  } })

hl.animation({ leaf = "global",        enabled = true, speed = 10,   bezier = "default" })
hl.animation({ leaf = "border",        enabled = true, speed = 5.39, bezier = "easeOutQuint" })
hl.animation({ leaf = "windows",       enabled = true, speed = 4.79, bezier = "easeOutQuint" })
hl.animation({ leaf = "windowsIn",     enabled = true, speed = 4.1,  bezier = "easeOutQuint", style = "popin 87%" })
hl.animation({ leaf = "windowsOut",    enabled = true, speed = 1.49, bezier = "linear",       style = "popin 87%" })
hl.animation({ leaf = "fadeIn",        enabled = true, speed = 1.73, bezier = "almostLinear" })
hl.animation({ leaf = "fadeOut",       enabled = true, speed = 1.46, bezier = "almostLinear" })
hl.animation({ leaf = "fade",          enabled = true, speed = 3.03, bezier = "quick" })
hl.animation({ leaf = "layers",        enabled = true, speed = 3.81, bezier = "easeOutQuint" })
hl.animation({ leaf = "layersIn",      enabled = true, speed = 4,    bezier = "easeOutQuint", style = "fade" })
hl.animation({ leaf = "layersOut",     enabled = true, speed = 1.5,  bezier = "linear",       style = "fade" })
hl.animation({ leaf = "fadeLayersIn",  enabled = true, speed = 1.79, bezier = "almostLinear" })
hl.animation({ leaf = "fadeLayersOut", enabled = true, speed = 1.39, bezier = "almostLinear" })
hl.animation({ leaf = "workspaces",    enabled = true, speed = 1.94, bezier = "almostLinear", style = "fade" })
hl.animation({ leaf = "workspacesIn",  enabled = true, speed = 1.21, bezier = "almostLinear", style = "fade" })
hl.animation({ leaf = "workspacesOut", enabled = true, speed = 1.94, bezier = "almostLinear", style = "fade" })
hl.animation({ leaf = "zoomFactor",    enabled = true, speed = 7,    bezier = "quick" })

-- See https://wiki.hypr.land/Configuring/Layouts/Dwindle-Layout/ for more
hl.config({
    dwindle = {
        preserve_split = true, -- You probably want this
    },

    master = {
        new_status = "master",
    },

    misc = {
        force_default_wallpaper = -1,   -- Set to 0 or 1 to disable the anime mascot wallpapers
        disable_hyprland_logo   = true, -- If true disables the random hyprland logo / anime girl background. :(
        -- The ambxst lockscreen lives inside quickshell. When Hyprland kills
        -- quickshell during a monitor power-off (see ambxst.service in
        -- a3chron.nix), the ext-session-lock client is gone; with this off,
        -- Hyprland keeps the session locked behind a dead red screen that no
        -- restarted locker may take over. With it on, the restarted ambxst
        -- can lock again and the session stays usable *and* locked.
        allow_session_lock_restore = true,
    },

    debug = {
        -- Hyprland-level logging (default off leaves only aquamarine's backend
        -- chatter in hyprland.log). Turned on 2026-09-05 to capture what
        -- Hyprland 0.56 does to client resources when HDMI-A-2 drops and
        -- returns -- clients die with "unknown object (N), message attach"
        -- protocol errors on nearly every screen power-off since 0.56.0.
        -- Turn back off once that is reported/fixed upstream.
        disable_logs = false,
    },
})


---------------
---- INPUT ----
---------------

hl.config({
    input = {
        kb_layout  = "de",
        kb_variant = "nodeadkeys",
        kb_model   = "",
        kb_options = "terminate:ctrl_alt_bksp,compose:rctrl",
        kb_rules   = "",

        follow_mouse = 1,

        sensitivity = 0, -- -1.0 - 1.0, 0 means no modification.

        touchpad = {
            natural_scroll = false,
        },
    },
})

-- See https://wiki.hypr.land/Configuring/Gestures
-- Replaces the old `gesture = 3, l, workspace, e-1` / `gesture = 3, r, ...`
-- pair -- one horizontal gesture now covers both directions.
hl.gesture({
    fingers   = 3,
    direction = "horizontal",
    action    = "workspace",
})

-- Example per-device config
-- See https://wiki.hypr.land/Configuring/Advanced-and-Cool/Devices/ for more
hl.device({
    name        = "epic-mouse-v1",
    sensitivity = -0.5,
})


---------------------
---- KEYBINDINGS ----
---------------------

-- See https://wiki.hypr.land/Configuring/Basics/Binds/
local mainMod = "SUPER" -- Sets "Windows" key as main modifier

hl.bind("CTRL + ALT + T",     hl.dsp.exec_cmd(terminal))
hl.bind(mainMod .. " + C",    hl.dsp.window.close())
hl.bind(mainMod .. " + M",    hl.dsp.exit())
hl.bind(mainMod .. " + E",    hl.dsp.exec_cmd(fileManager))
hl.bind(mainMod .. " + V",    hl.dsp.window.float({ action = "toggle" }))

-- SUPER+R used to be `bind = $mainMod, R, exec, $menu`, but $menu was never
-- defined (the `$menu = wofi --show drun` line was commented out), so the bind
-- has been dead for as long as it has existed. Left out rather than silently
-- wired to something new -- uncomment the `menu` local above and this line if
-- you want it back.
-- hl.bind(mainMod .. " + R", hl.dsp.exec_cmd(menu))

hl.bind(mainMod .. " + P",    hl.dsp.window.pseudo())
hl.bind(mainMod .. " + J",    hl.dsp.layout("togglesplit")) -- dwindle

-- Alt+Tab window cycling (built-in, no external tools needed).
-- The old config expressed this as two binds on the same key; Hyprland runs
-- every matching bind, so one Lua callback doing both dispatches is equivalent.
hl.bind("ALT + Tab", function()
    hl.dispatch(hl.dsp.window.cycle_next())
    hl.dispatch(hl.dsp.window.bring_to_top())
end)
-- Backwards is `next = false`, NOT `prev = true`. Dispatcher option tables are
-- not validated at parse time, so a wrong key here is silently ignored and you
-- just get a second forward-cycling bind. Verified live against 0.55 with three
-- windows: {} and {next=true} give 3->2->1->3, {next=false} gives 3->1->2->3.
hl.bind("ALT + SHIFT + Tab", function()
    hl.dispatch(hl.dsp.window.cycle_next({ next = false }))
    hl.dispatch(hl.dsp.window.bring_to_top())
end)

-- Switch workspaces with mainMod + [0-9]
-- Move active window to a workspace with mainMod + SHIFT + [0-9]
for i = 1, 10 do
    local key = i % 10 -- 10 maps to key 0
    hl.bind(mainMod .. " + " .. key,         hl.dsp.focus({ workspace = i }))
    hl.bind(mainMod .. " + SHIFT + " .. key, hl.dsp.window.move({ workspace = i }))
end

-- Example special workspace (scratchpad)
hl.bind(mainMod .. " + S",         hl.dsp.workspace.toggle_special("magic"))
hl.bind(mainMod .. " + SHIFT + S", hl.dsp.window.move({ workspace = "special:magic" }))

-- Scroll through existing workspaces with mainMod + scroll
hl.bind(mainMod .. " + mouse_down", hl.dsp.focus({ workspace = "e+1" }))
hl.bind(mainMod .. " + mouse_up",   hl.dsp.focus({ workspace = "e-1" }))

-- Move/resize windows with mainMod + LMB/RMB and dragging
hl.bind(mainMod .. " + mouse:272", hl.dsp.window.drag(),   { mouse = true })
hl.bind(mainMod .. " + mouse:273", hl.dsp.window.resize(), { mouse = true })

-- Laptop multimedia keys for volume and LCD brightness (old `bindel` = locked + repeating)
hl.bind("XF86AudioRaiseVolume",  hl.dsp.exec_cmd("wpctl set-volume -l 1 @DEFAULT_AUDIO_SINK@ 5%+"), { locked = true, repeating = true })
hl.bind("XF86AudioLowerVolume",  hl.dsp.exec_cmd("wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-"),      { locked = true, repeating = true })
hl.bind("XF86AudioMute",         hl.dsp.exec_cmd("wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle"),     { locked = true, repeating = true })
hl.bind("XF86AudioMicMute",      hl.dsp.exec_cmd("wpctl set-mute @DEFAULT_AUDIO_SOURCE@ toggle"),   { locked = true, repeating = true })
hl.bind("XF86MonBrightnessUp",   hl.dsp.exec_cmd("brightnessctl -e4 -n2 set 5%+"),                  { locked = true, repeating = true })
hl.bind("XF86MonBrightnessDown", hl.dsp.exec_cmd("brightnessctl -e4 -n2 set 5%-"),                  { locked = true, repeating = true })

-- Requires playerctl.
-- XF86AudioPlay / Next / Prev are NOT bound here: ambxst already binds all
-- three in its generated ~/.local/share/ambxst/hyprland.lua (loaded below),
-- and Hyprland 0.55 executes EVERY matching bind -- verified by binding one key
-- twice and counting executions: one press ran the command twice. Two
-- `playerctl play-pause` runs send two MPRIS PlayPause calls, which cancel out.
-- Measured against a real mpv reading on 2026-08-08: with both binds only 1 of
-- 3 presses worked, with one bind 3 of 3. Firefox happens to coalesce the
-- duplicate calls, which is why this stayed invisible until horus-read's mpv
-- joined the bus (see horus/read.nix for the whole media-key chain).
-- XF86AudioPause is kept -- ambxst does NOT bind it.
hl.bind("XF86AudioPause", hl.dsp.exec_cmd("playerctl play-pause"), { locked = true })


----------------
---- AMBXST ----
----------------

-- Replaces `source = ~/.local/share/ambxst/hyprland.conf`. axctl generates a
-- .lua alongside the .conf and they are equivalent except for the resizeactive
-- binds -- see the RESIZE section below.
--
-- Loaded defensively: a missing ambxst file, a syntax error in it, or a runtime
-- error partway through it must not take the whole config down, because a
-- config that fails to load is what drops you into Hyprland's bare fallback
-- session with no bar and no binds. Note this guard only catches Lua-level
-- failures; a bad *config key* inside that file (e.g. general.NOPE) is reported
-- out-of-band by Hyprland and still surfaces as a top-level config error.
-- NB: it is hl.notification.create({ text = ... }), not hl.notification(...).
-- hl.notification is a table (create/get), so calling it directly throws --
-- which would defeat the whole point of this being the error path.
local function warnOnScreen(msg)
    hl.notification.create({ text = msg, time = 10000 })
end

-- The generated file also registers `hl.on("hyprland.start", exec "ambxst")`.
-- That comes from `[startup] exec-once = "ambxst"` in axctl.toml, which
-- ambxst's TOML writer hardcodes and rewrites on every launch, so it cannot be
-- switched off in config. ambxst is started by ambxst.service instead (see
-- AUTOSTART), and a second launch replaces the running instance -- which would
-- race with the service at login. So while running the chunk, hl.on is shadowed
-- to swallow its hyprland.start registration (the only one it makes) and pass
-- everything else through unchanged.
local ambxstConfig = os.getenv("HOME") .. "/.local/share/ambxst/hyprland.lua"
local ambxstChunk, ambxstLoadErr = loadfile(ambxstConfig)
if ambxstChunk then
    local realOn = hl.on
    hl.on = function(event, ...)
        if event == "hyprland.start" then return end
        return realOn(event, ...)
    end
    local ok, runErr = pcall(ambxstChunk)
    hl.on = realOn
    if not ok then
        warnOnScreen("ambxst hyprland.lua failed: " .. tostring(runErr))
    end
else
    warnOnScreen("ambxst hyprland.lua not loaded: " .. tostring(ambxstLoadErr))
end


-- OVERRIDES
-- Down here you can write or load anything that you want to override from Ambxst's settings.

-- Re-assert allow_session_lock_restore AFTER the ambxst chunk. ambxst rewrites
-- the compositor config and triggers a Hyprland reload on every launch (its
-- CompositorTomlWriter), and each of those reloads re-runs this whole file. The
-- setting in the misc block above already survives that today because ambxst's
-- generated config does not touch this key, but ambxst restarts constantly (on
-- every output-loss crash, via ambxst.service), so keep the guarantee here too:
-- if the locker dies while the session is locked, this is what lets the
-- restarted ambxst re-attach and take over instead of leaving Hyprland stuck on
-- the red "your lockscreen app died" screen with no client able to unlock it.
hl.config({ misc = { allow_session_lock_restore = true } })

-----------------
---- LAUNCHER ----
-----------------

-- Hyprland 0.56 regression for "launcher on Super alone". ambxst generates
--     hl.bind("SUPER + Super_L", hl.dsp.exec_cmd("ambxst run launcher"))
-- a plain press bind on the modifier key itself. On 0.55 the modifier state
-- did not yet include SUPER while Super_L's own press was evaluated (see the
-- bindr comment in KeybindManager.cpp), so that bind could only ever match on
-- release, and Hyprland's bind shadowing cancelled it whenever another key
-- was pressed in between. On 0.56 the state already includes SUPER, so the
-- bind matches the instant Super goes down -- before the number key -- and
-- SUPER+<n> opens the launcher on top of switching workspaces.
--
-- Fix: make it a release bind (the legacy `bindr`). That is in fact what
-- ~/.local/share/ambxst/axctl.toml asks for -- the launcher keybind there has
-- `flags = "r"` -- but axctl 0.0.16 drops the flag in both its ConfigGenerator
-- (`bind =`, not `bindr =`) and its LuaGenerator (no `{ release = true }`).
-- Verified 2026-09-03 with an injected uinput keyboard: SUPER+2 fires only the
-- workspace switch, Super alone fires only the launcher. hl.unbind removes
-- every bind on that key, so ambxst's press-mode one goes away first.
hl.unbind("SUPER + Super_L")
hl.bind("SUPER + Super_L", hl.dsp.exec_cmd("ambxst run launcher"), { release = true })


--------------
---- RESIZE --
--------------

-- Works around an axctl 0.0.16 bug. Its ConfigGenerator emits the *dispatcher*
--     bind = SUPER ALT, Right, resizeactive, 50 0
-- but its LuaGenerator wraps that dispatcher name in exec_cmd:
--     hl.bind("SUPER + ALT + Right", hl.dsp.exec_cmd("resizeactive 50 0"))
-- There is no `resizeactive` binary, so on the .lua path all eight SUPER+ALT
-- resize binds are silent no-ops. Re-bind them here, after the ambxst load.
-- Duplicate binds stack rather than replace, but the ambxst ones do nothing,
-- so the net effect is one working resize per key.
--
-- hl.dsp.window.resize defaults to ABSOLUTE sizing; `relative = true` is what
-- reproduces resizeactive's delta behaviour. Verified live on 0.55 against a
-- floating 800x600 window: {x=50,y=0} left it at 800x600, {x=200,y=200} set it
-- to 200x200, and {x=50,y=0,relative=true} gave 850x600.
local function resizeBy(dx, dy)
    return hl.dsp.window.resize({ x = dx, y = dy, relative = true })
end

hl.bind(mainMod .. " + ALT + Right", resizeBy(50, 0))
hl.bind(mainMod .. " + ALT + l",     resizeBy(50, 0))
hl.bind(mainMod .. " + ALT + Left",  resizeBy(-50, 0))
hl.bind(mainMod .. " + ALT + h",     resizeBy(-50, 0))
hl.bind(mainMod .. " + ALT + Down",  resizeBy(0, 50))
hl.bind(mainMod .. " + ALT + j",     resizeBy(0, 50))
hl.bind(mainMod .. " + ALT + Up",    resizeBy(0, -50))
hl.bind(mainMod .. " + ALT + k",     resizeBy(0, -50))

--------------------------------
---- WINDOWS AND WORKSPACES ----
--------------------------------

-- See https://wiki.hypr.land/Configuring/Basics/Window-Rules/
-- and https://wiki.hypr.land/Configuring/Basics/Workspace-Rules/

-- Workspace 1 used to float every window by default (`float on, match:workspace 1`).
-- Dropped on 2026-09-03 -- workspace 1 is a normal tiling workspace now.

-- Full-width sizing for specific apps (whitelist). size/move only ever apply to
-- floating windows, so with workspace 1 tiling again these only kick in when
-- one of these apps is floated by hand.
-- Monitor: 1920x1200, reserved top: 40px (bar), 6px edge padding
hl.window_rule({
    name  = "zen-full-width",
    match = { class = "^(zen-beta)$" },
    size  = "1908 1148",
    move  = "6 46",
})
hl.window_rule({
    name  = "vscodium-full-width",
    match = { class = "^(codium|Code|VSCodium)$" },
    size  = "1908 1148",
    move  = "6 46",
})

-- Ignore maximize requests from apps.
hl.window_rule({
    name           = "suppress-maximize-events",
    match          = { class = ".*" },
    suppress_event = "maximize",
})

-- Fix some dragging issues with XWayland
hl.window_rule({
    name  = "fix-xwayland-drags",
    match = {
        class      = "^$",
        title      = "^$",
        xwayland   = true,
        float      = true,
        fullscreen = true,
        pin        = false,
    },
    no_focus = true,
})
