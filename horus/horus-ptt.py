# Horus PTT daemon: paddle-right (KEY_NEXTSONG) starts a voice query.
# Recording auto-stops on trailing silence (the headset sends NO AVRCP button
# events while in HFP mode, so a second press cannot be the stop signal).
# Audio: explicit BT profile switch to HFP for the mic, restored afterwards;
# if the user was already on HFP (call/meeting), profiles are left untouched.
import array
import os
import subprocess
import sys
import time
import wave

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

RATE = 16000
CHUNK_BYTES = RATE * 2 // 10        # 0.1s of s16 mono
BASELINE_CHUNKS = 5                 # first 0.5s calibrates ambient noise
SILENCE_HOLD_S = 1.2                # this much trailing quiet ends the recording
MAX_RECORD_S = 45
MIN_RECORD_S = 1.0


def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        print(f"timeout: {' '.join(cmd)}", file=sys.stderr, flush=True)
        return None


def chime(path, wait=True):
    try:
        p = subprocess.Popen(["pw-play", path])
        if wait:
            p.wait(timeout=4)
    except Exception as e:
        print(f"chime failed: {e}", file=sys.stderr, flush=True)


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


def bt_source():
    r = run(["pactl", "list", "sources", "short"])
    if not r:
        return None
    for line in r.stdout.splitlines():
        if "bluez_input" in line and "monitor" not in line:
            return line.split("\t")[1]
    return None


def wait_bt_sink(timeout_s=4.0):
    end = time.time() + timeout_s
    while time.time() < end:
        r = run(["pactl", "list", "sinks", "short"])
        if r and "bluez_output" in r.stdout:
            time.sleep(0.4)  # small extra settle after the sink appears
            return True
        time.sleep(0.3)
    return False


def rms(chunk):
    samples = array.array("h", chunk[: len(chunk) - (len(chunk) % 2)])
    if not samples:
        return 0
    return int((sum(s * s for s in samples) / len(samples)) ** 0.5)


def record_until_silence():
    """Returns path to wav, or None if nothing usable was captured."""
    prev = active_profile()
    was_hfp = prev is not None and prev.startswith("headset-head-unit")

    chime(CHIME_START, wait=True)  # finish the blip BEFORE we tear down A2DP
    if not was_hfp:
        run(["pactl", "set-card-profile", CARD, "headset-head-unit"])

    source = None
    for _ in range(12):
        source = bt_source()
        if source:
            break
        time.sleep(0.3)
    if not source:
        print("no bluetooth mic source found", file=sys.stderr, flush=True)
        if prev and not was_hfp:
            run(["pactl", "set-card-profile", CARD, prev])
        return None
    time.sleep(0.4)

    print(f"recording from {source} (was on {prev})", flush=True)
    rec = subprocess.Popen(
        ["parec", f"--device={source}", "--format=s16le", f"--rate={RATE}", "--channels=1"],
        stdout=subprocess.PIPE,
    )
    audio = bytearray()
    baseline = None
    baseline_samples = []
    heard_speech = False
    quiet_for = 0.0
    started = time.time()
    try:
        while True:
            chunk = rec.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            audio.extend(chunk)
            level = rms(chunk)
            elapsed = time.time() - started

            if baseline is None:
                baseline_samples.append(level)
                if len(baseline_samples) >= BASELINE_CHUNKS:
                    baseline = max(100, sorted(baseline_samples)[len(baseline_samples) // 2])
                continue

            speech_thresh = max(500, baseline * 3)
            if level >= speech_thresh:
                heard_speech = True
                quiet_for = 0.0
            else:
                quiet_for += 0.1

            if heard_speech and quiet_for >= SILENCE_HOLD_S and elapsed >= MIN_RECORD_S:
                break
            if elapsed >= MAX_RECORD_S:
                break
            if not heard_speech and elapsed >= 8.0:
                break  # user pressed but never spoke
    finally:
        rec.terminate()
        try:
            rec.wait(timeout=3)
        except subprocess.TimeoutExpired:
            rec.kill()
            rec.wait()
        if not was_hfp and prev:
            run(["pactl", "set-card-profile", CARD, prev])
            wait_bt_sink()
        chime(CHIME_STOP, wait=True)

    if not heard_speech:
        print("no speech detected", flush=True)
        return None
    with wave.open(WAV, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(audio))
    return WAV


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
    try:
        for ev in dev.read_loop():
            if ev.type == ecodes.EV_KEY and ev.code == PTT_KEY:
                if ev.value == 1:
                    wav = record_until_silence()
                    if wav:
                        print("responding...", flush=True)
                        subprocess.run(["horus-voice-respond", wav], timeout=600)
                    # drop paddle presses queued while we were busy
                    try:
                        while dev.read_one() is not None:
                            pass
                    except BlockingIOError:
                        pass
            else:
                ui.write_event(ev)  # pass through play/pause etc.
                ui.syn()
    finally:
        dev.ungrab()


main()
