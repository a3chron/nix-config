# Drain for Horus's self-scheduled wake-ups (queue written by the container
# tool ~/horus/.opencode/tools/wakeup.ts, format documented there). Runs every
# minute via wakeup.nix (user timer). For each due job: run the agent in the
# container with the job's message, deliver the answer over the job's channel
# (speak = horus-tts + pw-play on the host's default sink, whatsapp = bridge
# /send, auto = speak with whatsapp fallback, both = a very short spoken line
# plus the full text on WhatsApp). Semantics: repeating jobs are
# rescheduled BEFORE firing (a drain crash can't double-fire them); one-shots
# are marked done before firing (no double-send) — fire() itself reports a
# failed run to Kurt via WhatsApp, so a failure is never silent either way.
# That last ordering means firing into a stopped container CONSUMES a one-shot,
# so a tick is deferred wholesale while the stack is paused (see stack_paused).
import fcntl
import json
import os
import re
import subprocess
from datetime import datetime, timedelta

# shared with horus-morning.py; also holds the "un-initiated audio goes to the
# DEFAULT sink" decision and the markdown->speech stripping
from horus_deliver import send_whatsapp, short_form, speak

HOME = "/home/a3chron"
QUEUE = f"{HOME}/horus/memory/reminders/queue.jsonl"
LOCK = f"{HOME}/horus/memory/reminders/.drain.lock"
SUDO = "/run/wrappers/bin/sudo"
MC = "/run/current-system/sw/bin/machinectl"
SYSTEMCTL = "/run/current-system/sw/bin/systemctl"
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


TWO_PART_INSTRUCTIONS = (
    "[IMPORTANT: Do what the message says (your tools work normally). Answer in TWO parts: "
    "first ONE very short spoken line — max ~8 words, no numbers or detail unless essential; "
    "it is read aloud on the speakers, so it must be as short as possible (e.g. \"reminder for "
    "the exam\", NOT \"Reminder for you to go to the exam, which you have in 2h...\"). Then a "
    "line containing only %%DETAIL. Then the fuller version, which is sent to Kurt on WhatsApp — "
    "he asks for more if he wants it. No lists, no markdown in either part. If there is nothing "
    "worth delivering right now, answer with exactly the single word: skip]"
)


