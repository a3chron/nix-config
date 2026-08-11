# Morning status (A3C-164): the first time Horus is loaded on a given day, and
# only before noon, he says ONE short spoken line — today's weather plus any
# personal todo that is due or urgent. Speakers only, never WhatsApp.
# Instructions live in ~/horus/skills/morning-status.md; this script only
# decides WHEN and delivers the answer.
#
# Why this is NOT part of horus-warmup.sh:
#  1. horus-warmup.service is a SYSTEM service running as root at OnBootSec=45s.
#     Speaking needs the user's pipewire session (wakeup.nix says the same about
#     the drain) — a system unit physically cannot deliver this.
#  2. Warmup's gates are about GPU/model state ("model already loaded → nothing
#     to do"), which is exactly wrong for "first load today".
#  3. Warmup is called from four places including EVERY paddle press
#     (horus-ptt.py dispatch_warmup) and every headphone connect
#     (horus-bt-watch.py). Hanging a network + LLM + TTS run off that hot path
#     is unacceptable.
#
# Driven by a minutely user timer (morning.nix) rather than a "first load" hook:
# the user manager only exists after login, so the poll is behaviourally
# identical to the spec — PC off until 10:00 → the session and container come up
# at 10:00 and the next tick fires; paused for days → every tick defers and the
# tick after `horus resume` fires; first load at 14:00 → gate 3, never fires.
# In the steady state a tick is one open() + a string compare.
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime

from horus_deliver import speak

HOME = "/home/a3chron"
# "YYYY-MM-DD", same convention as memory/.last-backup (backup.nix)
STAMP = f"{HOME}/horus/memory/.last-morning-status"
BACKOFF_STATE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "horus-morning.backoff")
CRASH_STATE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "horus-morning.crash")
SUDO = "/run/wrappers/bin/sudo"
MC = "/run/current-system/sw/bin/machinectl"
SYSTEMCTL = "/run/current-system/sw/bin/systemctl"
# pactl is NOT on the host PATH (only horus-voice.service adds pkgs.pulseaudio);
# wpctl ships with wireplumber and is in /run/current-system/sw/bin.
WPCTL = "/run/current-system/sw/bin/wpctl"
# procps is NOT on a user unit's PATH (systemd.user gives coreutils/findutils/
# grep/sed/systemd only), so a bare "pgrep" is FileNotFoundError — it crashed
# every tick 05:00-11:59, and each crash fired OnFailure. Absolute, like the
# host binaries above; horus-warmup.sh gets away with a bare pgrep because it
# is a SYSTEM unit with a fuller PATH.
PGREP = "/run/current-system/sw/bin/pgrep"

DEADLINE_HOUR = 12  # ticket: the morning status happens "before 12:00" or not at all
OFFLINE_GIVEUP_H = 11  # still offline at 11:00 → skip the day entirely
RUN_TIMEOUT = 300
PROBE_URL = "https://connectivitycheck.gstatic.com/generate_204"
PROBE_TIMEOUT = 2
BACKOFF_START = 60  # seconds
BACKOFF_MAX = 3600  # seconds
CRASH_ALERT_COOLDOWN = 3600  # seconds between OnFailure alerts for the same crash
GPU_APPS = "warthunder|minecraft|kdenlive|bambu|blender"  # same list as horus-warmup.sh


def log(msg):
    print(f"morning: {msg}", flush=True)


def today():
    return datetime.now().strftime("%Y-%m-%d")


def shquote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def stamp_done():
    with open(STAMP, "w") as f:
        f.write(today() + "\n")


def read_stamp():
    try:
        with open(STAMP) as f:
            return f.read().strip()
    except OSError:
        return ""


# --- offline backoff -------------------------------------------------------
# Kurt is often offline for a while after boot. Probing (and logging) every
# minute is noise, so misses back off 1, 2, 4, 8, … minutes, capped at 60. The
# state lives in XDG_RUNTIME_DIR, i.e. it resets on logout — the right scope,
# since "this login session has been offline for a while" is the fact we track.


