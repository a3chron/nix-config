# Horus voice-log viewer (`horus log`): pretty, live view of the voice
# pipeline journal. Shows only events since launch; `f` toggles between that
# and the full retained transcript, `q` quits. Catppuccin Mocha, names colored.
#
# Rounds render as: recording (9.5s) -> transcribing (0.7s) -> Kurt block ->
# thinking (25.9s) -> Horus block -> synth (3.2s). Stage lines appear live
# ("thinking…") and are rewritten in place once their duration is known.
import datetime
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import textwrap
import tty

UNIT = os.environ.get("HORUS_LOG_UNIT", "horus-voice")

# Catppuccin Mocha
SAPPHIRE = "\x1b[38;2;116;199;236m"  # Kurt (petrol blue)
PEACH = "\x1b[38;2;250;179;135m"     # Horus (claude-ish orange)
OVERLAY0 = "\x1b[38;2;108;112;134m"  # timestamps, stage/system lines
SURFACE2 = "\x1b[38;2;88;91;112m"    # borders
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

NAME_COLOR = {"Kurt": SAPPHIRE, "Horus": PEACH}


def parse(entry):
    """journal json entry -> (kind, text, ts) or None"""
    msg = entry.get("MESSAGE", "")
    if isinstance(msg, list):  # journald encodes non-utf8 as byte arrays
        msg = bytes(msg).decode("utf-8", errors="replace")
    ts = int(entry.get("__REALTIME_TIMESTAMP", "0")) / 1e6
    if msg.startswith("heard: "):
        text = msg[len("heard: "):].strip()
        if not text:
            return ("sys", "didn't catch anything", ts)
        return ("kurt", text, ts)
    if msg.startswith("reply part: "):
        return ("horus", msg[len("reply part: "):].strip(), ts)
    if msg.startswith("recording from"):
        return ("rec_start", "", ts)
    if msg.startswith("responding..."):
        return ("rec_done", "", ts)
    m = re.match(r"synth: (\d+)ms", msg)
    if m:
        return ("synth", f"{int(m.group(1)) / 1000:.1f}", ts)
    for prefix, label in [
        ("no speech detected", "no speech detected"),
        ("no reply text received", "no reply — spoke fallback"),
        ("no bluetooth mic source found", "no bluetooth mic found"),
        ("kokoro failed", "kokoro failed — piper fallback"),
    ]:
        if msg.startswith(prefix):
            return ("sys", label, ts)
    return None


class Renderer:
    """Streams events as bordered blocks with live, in-place-updated stage
    lines. live=False (history redraw) prints only the finished stage forms."""

    def __init__(self, live=True):
        self.live = live
        self.last_role = None   # consecutive horus parts merge into one block
        self.stage_open = None  # (label, start_ts)
        self.stage_printed = False

    def width(self):
        return min(shutil.get_terminal_size().columns, 100)

    def sysline(self, text):
        print(f"\n  {OVERLAY0}· {text}{RESET}")

    def stage(self, label, ts):
        self.finish_stage(None)
        self.stage_open = (label, ts)
        self.stage_printed = False
        if self.live:
            print(f"\n  {OVERLAY0}· {label}…{RESET}")
            self.stage_printed = True

    def finish_stage(self, ts):
        if not self.stage_open:
            return
        label, t0 = self.stage_open
        suffix = f" ({ts - t0:.1f}s)" if ts is not None else "…"
        line = f"  {OVERLAY0}· {label}{suffix}{RESET}"
        if self.stage_printed:
            print(f"\x1b[1A\x1b[2K{line}")  # rewrite the live "label…" line
        else:
            print(f"\n{line}")
        self.stage_open = None
        self.stage_printed = False

    def header(self, name, ts):
        hhmm = datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
        plain = f"── {hhmm} ─ {name} "
        pad = "─" * max(0, self.width() - len(plain))
        print(
            f"\n{SURFACE2}── {RESET}{OVERLAY0}{hhmm}{RESET}{SURFACE2} ─ {RESET}"
            f"{BOLD}{NAME_COLOR[name]}{name}{RESET} {SURFACE2}{pad}{RESET}\n"
        )

    def body(self, text):
        for line in textwrap.wrap(text, self.width() - 4) or [""]:
            print(f"  {line}")

    def event(self, kind, text, ts):
        if kind == "rec_start":
            self.stage("recording", ts)
        elif kind == "rec_done":
            self.finish_stage(ts)
            self.stage("transcribing", ts)
        elif kind == "kurt":
            self.finish_stage(ts)
            self.header("Kurt", ts)
            self.body(text)
            self.last_role = "kurt"
            self.stage("thinking", ts)
        elif kind == "horus":
            if self.last_role != "horus":
                self.finish_stage(ts)
                self.header("Horus", ts)
            self.body(text)
            self.last_role = "horus"
        elif kind == "synth":
            print(f"  {OVERLAY0}· synth ({text}s){RESET}")
        elif kind == "sys":
            self.finish_stage(ts)
            self.sysline(text)


