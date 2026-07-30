#!/usr/bin/env python3
# Horus music daemon: a tiny localhost HTTP API around mpv, so the agent in
# the container can play Kurt's ~/Music on the HOST's audio (speakers or
# headphones — the sandbox has no sound device, but it shares the host
# network namespace, so it reaches this on 127.0.0.1:8877 via the `music`
# tool). Runs as a user service (music.nix), impure repo path like the voice
# scripts — edits need only `systemctl --user restart horus-music`.
#
# mpv is spawned lazily on the first /play (idle mode, kept around) and
# controlled over its JSON IPC socket. stdlib only — mpv comes from the
# service's PATH.
#
# API (JSON):
#   GET  /list?q=substr   -> {"files": [relative paths]}
#   GET  /status          -> {"state": "stopped|playing|paused", "file": ...,
#                             "position": s, "duration": s, "volume": 0-100,
#                             "started_at": unix-ts of the last loadfile}
#   POST /play    {"file": "rel/path.mp3", "volume": optional 0-100}
#   POST /pause   /resume   /stop
#   POST /volume  {"value": 0-100}
#
# started_at is load-bearing: horus-voice-respond.sh compares it against the
# round start to decide "a song just started -> the song IS the answer, skip
# TTS". Don't drop it.

import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MUSIC_DIR = Path(os.environ.get("HORUS_MUSIC_DIR", "/home/a3chron/Music")).resolve()
PORT = int(os.environ.get("HORUS_MUSIC_PORT", "8877"))
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".aac", ".wma"}
DEFAULT_VOLUME = 70

runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
MPV_SOCK = os.path.join(runtime_dir, "horus-music-mpv.sock")

_lock = threading.Lock()
_mpv: subprocess.Popen | None = None
_started_at: float = 0.0  # when the current file was loaded


def mpv_alive() -> bool:
    return _mpv is not None and _mpv.poll() is None


def ensure_mpv() -> None:
    """Spawn the idle mpv player if it isn't running (call with _lock held)."""
    global _mpv
    if mpv_alive():
        return
    try:
        os.unlink(MPV_SOCK)
    except FileNotFoundError:
        pass
    _mpv = subprocess.Popen(
        [
            "mpv",
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            f"--volume={DEFAULT_VOLUME}",
            f"--input-ipc-server={MPV_SOCK}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):  # wait for the IPC socket (max ~5s)
        if os.path.exists(MPV_SOCK):
            return
        if _mpv.poll() is not None:
            raise RuntimeError("mpv exited during startup")
        time.sleep(0.1)
    raise RuntimeError("mpv IPC socket did not appear")


def mpv_cmd(*command):
    """One mpv IPC request. Fresh connection per call: simple and mpv allows it."""
    req_id = 1
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3)
        s.connect(MPV_SOCK)
        s.sendall((json.dumps({"command": list(command), "request_id": req_id}) + "\n").encode())
        buf = b""
        while True:
            buf += s.recv(4096)
            for line in buf.split(b"\n"):
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("request_id") == req_id:  # skip async event lines
                    if msg.get("error") != "success":
                        raise RuntimeError(f"mpv: {msg.get('error')}")
                    return msg.get("data")
            buf = buf.rsplit(b"\n", 1)[-1]


def get_prop(name, default=None):
    try:
        return mpv_cmd("get_property", name)
    except Exception:
        return default


def list_files(query: str = "") -> list[str]:
    files = sorted(
        str(p.relative_to(MUSIC_DIR))
        for p in MUSIC_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    )
    if query:
        q = query.lower()
        files = [f for f in files if q in f.lower()]
    return files


def resolve_song(rel: str) -> Path:
    p = (MUSIC_DIR / rel).resolve()
    if not p.is_relative_to(MUSIC_DIR):
        raise ValueError("path escapes the music directory")
    if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
        raise FileNotFoundError(f"no such song: {rel}")
    return p


def status() -> dict:
    if not mpv_alive():
        return {"state": "stopped"}
    if get_prop("idle-active", True):
        return {"state": "stopped", "volume": get_prop("volume")}
    return {
        "state": "paused" if get_prop("pause", False) else "playing",
        "file": str(Path(get_prop("path", "")).relative_to(MUSIC_DIR))
        if get_prop("path") else None,
        "position": get_prop("time-pos"),
        "duration": get_prop("duration"),
        "volume": get_prop("volume"),
        "started_at": _started_at,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n == 0:
            return {}
        return json.loads(self.rfile.read(n))

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path == "/list":
            q = ""
            for part in qs.split("&"):
                if part.startswith("q="):
                    from urllib.parse import unquote_plus
                    q = unquote_plus(part[2:])
            self.send_json({"files": list_files(q)})
        elif path == "/status":
            with _lock:
                self.send_json(status())
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        global _started_at
        try:
            body = self.read_body()
            with _lock:
                if self.path == "/play":
                    song = resolve_song(str(body.get("file", "")))
                    ensure_mpv()
                    if isinstance(body.get("volume"), (int, float)):
                        mpv_cmd("set_property", "volume", max(0, min(100, body["volume"])))
                    mpv_cmd("loadfile", str(song))
                    mpv_cmd("set_property", "pause", False)
                    _started_at = time.time()
                    # brief wait until mpv has probed the file, so the status
                    # we return already carries duration/position
                    for _ in range(10):
                        if get_prop("duration") is not None:
                            break
                        time.sleep(0.1)
                elif self.path == "/pause":
                    mpv_alive() and mpv_cmd("set_property", "pause", True)
                elif self.path == "/resume":
                    mpv_alive() and mpv_cmd("set_property", "pause", False)
                elif self.path == "/stop":
                    mpv_alive() and mpv_cmd("stop")
                elif self.path == "/volume":
                    v = body.get("value")
                    if not isinstance(v, (int, float)):
                        raise ValueError("volume needs numeric 'value' 0-100")
                    ensure_mpv()
                    mpv_cmd("set_property", "volume", max(0, min(100, v)))
                else:
                    return self.send_json({"error": "not found"}, 404)
                self.send_json(status())
        except (ValueError, FileNotFoundError) as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    if not MUSIC_DIR.is_dir():
        raise SystemExit(f"music dir not found: {MUSIC_DIR}")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"horus-music: serving {MUSIC_DIR} on 127.0.0.1:{PORT}", flush=True)
    try:
        srv.serve_forever()
    finally:
        if mpv_alive():
            _mpv.terminate()


if __name__ == "__main__":
    main()
