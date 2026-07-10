# Kokoro-82M TTS via onnxruntime (CPU) — one-shot synthesis for Horus replies.
# Builds a throwaway engine per call; the warm path is horus-kokoro-daemon.py,
# and horus-tts (voice.nix) prefers the daemon and only falls back to this.
# Still handy standalone for A/B-ing voices:
#
#   horus-tts --out /tmp/t.wav [--voice af_heart] [--speed 1.0] "text"
import argparse
import sys

from horus_kokoro_core import VOICE, Engine


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--out", required=True)
    ap.add_argument("--voice", default=VOICE)
    ap.add_argument("--speed", type=float, default=1.0)
    args = ap.parse_args()

    engine = Engine()
    if not engine.synth_to_wav(args.text, args.out, voice=args.voice, speed=args.speed):
        print("nothing speakable", file=sys.stderr)
        sys.exit(1)


main()
