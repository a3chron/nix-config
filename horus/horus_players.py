# The one place that knows the host's audio producers, for the PYTHON callers
# (horus-ptt.py, horus_deliver.py). The bash caller (horus-voice-respond.sh)
# has the same table in horus-audio-players.sh — keep the two in sync.
#
# Underscore in the name on purpose: importable module, not a script (same
# pattern as horus_kokoro_core.py / horus_deliver.py). Every consumer is
# launched as `python /home/a3chron/nixos-config/horus/horus-*.py`, so
# sys.path[0] is this directory and a plain `import horus_players` resolves.
#
# WHY the two players are ducked differently:
#   music (horus-music.py, :8877) has no /duck — it is deliberately unchanged,
#   so ducking it means "read /status, POST /pause, remember that we did".
#   read (horus-read.py, :8878) HAS /duck and /unduck, which carry a
#   pause_reason. That difference matters: /unduck only resumes a reading that
#   *we* paused for a voice round, so an article Kurt paused himself with the
#   headphone button survives a voice round untouched. Music keeps its blunt
#   "any paused song gets resumed" repair (see resume_all_blunt) because it has
#   no way to tell the two apart.
#
# THIRD producer (added 2026-08-10): everything else on the desktop, via MPRIS.
# The two daemons above are Horus's OWN players — but most of Kurt's music is
# YouTube/Spotify in Zen, which neither daemon knows about, so an un-initiated
# line (a wake-up, the morning status) was mixed straight over a running song.
# MPRIS is the only interface all of them share. We speak it with `busctl`
# (systemd, always on the host PATH) rather than playerctl, which lives in
# read.nix's systemPackages and is therefore missing on any generation built
# before it.
import json
import os
import subprocess
import urllib.request

MUSIC_URL = "http://127.0.0.1:8877"
READ_URL = "http://127.0.0.1:8878"
TIMEOUT = 3

BUSCTL = "/run/current-system/sw/bin/busctl"
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_IFACE = "org.mpris.MediaPlayer2.Player"
# Bus names the generic path must NOT touch:
#   playerctld — a PROXY for the others; acting on it acts twice on the real
#                player (read.nix's header documents the same double-call trap
#                for the Hyprland media-key binds)
#   mpv*       — the read-aloud player. read.nix's wrapped mpv is the only mpv
#                here that loads mpv-mpris, and it has /duck + /unduck with a
#                pause_reason, which MPRIS Pause would bypass and corrupt.
MPRIS_SKIP = ("playerctld", "mpv")
# Which MPRIS players WE paused, so a SIGKILLed round (horus-ptt.py's blunt
# repair, which gets no `ducked` set) can still put them back. Runtime-scoped:
# a leftover from a previous login is meaningless and must not resume anything.
MPRIS_STATE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "horus-ducked-mpris")


def call(base, path, method="GET"):
    """One request at a player daemon. None on any failure (daemon down,
    timeout, bad JSON) — every caller treats that as "this player is not
    playing", which is the safe reading for both ducking and resuming."""
    try:
        req = urllib.request.Request(f"{base}{path}", method=method)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except Exception:
        return None


def music_call(path, method="GET"):
    return call(MUSIC_URL, path, method)


def read_call(path, method="GET"):
    return call(READ_URL, path, method)


# --- generic MPRIS players (Zen/Spotify/…) ---------------------------------


def _busctl(*args):
    """One busctl call on the SESSION bus. None on any failure — same contract
    as call(): "this player is not playing" is the safe reading."""
    try:
        r = subprocess.run(
            [BUSCTL, "--user", *args], capture_output=True, text=True, timeout=TIMEOUT
        )
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def mpris_players():
    """Bus names of the players actually RUNNING right now. --acquired drops
    the activatable-but-not-started names (playerctld shows up as one), which
    would otherwise be D-Bus-activated by our own property read."""
    out = _busctl("list", "--acquired", "--no-legend", "--no-pager")
    if not out:
        return []
    names = []
    for line in out.splitlines():
        name = line.split()[0] if line.split() else ""
        if not name.startswith(MPRIS_PREFIX):
            continue
        if name[len(MPRIS_PREFIX) :].split(".")[0] in MPRIS_SKIP:
            continue
        names.append(name)
    return names


