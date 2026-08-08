# Shared host-side delivery for un-initiated Horus output: things Horus says or
# sends when Kurt did NOT just talk to him — scheduled wake-ups
# (horus-wakeup-drain.py) and the morning status (horus-morning.py).
#
# Underscore in the name on purpose: this is an importable module, not a script
# (same pattern as horus_kokoro_core.py). Every consumer is launched as
# `python /home/a3chron/nixos-config/horus/horus-*.py`, so sys.path[0] is this
# directory and a plain `from horus_deliver import ...` resolves.
#
# ---------------------------------------------------------------------------
# DECISION: un-initiated audio goes to the PipeWire DEFAULT SINK. Never to a
# hard-picked device, never to a combine sink, never "the speakers".
#
# Rationale: headphones isolate well. If the headphones are the default sink and
# we forced output to the speakers, Kurt (wearing them) would never hear it. If
# the speakers are the default sink and we forced output to the headphones, he
# would be talking to a pair of headphones lying on the desk. The default sink
# is by definition where the user currently wants audio, so it is always the
# best guess — and it is the only one that needs no state.
#
# The ONE exception in this codebase is horus-voice-respond.sh's play(), which
# targets bluez_output explicitly. That is not un-initiated output: a voice
# round means the headphones are on by definition, and right after the HFP->A2DP
# flip the *default* sink can briefly point elsewhere (e.g. easyeffects) and the
# reply is lost silently. See the comment there.
# ---------------------------------------------------------------------------
import json
import os
import re
import subprocess
import tempfile
import urllib.request

# the host's audio producers (music :8877, read-aloud :8878) live in one place
import horus_players

HOME = "/home/a3chron"
CONTACTS = f"{HOME}/horus/memory/whatsapp-contacts.json"
TTS = "/run/current-system/sw/bin/horus-tts"
# Keep in sync with tts_speed in horus-voice-respond.sh — un-initiated output
# should not be spoken at a different rate than voice replies.
TTS_SPEED = "1.15"
PWPLAY = "/run/current-system/sw/bin/pw-play"
BRIDGE = "http://127.0.0.1:8765"


def log(msg):
    # unprefixed: journald already tags every line with the calling unit
    print(msg, flush=True)


def kurt_jid():
    try:
        with open(CONTACTS) as f:
            contacts = json.load(f)
        for name, jid in contacts.items():
            if name.lower().startswith("kurt"):
                return str(jid)
    except Exception as e:
        log(f"contacts unreadable: {e}")
    return None


def send_whatsapp(text):
    jid = kurt_jid()
    if not jid:
        log("no kurt JID — cannot deliver via whatsapp")
        return False
    try:
        req = urllib.request.Request(
            f"{BRIDGE}/send",
            data=json.dumps({"to": jid, "text": text}).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=70) as r:
            return r.status == 200
    except Exception as e:
        log(f"whatsapp send failed: {e}")
        return False


# markdown -> speakable text. Line-by-line port of the sed pipeline in
# horus-voice-respond.sh:87-96 (which is `sed -E`, i.e. per line, and only the
# marked substitutions are global). Without this an agent that answers
# "- **A3C-165** due Friday" gets "dash star star" read out: the voice pipeline
# strips markdown, the drain historically did not.
_MD = [
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),          # **bold**
    (re.compile(r"\*([^*]+)\*"), r"\1"),              # *italic*
    (re.compile(r"__([^_]+)__"), r"\1"),              # __bold__
    (re.compile(r"`+([^`]+)`+"), r"\1"),              # `code`
    (re.compile(r"^#+ +"), ""),                       # # heading
    (re.compile(r"^ *[-*+] +"), ""),                  # - bullet
    (re.compile(r"\[([^]]+)\]\([^)]*\)"), r"\1"),     # [text](url)
    (re.compile(r"https?://[^ )]+"), " link "),       # bare URL
    (re.compile(r"(\d+) *- *(\d+)"), r"\1 to \2"),    # 12-15 -> 12 to 15
]


def speakable(text):
    out = []
    for line in text.split("\n"):
        for pat, rep in _MD:
            line = pat.sub(rep, line)
        out.append(line)
    return "\n".join(out).strip()


_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def short_form(text, limit=160):
    """First sentence of `text`, hard-capped at `limit` chars on a word
    boundary. Returns (spoken, truncated) — `truncated` is True whenever
    anything was dropped, which is the signal that the model ignored a "keep it
    short" instruction and the prompt may need tuning."""
    t = text.strip()
    if not t:
        return "", False
    end = len(t)
    m = _SENTENCE_END.search(t)
    if m:
        end = min(end, m.end())
    nl = t.find("\n")
    if nl != -1:
        end = min(end, nl)
    first = t[:end].strip()
    truncated = len(first) < len(t)
    if len(first) <= limit:
        return first, truncated
    cut = first[:limit]
    sp = cut.rfind(" ")
    if sp > limit // 2:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-") + "…", True


def speak(text, tts_timeout=60, play_timeout=120):
    """Synthesize `text` and play it on the DEFAULT sink (see header). Returns
    True only if audio actually came out. The generous timeouts are for callers
    that run when horus-kokoro is down (it is PartOf horus-voice, i.e. only warm
    while the headphones are connected): horus-tts then falls through to the
    cold one-shot, which builds an ONNX session and the espeak backend first."""
    spoken = speakable(text)
    if not spoken.strip():
        log("nothing left to speak after markdown stripping")
        return False
    wav = None
    ducked = set()
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav = tf.name
        if subprocess.run(
            [TTS, "--speed", TTS_SPEED, "--out", wav, spoken],
            capture_output=True,
            timeout=tts_timeout,
        ).returncode != 0:
            return False
        if os.path.getsize(wav) == 0:
            return False
        # duck every host audio producer while speaking (same idea as voice
        # rounds) — a question mixed under a running song, or under an article
        # being read aloud, is easy to miss entirely
        ducked = horus_players.duck_all()
        ok = subprocess.run([PWPLAY, wav], capture_output=True, timeout=play_timeout).returncode == 0
        return ok
    except Exception as e:
        log(f"speak failed: {e}")
        return False
    finally:
        if ducked:
            horus_players.unduck_all(ducked)
        if wav:
            try:
                os.unlink(wav)
            except OSError:
                pass