def backoff_read():
    try:
        with open(BACKOFF_STATE) as f:
            nxt, delay = f.read().split()
        return float(nxt), int(delay)
    except (OSError, ValueError):
        return 0.0, 0


def backoff_bump():
    _, delay = backoff_read()
    delay = BACKOFF_START if delay <= 0 else min(delay * 2, BACKOFF_MAX)
    try:
        with open(BACKOFF_STATE, "w") as f:
            f.write(f"{time.time() + delay} {delay}")
    except OSError as e:
        log(f"cannot write backoff state: {e}")
    return delay


def backoff_clear():
    try:
        os.unlink(BACKOFF_STATE)
    except OSError:
        pass


def online():
    try:
        req = urllib.request.Request(PROBE_URL, method="HEAD")
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            return r.status < 500
    except Exception:
        return False


# --- gates -----------------------------------------------------------------


def gpu_busy():
    return subprocess.run([PGREP, "-fi", GPU_APPS], capture_output=True).returncode == 0


def stack_paused():
    """Same check as horus-wakeup-drain.stack_paused: which half of the stack
    `horus pause` took down, if any. Deferring while paused is what makes
    "resume after a pause" work with no changes to `horus resume`."""
    for unit in ("container@horus.service", "llama-swap.service"):
        if subprocess.run([SYSTEMCTL, "is-active", "--quiet", unit]).returncode != 0:
            return unit
    return None


def container_answering():
    """Cheaper version of briefing.nix's readiness probe — one shot, no retry
    loop: if the container is still booting we simply defer to the next tick."""
    try:
        return subprocess.run(
            [SUDO, "-n", MC, "shell", "horus@horus", "/run/current-system/sw/bin/true"],
            capture_output=True,
            timeout=20,
        ).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def pipewire_ready():
    try:
        return subprocess.run([WPCTL, "status"], capture_output=True, timeout=10).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# --- the run ---------------------------------------------------------------


def run_agent():
    """Returns (joined_text, died, detail, raw_lines). Same invocation shape and
    death detection as horus-wakeup-drain.run_agent / briefing.nix: a non-zero
    exit OR a last step_finish reason other than "stop" means the run died."""
    now = datetime.now()
    prompt = (
        f"[Morning status. It is {now.strftime('%Y-%m-%d')} ({now.strftime('%A')}), "
        f"{now.strftime('%H:%M')}. Kurt did NOT speak to you — this is the first time you were "
        "loaded today. Follow skills/morning-status.md exactly. Use the date above; do not look "
        "it up.]\n"
        "[IMPORTANT: your answer is read aloud on the speakers and goes nowhere else. ONE short "
        "flowing paragraph, under ~60 words, no lists, no markdown, no headings. If there is "
        "nothing worth saying, answer with exactly: skip]"
    )
    inner = (
        "cd /home/horus/work && "
        "OPENCODE_PERMISSION=$(cat .opencode/unattended-permission.json 2>/dev/null) "
        f"timeout {RUN_TIMEOUT} opencode run --format json {shquote(prompt)}; echo %%EXIT $?"
    )
    try:
        r = subprocess.run(
            [SUDO, "-n", MC, "shell", "horus@horus", "/run/current-system/sw/bin/bash", "-c", inner],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT + 60,
        )
    except subprocess.TimeoutExpired:
        return "", True, "machinectl timeout", []
    texts, finish, ec = [], None, None
    lines = r.stdout.replace("\r", "").split("\n")
    for line in lines:
        line = line.strip()
        if line.startswith("%%EXIT "):
            ec = line[7:].strip()
            continue
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "text":
            t = (ev.get("part") or {}).get("text", "").strip()
            if t:
                texts.append(t)
        elif ev.get("type") == "step_finish":
            finish = (ev.get("part") or {}).get("reason", finish)
    died = (ec not in (None, "0")) or (finish is not None and finish != "stop")
    return "\n".join(texts).strip(), died, f"finish={finish} exit={ec}", lines