def run_agent(job):
    """Returns (texts, died, detail)."""
    if job["channel"] == "both":
        tail = TWO_PART_INSTRUCTIONS
    else:
        tail = (
            "[IMPORTANT: Do what the message says (your tools work normally). Every text block you emit "
            f"is delivered to Kurt via {job['channel']} — keep it SHORT and conversational, it may be "
            "read aloud: no lists, no markdown. If there is nothing worth delivering right now, answer "
            "with exactly the single word: skip]"
        )
    prompt = (
        f"[Scheduled wake-up you set for yourself (id {job['id']}, due {job['next']}, now {now_str()}). "
        "Kurt did NOT just speak to you — a timer fired. Your message to yourself:]\n"
        f"{job['message']}\n" + tail
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


# The `both` channel splits ONE agent answer into a spoken half and a WhatsApp
# half on a sentinel line. Chosen over the alternatives because:
#  - "first text part is the short one" is actively wrong: opencode emits a text
#    part per streamed block, a short reply is often a single part (so there is
#    no long half at all), and a tool-call preamble ("let me check the lights")
#    is itself a text part — that would become the spoken half.
#  - a structured JSON reply forces schema wrangling onto a 35B local model on
#    the coldest run of the day; a malformed object degrades to nothing.
# The sentinel matches an idiom already used here (%%EXIT, and briefing.nix),
# survives markdown stripping, cannot occur accidentally in prose, and — the
# deciding property — is applied to the JOINED text, so arbitrary streaming
# splits do not matter.
SENTINEL = re.compile(r"(?mi)^[ \t]*%%\s*DETAIL[ \t]*:?[ \t]*$")


def split_two(joined):
    """(short, detail). No sentinel -> (joined, '')."""
    parts = SENTINEL.split(joined, maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def deliver(job, short, detail):
    ch = job["channel"]
    # speak/whatsapp/auto never see a sentinel (only `both` asks for one), so
    # rejoining is a no-op for them and their behaviour is unchanged.
    text = short if not detail else f"{short}\n{detail}".strip()
    if ch == "speak":
        if speak(text):
            log(f"{job['id']}: spoken on the default sink")
            return True
        # Under the default-sink rule a failed speak() no longer means "wrong
        # device" — it means no audio came out at all. An honestly-labelled
        # WhatsApp message beats losing the reminder, even when (as with
        # lights2130) it turns a spoken question into a written one.
        if send_whatsapp(f"(speakers unavailable) {text}"):
            log(f"{job['id']}: speakers failed — delivered via WhatsApp")
            return True
        return False
    if ch == "whatsapp":
        ok = send_whatsapp(text)
        if ok:
            log(f"{job['id']}: delivered via WhatsApp")
        return ok
    if ch == "both":
        spoken, truncated = short_form(short or detail, limit=160)
        if truncated:
            # either no sentinel at all, or the model ignored "one very short
            # line" — worth knowing about when tuning the prompt
            log(f"{job['id']}: spoken half had to be shortened to {len(spoken)} chars")
        wa = detail or short
        spoke = speak(spoken) if spoken else False
        sent = send_whatsapp(wa) if wa else False
        if spoke:
            log(f"{job['id']}: spoken on the default sink ({len(spoken)} chars)")
        else:
            log(f"{job['id']}: spoken half NOT delivered")
        if sent:
            log(f"{job['id']}: detail delivered via WhatsApp")
        else:
            log(f"{job['id']}: WhatsApp half NOT delivered")
        # deliberately NO "(speakers unavailable)" fallback here: the detail is
        # already on WhatsApp, so a fallback would just double-send it
        return spoke or sent
    # auto: speakers first, whatsapp fallback
    if speak(text):
        log(f"{job['id']}: spoken on the default sink")
        return True
    if send_whatsapp(text):
        log(f"{job['id']}: speakers unavailable — delivered via WhatsApp")
        return True
    return False


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
    texts, died, why = run_agent(job)
    joined = "\n".join(texts).strip()
    short, detail = split_two(joined)
    # For every channel but `both` there is no sentinel, so short == joined and
    # detail == "" — this is byte-identical to the old `joined.lower() == "skip"`
    # check. For `both` it also suppresses "skip\n%%DETAIL\nskip".
    if short.lower().strip(" .!") == "skip" and detail.lower().strip(" .!") in ("", "skip"):
        log(f"{job['id']}: agent chose skip")
        return
    if not joined or died:
        log(f"{job['id']}: run died or empty ({why})")
        send_whatsapp(
            f"Heads-up: my scheduled wake-up '{job['message'][:60]}…' (id {job['id']}) failed ({why}). "
            "I'll try again at its next occurrence if it repeats."
        )
        return
    if not deliver(job, short, detail):
        log(f"{job['id']}: delivery failed on all channels")


def stack_paused():
    """Which half of the stack `horus pause` took down, if any.

    Firing while paused doesn't just fail — it CONSUMES the job. One-shots are
    marked done before fire() (see the module docstring: that ordering exists so
    a crash can't double-send), so a reminder that comes due during a pause is
    struck off the queue, fails to reach a stopped container, and is gone. The
    WhatsApp "wake-up failed" notice tells Kurt, but the reminder itself is not
    retried. Deferring the whole tick keeps the job due instead, so it fires on
    resume. Repeating jobs already handle long gaps via STALE_REPEAT_H.
    """
    for unit in ("container@horus.service", "llama-swap.service"):
        if subprocess.run([SYSTEMCTL, "is-active", "--quiet", unit]).returncode != 0:
            return unit
    return None


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
        # checked only when something is actually due — this runs every minute,
        # and an unconditional check would spam the journal through every pause
        if due:
            down = stack_paused()
            if down:
                log(f"{len(due)} job(s) due but the stack is paused ({down} inactive) — deferring, they stay queued")
                return
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
