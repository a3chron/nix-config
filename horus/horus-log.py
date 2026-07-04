# Horus voice-log viewer (`horus log`): pretty, live view of the voice
# pipeline journal. Shows only events since launch; `f` toggles between that
# and the full retained transcript, `q` quits. Catppuccin Mocha, names colored.
import datetime
import json
import select
import shutil
import signal
import subprocess
import sys
import termios
import textwrap
import tty

UNIT = "horus-voice"

# Catppuccin Mocha
SAPPHIRE = "\x1b[38;2;116;199;236m"  # Kurt (petrol blue)
PEACH = "\x1b[38;2;250;179;135m"     # Horus (claude-ish orange)
OVERLAY0 = "\x1b[38;2;108;112;134m"  # timestamps, system lines
SURFACE2 = "\x1b[38;2;88;91;112m"    # borders
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

NAME_COLOR = {"Kurt": SAPPHIRE, "Horus": PEACH}

# journal message -> muted one-liner (whitelist; everything else is dropped)
SYS_MAP = [
    ("recording from", "recording…"),
    ("responding...", "thinking…"),
    ("no speech detected", "no speech detected"),
    ("no reply text received", "no reply — spoke fallback"),
    ("no bluetooth mic source found", "no bluetooth mic found"),
]


def parse(entry):
    """journal json entry -> ('kurt'|'horus'|'sys', text, ts) or None"""
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
    for prefix, label in SYS_MAP:
        if msg.startswith(prefix):
            return ("sys", label, ts)
    return None


class Renderer:
    """Streams events as bordered blocks; consecutive horus parts merge."""

    def __init__(self):
        self.last_role = None

    def width(self):
        return min(shutil.get_terminal_size().columns, 100)

    def header(self, name, ts):
        hhmm = datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
        plain = f"── {hhmm} ─ {name} "
        pad = "─" * max(0, self.width() - len(plain))
        print(
            f"{SURFACE2}── {RESET}{OVERLAY0}{hhmm}{RESET}{SURFACE2} ─ {RESET}"
            f"{BOLD}{NAME_COLOR[name]}{name}{RESET} {SURFACE2}{pad}{RESET}\n"
        )

    def body(self, text):
        for line in textwrap.wrap(text, self.width() - 4) or [""]:
            print(f"  {line}")

    def event(self, role, text, ts):
        if role == "sys":
            print(f"\n  {OVERLAY0}· {text}{RESET}")
            return
        name = "Kurt" if role == "kurt" else "Horus"
        # only horus merges into the previous block: its reply streams in parts;
        # every kurt line is a distinct utterance and gets its own header
        if role == "kurt" or role != self.last_role:
            print()
            self.header(name, ts)
        self.body(text)
        self.last_role = role

def full_history_events():
    out = subprocess.run(
        ["journalctl", "--user", "-u", UNIT, "-o", "json", "--no-pager", "-n", "2000"],
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


def redraw(r, title, events):
    print("\x1b[H\x1b[2J", end="")  # clear screen, cursor home
    print(f"{OVERLAY0}horus log — {title} · q quit{RESET}")
    r.last_role = None
    for ev in events:
        r.event(*ev)
    if not events:
        print(f"\n  {OVERLAY0}· nothing yet{RESET}")


def main():
    started = datetime.datetime.now().strftime("%H:%M")
    session_events = []  # everything since launch, for redrawing the live view
    full_mode = False

    def draw():
        if full_mode:
            redraw(r, "full transcript · f back to session", full_history_events())
        else:
            redraw(r, f"live since {started} · f full transcript", session_events)

    follower = subprocess.Popen(
        ["journalctl", "--user", "-u", UNIT, "-f", "-o", "json", "--since", "now", "-n", "0"],
        stdout=subprocess.PIPE, text=True,
    )
    r = Renderer()
    interactive = sys.stdin.isatty()
    old_attrs = termios.tcgetattr(sys.stdin.fileno()) if interactive else None
    if interactive:
        tty.setcbreak(sys.stdin.fileno())
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        draw()
        fds = [follower.stdout] + ([sys.stdin] if interactive else [])
        while True:
            ready, _, _ = select.select(fds, [], [])
            if sys.stdin in ready:
                key = sys.stdin.read(1)
                if key in ("q", "\x03", "\x04"):
                    break
                if key == "f":
                    full_mode = not full_mode
                    draw()
            if follower.stdout in ready:
                line = follower.stdout.readline()
                if not line:
                    print(f"\n  {OVERLAY0}· journal stream ended{RESET}")
                    break
                try:
                    ev = parse(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if ev:
                    session_events.append(ev)
                    r.event(*ev)  # new events append to whichever view is shown
    except KeyboardInterrupt:
        pass
    finally:
        if old_attrs:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
        follower.terminate()
        print(RESET)


main()
