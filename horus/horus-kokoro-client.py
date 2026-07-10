# Thin client for the warm Kokoro daemon. Same CLI as horus-kokoro-tts.py, but
# instead of building an engine it hands the request to the running daemon over
# its unix socket. Exits 0 on a synthesized WAV, non-zero on anything else
# (daemon down, socket error, synth error) so the horus-tts wrapper can fall
# back to the cold one-shot. No heavy imports — must start instantly.
import argparse
import json
import socket
import sys

from horus_kokoro_core import VOICE, socket_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--out", required=True)
    ap.add_argument("--voice", default=VOICE)
    ap.add_argument("--speed", type=float, default=1.0)
    args = ap.parse_args()

    req = json.dumps(
        {"text": args.text, "out": args.out, "voice": args.voice, "speed": args.speed}
    ) + "\n"

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(30)
            s.connect(socket_path())
            s.sendall(req.encode())
            s.shutdown(socket.SHUT_WR)
            reply = s.makefile("rb").readline().decode().strip()
    except OSError as e:
        print(f"daemon unavailable: {e}", file=sys.stderr)
        sys.exit(2)

    if reply == "ok":
        sys.exit(0)
    print(f"daemon: {reply or 'no reply'}", file=sys.stderr)
    sys.exit(1)


main()