def full_history_events():
    out = subprocess.run(
        ["journalctl", "--user", "-u", UNIT, "-o", "json", "--no-pager", "-n", "3000"],
        capture_output=True, text=True,
    ).stdout
    events = []
    for line in out.splitlines():
        try:
            ev = parse(json.loads(line))
        except json.JSONDecodeError:
            continue
        if ev:
            events.append(ev)
    return events


def redraw(title, events):
    print("\x1b[H\x1b[2J", end="")  # clear screen, cursor home
    print(f"{OVERLAY0}horus log — {title} · q quit{RESET}")
    r = Renderer(live=False)
    for ev in events:
        r.event(*ev)
    if r.stage_open:  # e.g. still thinking right now
        label, _ = r.stage_open
        print(f"\n  {OVERLAY0}· {label}…{RESET}")
        r.stage_printed = True  # the live renderer may rewrite it with a duration
    if not events:
        print(f"\n  {OVERLAY0}· nothing yet{RESET}")
    live = Renderer(live=True)
    live.last_role = r.last_role
    live.stage_open, live.stage_printed = r.stage_open, r.stage_printed
    return live


def main():
    started = datetime.datetime.now().strftime("%H:%M")
    session_events = []  # everything since launch, for redrawing the live view
    full_mode = False

    follower = subprocess.Popen(
        ["journalctl", "--user", "-u", UNIT, "-f", "-o", "json", "--since", "now", "-n", "0"],
        stdout=subprocess.PIPE, text=True,
    )
    # non-blocking + own line buffer: a plain readline() only surfaces one
    # buffered line per select() wakeup, so the view lagged one message behind
    os.set_blocking(follower.stdout.fileno(), False)
    pending = ""

    def draw():
        if full_mode:
            return redraw("full transcript · f back to session", full_history_events())
        return redraw(f"live since {started} · f full transcript", session_events)

    interactive = sys.stdin.isatty()
    old_attrs = termios.tcgetattr(sys.stdin.fileno()) if interactive else None
    if interactive:
        tty.setcbreak(sys.stdin.fileno())
    try:
        r = draw()
        fds = [follower.stdout] + ([sys.stdin] if interactive else [])
        while True:
            ready, _, _ = select.select(fds, [], [])
            if sys.stdin in ready:
                key = sys.stdin.read(1)
                if key in ("q", "\x03", "\x04"):
                    break
                if key == "f":
                    full_mode = not full_mode
                    r = draw()
            if follower.stdout in ready:
                data = follower.stdout.read()
                if data == "":
                    print(f"\n  {OVERLAY0}· journal stream ended{RESET}")
                    break
                pending += data or ""
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    try:
                        ev = parse(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if ev:
                        session_events.append(ev)
                        r.event(*ev)  # renders into whichever view is shown
    except KeyboardInterrupt:
        pass
    finally:
        if old_attrs:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
        follower.terminate()
        print(RESET)


main()