def main():
    forced = bool(os.environ.get("HORUS_MORNING_FORCE"))

    # 1. already done today? (the common case — ~1400 ticks a day land here)
    if read_stamp() == today() and not forced:
        return

    # 2/3. after noon it does not happen at all — and NO stamp, so a run
    #      tomorrow morning is unaffected.
    if not forced and datetime.now().hour >= DEADLINE_HOUR:
        return

    # 4-7. defer (write nothing, retry next tick)
    if not forced and gpu_busy():
        log("GPU-heavy app running — deferring")
        return
    down = stack_paused()
    if down:
        log(f"stack paused ({down} inactive) — deferring")
        return
    if not container_answering():
        log("container not answering yet — deferring")
        return
    if not pipewire_ready():
        log("pipewire not ready — deferring")
        return

    # 8. offline: exponential backoff, and give up on the day at 11:00. Offline
    #    means no weather AND no Linear, so the whole status would be "I'm
    #    offline" — spoken noise, and the speakers must not talk for nothing.
    if not forced:
        nxt, _ = backoff_read()
        if time.time() < nxt:
            return  # not even worth probing yet
        if not online():
            if datetime.now().hour >= OFFLINE_GIVEUP_H:
                log(f"still offline at {OFFLINE_GIVEUP_H}:00 — skipping today's morning status")
                stamp_done()
                backoff_clear()
                return
            delay = backoff_bump()
            log(f"offline — retrying in {delay // 60} min")
            return
        backoff_clear()

    # 9. Stamp BEFORE running: at-most-once, same reasoning as the wake-up
    #    drain's "mark one-shots done before firing" and backup.nix's stamp.
    #    A duplicated spoken morning status is worse than a missed one, and a
    #    miss is not silent — the unit fails and OnFailure pings Kurt.
    stamp_done()

    log("running the morning status")
    joined, died, detail, raw = run_agent()
    if joined.lower().strip(" .!") == "skip":
        log("agent chose skip — nothing spoken")
        return
    if died or not joined:
        log(f"run died or empty ({detail})")
        for line in raw[-20:]:
            print(line, flush=True)
        # deliberately NO WhatsApp fallback: per A3C-164 the morning status is
        # speakers-only. The failed unit + OnFailure alert is the notification.
        sys.exit(1)
    log(f"speaking ({len(joined)} chars): {joined}")
    # cold Kokoro is likely here (horus-kokoro is PartOf horus-voice, so it is
    # only warm while the headphones are connected) — generous timeouts
    if not speak(joined, tts_timeout=120, play_timeout=300):
        log("speaking FAILED")
        sys.exit(1)
    log("done")


# --- crash alerting -------------------------------------------------------
# A minutely timer turns any crash BEFORE the stamp into one failed unit per
# minute, and alert.nix's OnFailure turns each of those into a WhatsApp message
# — the bare-`pgrep` bug sent one every minute from 05:00 to noon. So: the
# first crash of an episode still fails loudly (that is the notification), the
# repeats within the cooldown exit 0 and only log. Runtime-scoped state, like
# the offline backoff, so a fresh login alerts again.


def crash_should_alert():
    try:
        with open(CRASH_STATE) as f:
            last = float(f.read().strip())
        if time.time() - last < CRASH_ALERT_COOLDOWN:
            return False
    except (OSError, ValueError):
        pass
    try:
        with open(CRASH_STATE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass
    return True


try:
    main()
except SystemExit:
    raise  # the deliberate exit(1)s in main() are post-stamp, i.e. once a day
except Exception:
    traceback.print_exc()
    if crash_should_alert():
        log("crashed — alerting (further crashes stay quiet for an hour)")
        sys.exit(1)
    log(f"crashed again within {CRASH_ALERT_COOLDOWN // 60} min — alert suppressed")
