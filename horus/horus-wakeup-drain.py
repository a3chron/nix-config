# Drain for Horus's self-scheduled wake-ups (queue written by the container
# tool ~/horus/.opencode/tools/wakeup.ts, format documented there). Runs every
# minute via wakeup.nix (user timer). For each due job: run the agent in the
# container with the job's message, deliver the answer over the job's channel
# (speak = horus-tts + pw-play on the host's default sink, whatsapp = bridge
# /send, auto = speak with whatsapp fallback). Semantics: repeating jobs are
# rescheduled BEFORE firing (a drain crash can't double-fire them); one-shots
# are marked done before firing (no double-send) — fire() itself reports a
# failed run to Kurt via WhatsApp, so a failure is never silent either way.
import fcntl
import json
import os
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timedelta

HOME = "/home/a3chron"
QUEUE = f"{HOME}/horus/memory/reminders/queue.jsonl"
LOCK = f"{HOME}/horus/memory/reminders/.drain.lock"
CONTACTS = f"{HOME}/horus/memory/whatsapp-contacts.json"
SUDO = "/run/wrappers/bin/sudo"
MC = "/run/current-system/sw/bin/machinectl"
TTS = "/run/current-system/sw/bin/horus-tts"
PWPLAY = "/run/current-system/sw/bin/pw-play"
BRIDGE = "http://127.0.0.1:8765"
RUN_TIMEOUT = 300
STALE_REPEAT_H = 12  # repeating occurrences older than this go to the catch-up summary, not fired individually
COMPACT_AT = 200  # lines


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def log(msg):
    print(f"wakeup-drain: {msg}", flush=True)


def replay(lines):
    jobs = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        op = e.get("op")
        if op == "add":
            jobs[e["id"]] = e
        elif op in ("cancel", "done"):
            jobs.pop(e.get("id"), None)
        elif op == "reschedule" and e.get("id") in jobs:
            jobs[e["id"]]["next"] = e["next"]
    return jobs


def append(entry):
    with open(QUEUE, "a") as f:
        f.write(json.dumps(entry) + "\n")


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


def speak(text):
    wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav = tf.name
        if subprocess.run([TTS, "--out", wav, text], capture_output=True, timeout=60).returncode != 0:
            return False
        if os.path.getsize(wav) == 0:
            return False
        ok = subprocess.run([PWPLAY, wav], capture_output=True, timeout=120).returncode == 0
        return ok
    except Exception as e:
        log(f"speak failed: {e}")
        return False
    finally:
        if wav:
            try:
                os.unlink(wav)
            except OSError:
                pass


def run_agent(job):
    """Returns (texts, died, detail)."""
    prompt = (
        f"[Scheduled wake-up you set for yourself (id {job['id']}, due {job['next']}, now {now_str()}). "
        "Kurt did NOT just speak to you — a timer fired. Your message to yourself:]\n"
        f"{job['message']}\n"
        "[IMPORTANT: Do what the message says (your tools work normally). Every text block you emit "
        f"is delivered to Kurt via {job['channel']} — keep it SHORT and conversational, it may be "
        "read aloud: no lists, no markdown. If there is nothing worth delivering right now, answer "
        "with exactly the single word: skip]"
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
        return [], True, "machinectl timeout"
    texts, finish, ec = [], None, None
    for line in r.stdout.replace("\r", "").split("\n"):
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
    return texts, died, f"finish={finish} exit={ec}"


def shquote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def deliver(job, text):
    ch = job["channel"]
    if ch == "speak":
        return speak(text) or send_whatsapp(f"(speakers unavailable) {text}")
    if ch == "whatsapp":
        return send_whatsapp(text)
    # auto: speakers first, whatsapp fallback
    return speak(text) or send_whatsapp(text)


def fire_catchup(missed):
    """One grouped run for everything that fired into the void while the PC
    was off — '14× lights2130, 2× plantsweek' — so Horus starts back up
    knowing what he slept through and says one sensible thing (or nothing)."""
    lines = [
        f"- {count}x {job['id']} (last due {job['next']}): {job['message'][:100]}"
        for job, count in missed[:10]
    ]
    if len(missed) > 10:
        lines.append(f"- …and {len(missed) - 10} more job(s)")
    summary = "\n".join(lines)
    counts = ", ".join("{}x {}".format(c, j["id"]) for j, c in missed)
    log(f"catch-up: {counts}")
    job = {
        "id": "catchup",
        "next": now_str(),
        "channel": "auto",
        "message": (
            "The PC was off; these scheduled wake-ups of yours were missed (grouped, count x job):\n"
            f"{summary}\n"
            "Decide what is still worth acting on or saying to Kurt NOW, in one short message at "
            "most — e.g. the plant nudge may still be relevant, 14 missed evening lights check-ins "
            "are not. If nothing is worth saying, answer exactly: skip"
        ),
    }
    fire(job)


def fire(job):
    log(f"firing {job['id']} ({job['next']}, {job['channel']}): {job['message'][:80]}")
    texts, died, detail = run_agent(job)
    joined = "\n".join(texts).strip()
    if joined.lower() == "skip":
        log(f"{job['id']}: agent chose skip")
        return
    if not joined or died:
        log(f"{job['id']}: run died or empty ({detail})")
        send_whatsapp(
            f"Heads-up: my scheduled wake-up '{job['message'][:60]}…' (id {job['id']}) failed ({detail}). "
            "I'll try again at its next occurrence if it repeats."
        )
        return
    if not deliver(job, joined):
        log(f"{job['id']}: delivery failed on all channels")


def main():
    os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
    if not os.path.exists(QUEUE):
        return
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("previous drain still running — skipping this tick")
            return
        with open(QUEUE) as f:
            lines = f.readlines()
        jobs = replay(lines)
        now = now_str()
        due = [j for j in jobs.values() if j["next"] <= now]
        catchup = []
        for job in due:
            overdue_h = (datetime.now() - datetime.strptime(job["next"], "%Y-%m-%d %H:%M")).total_seconds() / 3600
            if job["repeat"] != "none":
                # reschedule FIRST so a crash can't double-fire a repeating job;
                # count the occurrences that fired into the void meanwhile
                step = timedelta(days=1 if job["repeat"] == "daily" else 7)
                nxt = datetime.strptime(job["next"], "%Y-%m-%d %H:%M")
                missed = 0
                while nxt.strftime("%Y-%m-%d %H:%M") <= now:
                    nxt += step
                    missed += 1
                append({"op": "reschedule", "id": job["id"], "next": nxt.strftime("%Y-%m-%d %H:%M")})
                if overdue_h > STALE_REPEAT_H:
                    # PC was off — collect for ONE grouped catch-up run instead
                    # of silently skipping (or worse, firing 14 stale check-ins)
                    catchup.append((job, missed))
                    continue
            else:
                append({"op": "done", "id": job["id"], "firedAt": now})
            fire(job)
        if catchup:
            fire_catchup(catchup)
        # compact under the lock once the log grows — active jobs only
        if len(lines) > COMPACT_AT:
            active = replay(open(QUEUE).readlines())  # re-read: we appended above
            tmp = QUEUE + ".tmp"
            with open(tmp, "w") as f:
                for j in active.values():
                    f.write(json.dumps(j) + "\n")
            os.replace(tmp, QUEUE)
            log(f"compacted queue to {len(active)} active job(s)")


main()
