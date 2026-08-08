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
import json
import urllib.request

MUSIC_URL = "http://127.0.0.1:8877"
READ_URL = "http://127.0.0.1:8878"
TIMEOUT = 3


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
        return
    if "music" in ducked:
        music_call("/resume", "POST")
    if "read" in ducked:
        read_call("/unduck", "POST")


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
