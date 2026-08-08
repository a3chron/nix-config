# Read-aloud: host-side daemon that fetches an article, extracts its text,
# synthesizes it with Kokoro in chunks and plays them through mpv on the HOST's
# DEFAULT sink. The agent's `read_aloud` tool in the container reaches it on
# 127.0.0.1:8878 over the shared network namespace — same pattern as music.nix
# (:8877) and studium.nix (:8899). Logic lives in ./horus-read.py, deliberately
# impure: the article extractor and the chunker WILL need per-site tuning and
# that must never cost a rebuild.
#
# Unlike horus-music's mpv, THIS mpv loads mpv-mpris, so a reading shows up as
# a normal media player on the desktop (ambxst's player widget) and Kurt's
# headphone play/pause button reaches it. The full key chain, all of it
# measured on 2026-08-08:
#
#   headphone play button
#     -> "Nothing Headphone (1) (AVRCP)" evdev KEY_PLAYPAUSE
#     -> horus-ptt.py grabs the device and re-emits it via uinput (:336,:352)
#     -> Hyprland bind XF86AudioPlay -> `playerctl play-pause`
#          (ambxst KeybindActions.js:102 -> its generated hyprland.conf:92)
#     -> MPRIS PlayPause on org.mpris.MediaPlayer2.mpv
#     -> mpv toggles pause; horus-read.py's monitor reconciles its own state
#
# TWO things had to be fixed for that chain to actually work, both measured
# rather than assumed:
#
#  1. playerctld (below). `playerctl` with no -p picks "the first available
#     player", and Zen/Firefox is permanently registered on this machine — with
#     both on the bus it picked FIREFOX every time, so the button would never
#     have reached a reading. playerctld proxies to the most-recently-active
#     player instead: after /speak that is mpv, after Kurt hits play in Zen it
#     is Zen. Measured: without it 0/4 key presses reached mpv, with it the
#     right player wins.
#
#  2. The duplicate XF86AudioPlay bind in home/hyprland.conf had to go.
#     Hyprland 0.55 executes EVERY matching bind (verified by binding one key
#     twice and counting: one press -> two executions), so with both our bind
#     and ambxst's, `playerctl play-pause` ran twice and the two MPRIS PlayPause
#     calls cancelled out. Measured against a real reading: 2 binds -> 1/3
#     presses worked, 1 bind -> 3/3. Firefox happens to coalesce the duplicate
#     calls, which is why this never showed up as a bug before mpv joined.
#
# Deliberately NOT adding mpris to music.nix: exactly one Horus mpv on the bus
# keeps "which player does the button mean" as unambiguous as it can be.
{ pkgs, ... }:

let
  # pkgs.mpv here is already wrapMpv (mpv-with-scripts), so `scripts` is the
  # supported idiom and wrapMpv supplies the mpris.so path — no store path and
  # no script filename ever appears in our code.
  mpvMpris = pkgs.mpv.override { scripts = [ pkgs.mpvScripts.mpris ]; };
in
{
  systemd.user.services.horus-read = {
    description = "Horus read-aloud daemon (article TTS player on 127.0.0.1:8878)";
    wantedBy = [ "default.target" ];
    path = [ mpvMpris ];
    unitConfig = {
      OnFailure = [ "horus-alert@%n.service" ];
      StartLimitIntervalSec = 0;
      ConditionUser = "a3chron"; # see music.nix — greeter session alert storm
    };
    serviceConfig = {
      # impure repo path, same pattern as horus-music
      ExecStart = "${pkgs.python3}/bin/python /home/a3chron/nixos-config/horus/horus-read.py";
      Restart = "on-failure";
      RestartSec = 5;
    };
  };

  # See the header: without this the headphone button talks to Zen, never to a
  # reading. playerctld is already an activatable name on the session bus; this
  # just makes sure it is actually running so `playerctl` prefers it.
  systemd.user.services.playerctld = {
    description = "playerctld — route media keys to the most recently active player";
    wantedBy = [ "default.target" ];
    unitConfig = {
      StartLimitIntervalSec = 0;
      ConditionUser = "a3chron"; # see music.nix
    };
    serviceConfig = {
      ExecStart = "${pkgs.playerctl}/bin/playerctld daemon";
      Restart = "on-failure";
      RestartSec = 5;
    };
  };

  environment.systemPackages = [ pkgs.playerctl ];
}
