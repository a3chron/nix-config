#!/usr/bin/env python3
# Horus studium daemon: a tiny localhost HTTP API that starts/stops the
# studium-progress dev server (Kurt's TUM dashboard, ~/Projects/studium-progress)
# on the HOST, so the agent in the container can bring it up on demand — same
# shared-network-namespace pattern as horus-music (:8877). Runs as a user
# service (studium.nix), impure repo path — edits need only
# `systemctl --user restart horus-studium`.
#
# The dev server is spawned as `nix develop -c pnpm dev`, which first syncs
# the grades from TUMonline (if .tum-token is present), then serves on :3005.
# Like horus-music manages mpv, this daemon owns the child process — stopping
# the daemon stops the server.
#
# API (JSON):
#   GET  /status  -> {"state": "stopped|starting|running", "port": 3005,
#                     "pid": ..., "started_at": unix-ts}
#                    "running" = the dev server accepts TCP on :3005
#   POST /start   -> spawns if needed, returns status (does NOT wait for ready;
#                    poll /status)
#   POST /stop    -> SIGTERM to the process group, returns status

import json
import os
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_DIR = Path(
    os.environ.get("STUDIUM_DIR", "/home/a3chron/Projects/studium-progress")
).resolve()
PORT = int(os.environ.get("STUDIUM_CTL_PORT", "8899"))
DEV_PORT = int(os.environ.get("STUDIUM_DEV_PORT", "3005"))

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_started_at: float = 0.0


def proc_alive() -> bool:
    return _proc is not None and _proc.poll() is None


def dev_port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", DEV_PORT), timeout=1):
            return True
    except OSError:
        return False


def status() -> dict:
    alive = proc_alive()
    up = dev_port_open()
    if up:
        state = "running"
    elif alive:
        state = "starting"
    else:
        state = "stopped"
    return {
        "state": state,
        "port": DEV_PORT,
        "url": f"http://localhost:{DEV_PORT}",
        "pid": _proc.pid if alive else None,
        "started_at": _started_at if alive else None,
    }


def start() -> dict:
    global _proc, _started_at
    with _lock:
        if proc_alive() or dev_port_open():
            return status()
        # own session -> we can SIGTERM the whole group (nix develop + pnpm + next).
        # stderr inherits to the unit journal: a failed TUMonline grade sync used
        # to vanish into DEVNULL and was unknowable; stdout (dev-server spam)
        # stays discarded.
        _proc = subprocess.Popen(
            ["nix", "develop", str(PROJECT_DIR), "-c", "pnpm", "dev"],
            cwd=PROJECT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=None,
            start_new_session=True,
        )
        _started_at = time.time()
        return status()


def stop() -> dict:
    global _proc
    with _lock:
        if proc_alive():
            try:
                os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                _proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(_proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    _proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        # don't forget a child that would not die: with _proc = None a stale
        # server holding :3005 would be reported as healthy "running" forever
        if proc_alive() and dev_port_open():
            print(f"horus-studium: child {_proc.pid} survived SIGKILL — orphan holding :{DEV_PORT}", flush=True)
            return {**status(), "state": "orphan", "error": f"dev server would not die, still holding :{DEV_PORT}"}
        _proc = None
        return status()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/status":
            self._send(200, status())
        else:
            self._send(404, {"error": "unknown endpoint"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path == "/start":
            self._send(200, start())
        elif path == "/stop":
            self._send(200, stop())
        else:
            self._send(404, {"error": "unknown endpoint"})

    def log_message(self, *args) -> None:  # quiet
        pass


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    finally:
        stop()


if __name__ == "__main__":
    main()