def mpris_playing(name):
    out = _busctl("get-property", name, MPRIS_PATH, MPRIS_IFACE, "PlaybackStatus")
    return bool(out) and "Playing" in out  # busctl prints: s "Playing"


def mpris_do(name, method):
    return _busctl("call", name, MPRIS_PATH, MPRIS_IFACE, method) is not None


def _mpris_state_write(names):
    try:
        if names:
            with open(MPRIS_STATE, "w") as f:
                f.write("\n".join(sorted(names)))
        else:
            os.unlink(MPRIS_STATE)
    except OSError:
        pass


def _mpris_state_read():
    try:
        with open(MPRIS_STATE) as f:
            return [n for n in f.read().split("\n") if n.strip()]
    except OSError:
        return []


def mpris_duck():
    """Pause every generic MPRIS player that is currently playing. Returns the
    bus names we paused."""
    paused = []
    for name in mpris_players():
        if mpris_playing(name) and mpris_do(name, "Pause"):
            paused.append(name)
    if paused:
        _mpris_state_write(paused)
    return paused


def mpris_unduck(names):
    """Resume exactly the players in `names`, and only those — Play, never
    PlayPause: if Kurt already restarted it himself, Play is a no-op, whereas
    PlayPause would stop the song he just resumed. A name that vanished
    meanwhile (browser closed) simply fails its call and is dropped."""
    for name in names:
        mpris_do(name, "Play")
    _mpris_state_write([])


def duck_all():
    """Pause whatever is currently AUDIBLE so Horus can be heard. Returns the
    set of players we actually paused — pass it back to unduck_all() so we only
    resume what we took away."""
    ducked = set()
    st = music_call("/status")
    if st and st.get("state") == "playing":
        if music_call("/pause", "POST") is not None:
            ducked.add("music")
    # read decides for itself whether it was playing (avoids a status race
    # against its own synth worker) and reports back whether it ducked
    r = read_call("/duck", "POST")
    if r and r.get("ducked"):
        ducked.add("read")
    # everything else on the desktop (Zen/Spotify/…), one entry per bus name so
    # unduck_all resumes exactly what it paused
    for name in mpris_duck():
        ducked.add(f"mpris:{name}")
    return ducked


def unduck_all(ducked=None):
    """Resume the players in `ducked`. With ducked=None this is the BLUNT
    repair path used after a SIGKILLed voice round, where nobody recorded what
    was paused: music resumes if it is paused at all (rare false positive,
    accepted — see horus-ptt.py), while read is asked to /unduck, which is
    self-guarding and will refuse unless it paused itself for a voice round."""
    if ducked is None:
        st = music_call("/status")
        if st and st.get("state") == "paused":
            if music_call("/resume", "POST") is not None:
                print("resumed music left paused by cancelled round", flush=True)
        read_call("/unduck", "POST")
        # the MPRIS half is NOT blunt: the killed round left the bus names it
        # paused in MPRIS_STATE, so we resume those and nothing else
        left = _mpris_state_read()
        if left:
            print(f"resuming {len(left)} MPRIS player(s) left paused by cancelled round", flush=True)
            mpris_unduck(left)
        return
    if "music" in ducked:
        music_call("/resume", "POST")
    if "read" in ducked:
        read_call("/unduck", "POST")
    mpris_unduck([d[len("mpris:") :] for d in ducked if d.startswith("mpris:")])


def any_started_since(ts):
    """True if any player STARTED something at or after `ts`. This is the
    "the audio IS the answer" rule: a song or an article that the agent kicked
    off during this round replaces the spoken reply, so every later TTS part
    must be suppressed or Horus talks over what he just started."""
    for base in (MUSIC_URL, READ_URL):
        st = call(base, "/status")
        if not st or st.get("state") in (None, "stopped", "idle"):
            continue
        started = st.get("started_at")
        if isinstance(started, (int, float)) and started >= ts:
            return True
    return False
