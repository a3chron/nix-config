# Horus PTT daemon: grabs the headphone AVRCP input device, uses paddle-right
# (KEY_NEXTSONG) as talk-toggle, re-injects everything else so normal media
# keys keep working. Deterministic mic handling: explicitly switch the BT card
# to HFP while recording, back to A2DP (LDAC) for playback.
import os
import subprocess
import sys
import time

import evdev
from evdev import InputDevice, UInput, ecodes

DEVICE_NAME = "Nothing Headphone (1) (AVRCP)"
PTT_KEY = ecodes.KEY_NEXTSONG  # paddle-right (the AI Button is silent on Linux)
MAC = os.environ.get("HORUS_HEADPHONE_MAC", "3C:B0:ED:A7:8B:42")
CARD = "bluez_card." + MAC.replace(":", "_")
WAV = "/tmp/horus-voice.wav"
SOUNDS = "/run/current-system/sw/share/sounds/freedesktop/stereo"
CHIME_START = f"{SOUNDS}/audio-volume-change.oga"
CHIME_STOP = f"{SOUNDS}/complete.oga"


def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        print(f"timeout: {' '.join(cmd)}", file=sys.stderr, flush=True)
        return None


def chime(path):
    # never let a stuck audio server stall the daemon
    subprocess.Popen(["pw-play", path])


def bt_source():
    r = run(["pactl", "list", "sources", "short"])
    if not r:
        return None
    for line in r.stdout.splitlines():
        if "bluez_input" in line or ("bluez" in line and "monitor" not in line):
            return line.split("\t")[1]
    return None


def active_profile():
    r = run(["pactl", "list", "cards"])
    if not r:
        return None
    in_card = False
    for line in r.stdout.splitlines():
        if line.startswith("\tName: "):
            in_card = line.split("Name: ")[1].strip() == CARD
        if in_card and "Active Profile:" in line:
            return line.split("Active Profile:")[1].strip()
    return None


def start_recording():
    chime(CHIME_START)
    # remember what the user was on: if already HFP (e.g. in a meeting),
    # don't touch profiles at all and restore to exactly this afterwards
    prev = active_profile()
    if prev is None or not prev.startswith("headset-head-unit"):
        run(["pactl", "set-card-profile", CARD, "headset-head-unit"])
    source = None
    for _ in range(10):  # wait for the HFP source to appear
        source = bt_source()
        if source:
            break
        time.sleep(0.3)
    if not source:
        print("no bluetooth mic source found", file=sys.stderr, flush=True)
        if prev:
            run(["pactl", "set-card-profile", CARD, prev])
        return None
    time.sleep(0.5)  # let the profile settle so the wav isn't empty at the start
    print(f"recording from {source} (was on {prev})", flush=True)
    rec = subprocess.Popen(["pw-record", "--target", source, "--rate", "16000", "--channels", "1", WAV])
    rec.horus_prev_profile = prev
    return rec


def stop_recording(rec):
    rec.terminate()
    rec.wait()
    prev = getattr(rec, "horus_prev_profile", None)
    if prev and not prev.startswith("headset-head-unit"):
        run(["pactl", "set-card-profile", CARD, prev])
        time.sleep(0.5)  # settle before the confirmation chime
    chime(CHIME_STOP)


def find_device():
    for path in evdev.list_devices():
        d = InputDevice(path)
        if d.name == DEVICE_NAME:
            return d
    return None


def main():
    dev = None
    for _ in range(30):
        dev = find_device()
        if dev:
            break
        time.sleep(2)
    if not dev:
        print("headphone input device not found", file=sys.stderr)
        sys.exit(1)

    print(f"grabbing {dev.path} ({dev.name})", flush=True)
    dev.grab()
    ui = UInput.from_device(dev, name="horus-ptt-passthrough")
    rec = None
    try:
        for ev in dev.read_loop():
            if ev.type == ecodes.EV_KEY and ev.code == PTT_KEY:
                if ev.value == 1:  # key down = toggle
                    if rec is None:
                        rec = start_recording()
                    else:
                        stop_recording(rec)
                        rec = None
                        print("responding...", flush=True)
                        subprocess.run(["horus-voice-respond", WAV], timeout=600)
            else:
                ui.write_event(ev)  # pass through play/pause etc.
                ui.syn()
    finally:
        if rec:
            rec.terminate()
            prev = getattr(rec, "horus_prev_profile", None)
            if prev:
                run(["pactl", "set-card-profile", CARD, prev])
        dev.ungrab()


main()
