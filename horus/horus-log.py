# Horus voice-log viewer (`horus log`): pretty, live view of the voice
# pipeline journal. Shows only events since launch; `f` prints the full
# retained transcript once, `q` quits. Catppuccin Mocha, names colored.
# Prints to the normal buffer (no alt screen) so terminal scrollback works.
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

    def rule(self, label):
        plain = f"── {label} "
        pad = "─" * max(0, self.width() - len(plain))
        print(f"\n{SURFACE2}── {label} {pad}{RESET}")
        self.last_role = None


def print_full_history(r):
    out = subprocess.run(
        ["journalctl", "--user", "-u", UNIT, "-o", "json", "--no-pager", "-n", "2000"],
        capture_output=True, text=True,
    ).stdout
    r.rule("full transcript")
    shown = 0
    for line in out.splitlines():
        try:
            ev = parse(json.loads(line))
        except json.JSONDecodeError:
            continue
        if ev:
            r.event(*ev)
            shown += 1
    if not shown:
        print(f"\n  {OVERLAY0}· journal is empty{RESET}")
    r.rule("end of history — live again")


def main():
    started = datetime.datetime.now().strftime("%H:%M")
    print(
        f"{OVERLAY0}horus log — live since {started}"
        f" · f full history · q quit{RESET}"
    )

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
        fds = [follower.stdout] + ([sys.stdin] if interactive else [])
        while True:
            ready, _, _ = select.select(fds, [], [])
            if sys.stdin in ready:
                key = sys.stdin.read(1)
                if key in ("q", "\x03", "\x04"):
                    break
                if key == "f":
                    print_full_history(r)
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
                    r.event(*ev)
    except KeyboardInterrupt:
        pass
    finally:
        if old_attrs:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
        follower.terminate()
        print(RESET)


main()
