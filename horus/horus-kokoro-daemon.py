# Warm Kokoro TTS daemon: builds the ONNX session + espeak backend + voices
# ONCE and serves synth requests over a unix socket, so each spoken reply pays
# only inference cost — not the ~model-load + phonemizer-init tax the one-shot
# CLI pays every time. Started alongside horus-voice (voice.nix); horus-tts
# prefers it and falls back to the cold one-shot if it isn't up.
#
# Protocol: one JSON line per request -> {"text","out","voice"?,"speed"?}
#           reply: "ok\n" | "empty\n" | "err: <msg>\n"
import json
import os
import signal
import socketserver
import sys

from horus_kokoro_core import VOICE, Engine, socket_path

ENGINE = None
SOCK = socket_path()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline()
        if not line:
            return
        try:
            req = json.loads(line)
            ok = ENGINE.synth_to_wav(
                req["text"], req["out"],
                voice=req.get("voice") or VOICE,
                speed=float(req.get("speed", 1.0)),
            )
            self.wfile.write(b"ok\n" if ok else b"empty\n")
        except Exception as e:  # never let one bad request kill the daemon
            self.wfile.write(f"err: {e}\n".encode())


def cleanup(*_):
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    sys.exit(0)


def main():
    global ENGINE
    ENGINE = Engine()  # the expensive, once-only load
    try:
        os.unlink(SOCK)  # drop a stale socket from an unclean exit
    except FileNotFoundError:
        pass
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    server = socketserver.UnixStreamServer(SOCK, Handler)
    print(f"horus-kokoro daemon ready on {SOCK}", flush=True)
    try:
        server.serve_forever()
    finally:
        cleanup()


main()
