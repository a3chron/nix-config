#!/usr/bin/env python3
"""Horus remote-approval broker (A3C-167, phase 2).

WHAT IT IS
    A host-side daemon that watches the container's `opencode serve` event
    stream, and whenever an unattended run asks for permission it pings Kurt
    wherever he actually is (WhatsApp today, ntfy + Garmin watch in phase 5),
    waits for a two-character answer, and posts the verdict back to the server.

    Without it a permission asked by a non-interactive run is unanswerable:
    `opencode run` auto-rejects every permission.asked in-process, and the
    server's Permission.ask has NO timeout at all — an unanswered request
    blocks that session forever. The broker's expiry timer is the only thing
    in the whole system that can unblock a pending permission. Hence
    Restart=always and OnFailure=horus-alert@ in approval.nix.

WHY IT RUNS ON THE HOST AND NOT IN THE CONTAINER
    - It must outlive `horus pause` and container restarts; those are exactly
      the moments pending approvals need cleaning up.
    - It will hold the ntfy HMAC key (phase 5), which must live somewhere the
      agent cannot read. ~/horus is bind-mounted into the container in full,
      so a container-resident broker could not keep a secret from the agent.
    - ntfy and tailscale run on the host; `tailscale serve` can only publish a
      host-local listener.
    - Same house pattern as horus-music / horus-wakeup-drain: impure repo path
      in ExecStart, stdlib only, tuning needs a service restart, not a rebuild.

THE ONE RULE THAT MUST NEVER BE BROKEN
    **Never send a bare reject.** Every single reject this file posts carries a
    non-empty `message`. That is not politeness, it is the honesty mechanism:

      - a bare  {"reply":"reject"}                 -> PermissionRejectedError
      - a reject WITH a message                    -> PermissionCorrectedError
                                                      ("...with the following
                                                      feedback: <msg>")

      With experimental.continue_loop_on_deny (now set in ~/horus/opencode.json)
      BOTH let the agent loop continue. The difference is what the model does
      next. Measured during the A3C-167 spike, three independent times: given a
      denial with no explanation, qwen3.6 **fabricates the result** — it invented
      a plausible `curl 8.14.1` version banner when the real container curl is
      8.20.0. So continue_loop_on_deny does not "fix" a bare denial; it converts
      a loud death into a silent confabulation, which is strictly worse.

      Every DENY_* message below therefore says three things, in this order:
      it did not run / do not retry it / do not invent its output. They land
      verbatim in the model's context and are the cheapest steering lever in
      the system. Keep them that way.

    The one denial the broker CANNOT attach a message to is a cascade victim:
    rejecting one pending request makes the server bare-reject every other
    pending request in the same session within ~2s, and it does that itself.
    That case is covered by continue_loop_on_deny plus the "A blocked tool call
    did NOT run" rule in ~/horus/AGENTS.md, and by nothing else.

SECURITY POSTURE — read before adding anything
    The `X-Horus-Broker` token lives in ~/horus/.secrets/broker-local, which is
    bind-mounted into the container and readable by the agent. It is anti-
    accident hygiene, NOT a security boundary. What actually stops the agent
    approving its own requests is the container hardening: its `bash` tool runs
    under horus-netless-shell in an empty network namespace (no loopback, no
    localhost, verified rc=7 on curl), and opencode.json / .opencode/ /
    AGENTS.md / bridge/server.js / .git/hooks / .git/config are read-only
    inside the container, so it cannot add a tool or edit its own rules either.
    The one component with both network and in-process execution is
    .opencode/tools/web_fetch.ts, and it is GET-only with no caller control of
    method or headers, so it cannot POST a reply.

    >>> STANDING RULE: no future tool may accept a caller-supplied URL together
    >>> with a caller-supplied HTTP method (or headers, or body). That single
    >>> combination re-opens self-approval and every other loopback service on
    >>> this host at once.

    The orphaned-reply detector below is the backstop: any permission.replied
    for a registered session that this broker did not cause, and that is not
    explainable as a cascade, raises horus-alert.

PHASE 2 SCOPE
    Nothing is migrated. bridge/server.js and ask-horus.sh still use
    `opencode run` and never talk to this broker. It is exercised by hand (see
    the curl recipes at the bottom of this file) and by the messages.jsonl
    tailer, which is a temporary stand-in for the inbound vote route that
    phase 3 will add to the bridge.
"""

import base64
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

# --------------------------------------------------------------------------
# Configuration. Everything is env-overridable so the whole broker can be run
# against a scratch server on another port without touching the unit file.
# --------------------------------------------------------------------------

HOME = os.path.expanduser("~")

BIND_HOST = os.environ.get("HORUS_BROKER_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("HORUS_BROKER_PORT", "8790"))

OPENCODE_URL = os.environ.get("HORUS_OPENCODE_URL", "http://127.0.0.1:4096").rstrip("/")
OPENCODE_USER = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
OPENCODE_ENV_FILE = os.environ.get(
    "HORUS_OPENCODE_ENV", os.path.join(HOME, "horus/.secrets/opencode-server.env")
)

BROKER_TOKEN_FILE = os.environ.get(
    "HORUS_BROKER_TOKEN_FILE", os.path.join(HOME, "horus/.secrets/broker-local")
)

BRIDGE_URL = os.environ.get("HORUS_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
CONTACTS_FILE = os.environ.get(
    "HORUS_CONTACTS", os.path.join(HOME, "horus/memory/whatsapp-contacts.json")
)
WA_LOG = os.environ.get("HORUS_WA_LOG", os.path.join(HOME, "horus/bridge/messages.jsonl"))
WA_TAIL = os.environ.get("HORUS_BROKER_WA_TAIL", "1") not in ("0", "false", "no")

STATE_FILE = os.environ.get(
    "HORUS_BROKER_STATE", os.path.join(HOME, ".local/state/horus/approvals.json")
)

DEFAULT_TTL_SEC = int(os.environ.get("HORUS_BROKER_TTL", "600"))
MAX_CONCURRENT = int(os.environ.get("HORUS_BROKER_MAX_CONCURRENT", "8"))
MAX_PER_HOUR = int(os.environ.get("HORUS_BROKER_MAX_PER_HOUR", "20"))

# A code stays reserved after its request is resolved, so a late "cy" gets
# "that one already timed out at 14:32" instead of voting on a fresh request
# that happened to recycle the letter.
QUARANTINE_SEC = int(os.environ.get("HORUS_BROKER_QUARANTINE", "300"))
# A vote older than this is ignored: WhatsApp replays offline messages hours
# later and a resurrected "cy" must never approve something.
VOTE_MAX_AGE_SEC = int(os.environ.get("HORUS_BROKER_VOTE_MAX_AGE", "1800"))
# "We rejected another request in this same session milliseconds ago" is the
# ONLY discriminator between a cascade victim and someone else answering.
CASCADE_WINDOW_SEC = float(os.environ.get("HORUS_BROKER_CASCADE_WINDOW", "10"))
# Measured SSE heartbeat is ~10.6s, so 30s of silence really is a dead stream.
SSE_READ_TIMEOUT = int(os.environ.get("HORUS_BROKER_SSE_TIMEOUT", "30"))
SSE_DOWN_ALERT_SEC = int(os.environ.get("HORUS_BROKER_SSE_DOWN_ALERT", "60"))
RENOTIFY_AFTER_SEC = int(os.environ.get("HORUS_BROKER_RENOTIFY", "120"))
# How often to check that the runs behind our pending approvals are still alive.
# One GET /session/status; the abort path leaves zombies that emit no event.
ZOMBIE_SWEEP_SEC = int(os.environ.get("HORUS_BROKER_ZOMBIE_SWEEP", "30"))
SESSION_MAX_AGE_SEC = int(os.environ.get("HORUS_BROKER_SESSION_MAX_AGE", str(24 * 3600)))

# Consonants only (can't spell a word), minus y/n/a (the verdict letters),
# minus l/i/o (1/I/0 confusion), minus b d h j k m s t (because "by my da ha ja
# ka sa ta" are real English/German words that would collide with <code>a).
# 10 codes with MAX_CONCURRENT=8 leaves slack so a freshly freed code always
# gets to sit out its quarantine.
ALPHABET = "cfgpqrvwxz"
# Belt and braces for future alphabet edits: nothing here may ever be a token.
BLOCKED_TOKENS = {
    "ya", "ja", "na", "no", "an", "am", "as", "at", "by", "do", "go", "he",
    "if", "in", "is", "it", "me", "my", "of", "on", "or", "so", "to", "up",
    "us", "we", "da", "du", "ei", "er", "es", "im", "ob", "um", "wo", "zu",
}

ALERTS_ENABLED = os.environ.get("HORUS_BROKER_ALERTS", "1") not in ("0", "false", "no")

VERSION = "phase2"

# --------------------------------------------------------------------------
# The deny vocabulary. Read the module docstring before touching any of these.
# Structure of every one: (1) what happened, in Kurt's voice, (2) do not retry,
# (3) do not invent the output, (4) tell him what you skipped.
# --------------------------------------------------------------------------

_NO_FABRICATE = (
    "The command did not run, so there is no output from it — do not describe, "
    "quote or guess what it would have printed."
)

DENY_VOTE = (
    "Kurt saw this request and denied it. Do not retry it and do not try a "
    "variation of it. " + _NO_FABRICATE + " Continue with whatever you can do "
    "without it, and say clearly at the end what you skipped and why."
)

DENY_EXPIRY_TMPL = (
    "No answer from Kurt within {mins} minutes (the remote approval request "
    "timed out — treat that as a NO, it is a decision and not a glitch). Do "
    "not retry this action and do not ask again. " + _NO_FABRICATE + " "
    "Continue with whatever you can do without it, and say clearly at the end "
    "what you skipped and why."
)

DENY_RATELIMIT = (
    "Too many approval requests are already queued, so this one was declined "
    "automatically without ever reaching Kurt. Do not retry it. " + _NO_FABRICATE
    + " Stop asking for permissions in this run, finish with what you already "
    "have, and tell Kurt at the end that you ran out of approval budget."
)

DENY_CANCEL = (
    "Kurt cancelled this run from the CLI (`horus cancel`) while this action "
    "was waiting for his approval. Stop here. Do not retry it. " + _NO_FABRICATE
)

DENY_SESSION_END = (
    "This run was ended before Kurt answered the approval request, so the "
    "action was declined. Do not retry it. " + _NO_FABRICATE
)

DENY_ZOMBIE = (
    "This approval request outlived the run that made it and was cleaned up. "
    "Do not retry it. " + _NO_FABRICATE
)


def now() -> float:
    return time.time()


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def log(level: str, msg: str) -> None:
    print(f"{ts()} {level:<5} {msg}", flush=True)


def hhmm(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


# --------------------------------------------------------------------------
# opencode server client
# --------------------------------------------------------------------------


class Opencode:
    """Thin client for the container's `opencode serve`.

    NOTE on `directory`: every route accepts an optional ?directory=. It never
    400s, but a WRONG value is accepted silently and resolves a DIFFERENT
    project instance (?directory=/tmp answered with worktree:"/" and HTTP 200
    during the spike). The server's WorkingDirectory is /home/horus/work, which
    is what we want, so we omit the parameter everywhere. Do not "helpfully"
    add a computed path here.
    """

    def __init__(self, base: str, user: str, env_file: str):
        self.base = base
        self.user = user
        self.env_file = env_file
        self._password = os.environ.get("OPENCODE_SERVER_PASSWORD") or ""
        self._pw_warned = False

    def password(self) -> str:
        # Re-read lazily: the broker may well start before Kurt has created the
        # secret, and we want it to pick the file up without a restart.
        if self._password:
            return self._password
        try:
            with open(self.env_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("OPENCODE_SERVER_PASSWORD="):
                        val = line.split("=", 1)[1].strip().strip("'\"")
                        if val:
                            self._password = val
                            log("info", f"loaded server password from {self.env_file}")
                        break
        except OSError as exc:
            if not self._pw_warned:
                log("error", f"cannot read {self.env_file}: {exc} — the server will 401")
                self._pw_warned = True
        return self._password

    def _auth_header(self) -> str:
        raw = f"{self.user}:{self.password()}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def request(self, method: str, path: str, body=None, timeout: int = 15):
        """-> (status, parsed_or_text). Never raises for HTTP status."""
        url = self.base + path
        data = None
        headers = {"Authorization": self._auth_header(), "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, _maybe_json(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            return exc.code, _maybe_json(raw)
        except Exception as exc:  # URLError, socket timeout, ...
            return 0, {"error": str(exc)}

    def healthy(self) -> bool:
        status, _ = self.request("GET", "/global/health", timeout=5)
        return status == 200

    def permissions(self):
        status, body = self.request("GET", "/permission", timeout=10)
        return body if status == 200 and isinstance(body, list) else None

    def session_status(self):
        status, body = self.request("GET", "/session/status", timeout=10)
        return body if status == 200 and isinstance(body, dict) else None

    def reply(self, request_id: str, decision: str, message=None):
        """-> ('ok' | 'gone' | 'error', detail).

        `gone` means the server no longer knows the request (someone answered
        it, or it cascaded). That is a normal, idempotent outcome, not a fault.
        """
        body = {"reply": decision}
        if decision == "reject":
            # The invariant. If this ever fires we would rather crash than post
            # a bare reject and have the model invent a result.
            assert message, "reject without a message — see the module docstring"
        if message:
            body["message"] = message
        status, resp = self.request("POST", f"/permission/{request_id}/reply", body)
        if status == 200:
            return "ok", resp
        if status == 404 or (isinstance(resp, dict) and resp.get("_tag") == "PermissionNotFoundError"):
            return "gone", resp
        return "error", f"HTTP {status}: {resp}"


def _maybe_json(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return raw


# --------------------------------------------------------------------------
# Notifiers
# --------------------------------------------------------------------------


class Notifier:
    """One outbound channel.

    Phase 5 drops NtfyNotifier in beside WhatsAppNotifier with no change to the
    broker, so keep this contract stable:

      available()  cheap liveness probe; an unavailable channel is skipped and
                   the request still goes out over the others.
      notify()     returns an opaque handle dict (or None) that is persisted
                   with the request and handed back to cancel(). ntfy will put
                   the published message id in there so a resolved request can
                   be cleared from the phone's shade with X-Delete.
      cancel()     the request went away; withdraw/annotate the notification.
                   `text` is None when the broker wants silence (e.g. the
                   cascade case, where one summary beats five retractions).
      ack()        confirm a vote on the same channel it arrived on. Every
                   consumed vote gets one — it is the entire mitigation for a
                   misparse, because Kurt sees within seconds that his message
                   was eaten and can just re-send.

    A notifier that needs per-decision credentials (ntfy's action buttons carry
    a single-use HMAC token per button) gets them from the broker reference
    passed to the constructor, so this signature does not have to grow.
    """

    name = "base"

    def __init__(self, broker):
        self.broker = broker

    def available(self) -> bool:
        return True

    def notify(self, req, sess):
        raise NotImplementedError

    def cancel(self, req, sess, handle, text):
        pass

    def ack(self, sess, text, req=None):
        pass


class WhatsAppNotifier(Notifier):
    """Outbound over the bridge's existing localhost POST /send.

    Deliberately requires NO change to bridge/server.js: /send already exists,
    already enforces the contact allowlist, and is already used by horus-alert.
    """

    name = "whatsapp"

    def _post(self, jid: str, text: str) -> bool:
        body = json.dumps({"to": jid, "text": text}).encode()
        req = urllib.request.Request(
            BRIDGE_URL + "/send",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status == 200
        except Exception as exc:
            log("error", f"whatsapp send failed: {exc}")
            return False

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(BRIDGE_URL + "/status", timeout=5) as resp:
                return json.loads(resp.read()).get("status") == "connected"
        except Exception:
            return False

    def notify(self, req, sess):
        jid = sess.get("jid")
        if not jid:
            return None
        ok = self._post(jid, self.broker.notification_text(req, sess))
        return {"sentAt": now(), "ok": ok} if ok else None

    def cancel(self, req, sess, handle, text):
        if text and sess.get("jid"):
            self._post(sess["jid"], text)

    def ack(self, sess, text, req=None):
        if sess.get("jid"):
            self._post(sess["jid"], text)


class DesktopNotifier(Notifier):
    """Last-resort channel that does not depend on WhatsApp.

    Same reasoning as wa-watch.nix: when WhatsApp is the broken thing, the
    desktop is the only path left. Answering still happens elsewhere; this only
    tells Kurt to go look.
    """

    name = "desktop"

    def available(self) -> bool:
        return shutil.which("notify-send") is not None

    def _send(self, title, body, urgency="normal"):
        try:
            subprocess.run(
                ["notify-send", "-u", urgency, title, body],
                timeout=10,
                check=False,
            )
        except Exception as exc:
            log("error", f"notify-send failed: {exc}")

    def notify(self, req, sess):
        self._send(
            f"Horus approval [{req['code']}]",
            f"{req['permission']}: {self.broker.describe(req)}\nReply {req['code']}y / {req['code']}n on WhatsApp",
            "critical",
        )
        return {"sentAt": now()}

    def cancel(self, req, sess, handle, text):
        if text:
            self._send(f"Horus approval [{req['code']}]", text)

    def ack(self, sess, text, req=None):
        self._send("Horus approval", text)


class LogNotifier(Notifier):
    """Journal-only channel. Exists so the whole state machine can be exercised
    end to end without sending Kurt a single WhatsApp — every phase-2 test
    below registers its scratch session with channels:["log"]."""

    name = "log"

    def notify(self, req, sess):
        log("info", f"NOTIFY[log] {self.broker.notification_text(req, sess)!r}")
        return {"sentAt": now()}

    def cancel(self, req, sess, handle, text):
        if text:
            log("info", f"CANCEL[log] [{req['code']}] {text!r}")

    def ack(self, sess, text, req=None):
        log("info", f"ACK[log] {text!r}")


class NtfyNotifier(Notifier):
    """PHASE 5 — deliberately not implemented, deliberately still registered.

    Registered-but-unavailable means a session that asks for channels
    ["ntfy","whatsapp"] today degrades to WhatsApp instead of erroring, so the
    call sites can be written against the final channel list now.

    When it lands it must:
      - publish to the topic with Title "Horus approval [c]", Priority high,
        Tags lock, Cache no, Firebase no, body = describe(req) on ONE line of
        <= ~40 chars (Instinct 3 Solar is 176x176 MIP);
      - carry exactly three http actions (ntfy's hard maximum) — Yes / Always /
        No — each POSTing to the broker's /approve with its own single-use
        token in an Authorization header, and clear=true;
      - return {"id": <ntfy message id>} as its handle, so cancel() can send
        X-Delete for that id and a resolved approval does not sit in the shade;
      - never be given a token that authorises more than one decision on one
        request: the signed payload is {r: requestID, d: decision, n: nonce,
        e: expiry}.
    Kurt's Garmin test passed (both action buttons mirrored to the Instinct 3
    with the phone locked, and selecting one really did fire the POST), so this
    becomes the primary channel and WhatsApp the fallback.
    """

    name = "ntfy"

    def available(self) -> bool:
        return False

    def notify(self, req, sess):
        return None


# --------------------------------------------------------------------------
# Broker
# --------------------------------------------------------------------------


class Broker:
    def __init__(self):
        self.lock = threading.RLock()
        self.opencode = Opencode(OPENCODE_URL, OPENCODE_USER, OPENCODE_ENV_FILE)
        self.notifiers = {}
        for cls in (WhatsAppNotifier, DesktopNotifier, LogNotifier, NtfyNotifier):
            n = cls(self)
            self.notifiers[n.name] = n

        self.sessions = {}     # sessionID -> registration
        self.pending = {}      # requestID -> request record
        self.quarantine = {}   # code -> {reqID, outcome, summary, at, until}
        self.notify_times = deque()   # epoch of each notified request (rate limit)
        self.our_replies = deque()    # (sessionID, requestID, epoch) — cascade discriminator
        self.recent_reply_ids = {}    # requestID -> epoch, suppress our own replied event

        self.sse_connected = False
        self.sse_last_event = 0.0
        self.sse_down_since = now()
        self.stop = threading.Event()
        self._alerts = {}

        self.load()

    # ---------------- persistence ----------------

    def load(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:
            log("error", f"state file unreadable ({exc}) — starting empty")
            return
        self.sessions = data.get("sessions", {}) or {}
        self.pending = data.get("pending", {}) or {}
        self.quarantine = data.get("quarantine", {}) or {}
        log(
            "info",
            f"restored {len(self.sessions)} session(s), {len(self.pending)} pending, "
            f"{len(self.quarantine)} quarantined code(s)",
        )

    def save(self):
        data = {
            "version": VERSION,
            "savedAt": now(),
            "sessions": self.sessions,
            "pending": self.pending,
            "quarantine": self.quarantine,
        }
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, STATE_FILE)
        except Exception as exc:
            log("error", f"state save failed: {exc}")

    # ---------------- alerting ----------------

    def alert(self, key: str, text: str):
        """Loud, deduplicated failure notification. 10 minutes per key so a
        flapping condition cannot turn into the 16-notifications-at-boot mess
        that music.nix documents."""
        log("ERROR", text)
        if not ALERTS_ENABLED:
            # HORUS_BROKER_ALERTS=0 — for hand-testing the failure paths without
            # WhatsApping Kurt "horus-approval-broker failed". Never set it on
            # the real unit: the orphan detector and the dead-SSE detector are
            # the only warning Kurt gets that approvals have stopped working.
            log("warn", "(alert suppressed: HORUS_BROKER_ALERTS=0)")
            return
        with self.lock:
            last = self._alerts.get(key, 0)
            if now() - last < 600:
                return
            self._alerts[key] = now()
        try:
            subprocess.run(
                ["systemctl", "--user", "start", "horus-alert@horus-approval-broker.service"],
                timeout=15,
                check=False,
            )
        except Exception as exc:
            log("error", f"could not fire horus-alert: {exc}")
        desktop = self.notifiers.get("desktop")
        if desktop and desktop.available():
            desktop._send("Horus approvals", text, "critical")

    # ---------------- presentation ----------------

    def describe(self, req) -> str:
        """One short human line for the thing being asked about. Kurt may be
        reading this on a watch, so 60 chars is the budget."""
        md = req.get("metadata") or {}
        for key in ("command", "cmd", "filePath", "path", "file", "url", "directory", "pattern"):
            val = md.get(key)
            if isinstance(val, str) and val.strip():
                text = val.strip()
                break
        else:
            pats = req.get("patterns") or []
            text = str(pats[0]) if pats else req.get("permission", "?")
        text = " ".join(text.split())
        return text[:60] + ("…" if len(text) > 60 else "")

    def notification_text(self, req, sess) -> str:
        code = req["code"]
        mins = max(1, int(round((req["expiresAt"] - now()) / 60)))
        label = sess.get("label") or ""
        head = f"Approval [{code}] — {req.get('permission', '?')}"
        if label:
            head += f" ({label})"
        return (
            f"{head}\n"
            f"{self.describe(req)}\n"
            f"Reply {code}y = yes, {code}n = no. Auto-deny in {mins} min."
        )

    # ---------------- codes ----------------

    def _free_code(self):
        used = {r["code"] for r in self.pending.values()} | set(self.quarantine)
        for ch in ALPHABET:
            if ch not in used:
                return ch
        return None

    def _quarantine(self, req, outcome: str):
        if req["code"] not in ALPHABET:
            return  # auto-declined requests never had a code to protect
        self.quarantine[req["code"]] = {
            "code": req["code"],
            "reqID": req["id"],
            "sessionID": req["sessionID"],
            "outcome": outcome,
            "summary": self.describe(req),
            "at": now(),
            "until": now() + QUARANTINE_SEC,
        }

    # ---------------- channels ----------------

    def _channels(self, sess):
        out = []
        for name in sess.get("channels") or ["whatsapp"]:
            n = self.notifiers.get(name)
            if not n:
                log("warn", f"session {sess['sessionID'][:12]} asked for unknown channel {name!r}")
                continue
            if not n.available():
                log("warn", f"channel {name} unavailable — skipping")
                continue
            out.append(n)
        return out

    def _fanout_notify(self, req, sess):
        handles = {}
        for n in self._channels(sess):
            try:
                h = n.notify(req, sess)
                if h is not None:
                    handles[n.name] = h
            except Exception as exc:
                log("error", f"notifier {n.name} raised: {exc}")
        req["handles"] = handles
        req["lastNotifyAt"] = now()
        req["notifyCount"] = req.get("notifyCount", 0) + 1
        if not handles:
            self.alert(
                "no-channel",
                f"approval [{req['code']}] {req.get('permission')} could not be delivered on ANY "
                f"channel ({sess.get('channels')}) — it will auto-deny at "
                f"{hhmm(req['expiresAt'])}",
            )
        return handles

    def _fanout_cancel(self, req, sess, text):
        for name, handle in (req.get("handles") or {}).items():
            n = self.notifiers.get(name)
            if not n:
                continue
            try:
                n.cancel(req, sess, handle, text)
            except Exception as exc:
                log("error", f"notifier {name} cancel raised: {exc}")

    def _fanout_ack(self, sess, text, req=None):
        for n in self._channels(sess):
            try:
                n.ack(sess, text, req)
            except Exception as exc:
                log("error", f"notifier {n.name} ack raised: {exc}")

    # ---------------- registration ----------------

    def register(self, sid, channels, jid, label, ttl):
        with self.lock:
            self.sessions[sid] = {
                "sessionID": sid,
                "channels": channels,
                "jid": jid,
                "label": label,
                "ttlSec": ttl,
                "registeredAt": now(),
            }
            self.save()
        log("info", f"registered session {sid} channels={channels} ttl={ttl}s label={label!r}")
        # A prompt may already be blocked on an ask by the time registration
        # lands (the caller POSTs the prompt first if it is impatient), so pick
        # up anything already pending for this session right now.
        self.reconcile(only_session=sid)

    def unregister(self, sid, reason_message=None, cancel_text=None):
        with self.lock:
            sess = self.sessions.pop(sid, None)
            reqs = [r for r in self.pending.values() if r["sessionID"] == sid]
            self.save()
        n = 0
        for req in reqs:
            if self.decide(req["id"], "reject", "session-end", reason_message or DENY_SESSION_END,
                           cancel_text=cancel_text):
                n += 1
        if sess or n:
            log("info", f"unregistered session {sid} — rejected {n} pending approval(s)")
        return sess, n

    # ---------------- the state machine ----------------

    def on_asked(self, props):
        rid = props.get("id")
        sid = props.get("sessionID")
        if not rid or not sid:
            return
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                # THE gate that keeps the broker from WhatsApping Kurt while he
                # is sitting at `horus chat`: only sessions someone explicitly
                # registered are brokered. Everything else is somebody else's
                # problem, including an unattached TUI (whose permissions are
                # per-process and never show up here at all).
                log("debug", f"ask {rid[:16]} for unregistered session {sid[:16]} — ignoring")
                return
            if rid in self.pending:
                return
            # rate limits
            cutoff = now() - 3600
            while self.notify_times and self.notify_times[0] < cutoff:
                self.notify_times.popleft()
            too_many = len(self.pending) >= MAX_CONCURRENT
            too_fast = len(self.notify_times) >= MAX_PER_HOUR
            code = None if (too_many or too_fast) else self._free_code()
            if code is None:
                reason = "concurrency" if too_many else ("rate" if too_fast else "no free code")
                log("warn", f"auto-declining {rid[:16]} ({reason}) without notifying Kurt")
                req = self._make_req(props, sess, code="-")
                self.pending[rid] = req
                self.save()
                declined = True
            else:
                req = self._make_req(props, sess, code=code)
                self.pending[rid] = req
                self.notify_times.append(now())
                self.save()
                declined = False
        if declined:
            self.decide(rid, "reject", "rate-limit", DENY_RATELIMIT, cancel_text=None)
            self.alert(
                "rate-limit",
                f"approval flood: auto-declined a {props.get('permission')} request from "
                f"session {sid[:16]} without asking (>= {MAX_CONCURRENT} pending or "
                f"{MAX_PER_HOUR}/h)",
            )
            return
        log(
            "info",
            f"ASK [{req['code']}] {req.get('permission')} {self.describe(req)!r} "
            f"session={sid[:16]} expires {hhmm(req['expiresAt'])}",
        )
        self._fanout_notify(req, sess)
        self.save()

    def _make_req(self, props, sess, code):
        ttl = sess.get("ttlSec") or DEFAULT_TTL_SEC
        return {
            "id": props.get("id"),
            "code": code,
            "sessionID": props.get("sessionID"),
            "permission": props.get("permission"),
            "patterns": props.get("patterns") or [],
            "metadata": props.get("metadata") or {},
            "always": props.get("always") or [],
            "tool": props.get("tool") or {},
            "createdAt": now(),
            "expiresAt": now() + ttl,
            "state": "pending",
            "handles": {},
            "lastNotifyAt": 0,
            "notifyCount": 0,
        }

    def claim(self, rid):
        """Take exclusive ownership of a request before posting a reply, so the
        expiry ticker and an incoming vote can never both answer it."""
        with self.lock:
            req = self.pending.get(rid)
            if not req or req["state"] != "pending":
                return None
            req["state"] = "deciding"
            return req

    def decide(self, rid, decision, by, message=None, cancel_text=None):
        """Post a verdict to the server. Returns True if we actually replied."""
        req = self.claim(rid)
        if req is None:
            return False
        outcome, detail = self.opencode.reply(req["id"], decision, message)
        with self.lock:
            if outcome != "error":
                # Only a reply that actually reached the server may act as the
                # cascade discriminator, or a failed POST would make a genuine
                # foreign answer look like a cascade and silence the alarm.
                self.our_replies.append((req["sessionID"], req["id"], now()))
                self.recent_reply_ids[req["id"]] = now()
            req["decision"] = decision
            req["decidedBy"] = by
            req["decidedAt"] = now()
            sess = self.sessions.get(req["sessionID"], {"sessionID": req["sessionID"]})
            if outcome == "error":
                # Put it back: a transient 500/connection error must not silently
                # lose an approval that is still blocking a run.
                req["state"] = "pending"
                log("error", f"reply for [{req['code']}] failed: {detail} — leaving it pending")
                return False
            if outcome == "gone":
                log("info", f"[{req['code']}] was already resolved server-side; treating as done")
                req["state"] = "resolved-elsewhere"
            else:
                req["state"] = "resolved"
            self.pending.pop(req["id"], None)
            self._quarantine(req, decision if outcome == "ok" else "already-resolved")
            self.save()
        log("info", f"DECIDE [{req['code']}] {decision} by={by} ({outcome})")
        if cancel_text is not None:
            self._fanout_cancel(req, sess, cancel_text)
        return outcome == "ok"

    def on_replied(self, props):
        rid = props.get("requestID")
        sid = props.get("sessionID")
        reply = props.get("reply")
        with self.lock:
            mine = self.recent_reply_ids.pop(rid, None)
            req = self.pending.get(rid)
            if mine or not req:
                # Ours (the event racing our own POST), or for a request we
                # never tracked. Either way nothing to do.
                return
            # Someone/something else answered a request we were brokering. The
            # ONLY thing that distinguishes a cascade victim from a genuine
            # foreign answer is that we rejected a sibling in the same session
            # moments ago — the replied event carries no message and no cascade
            # marker, so there is nothing else to key on.
            cutoff = now() - CASCADE_WINDOW_SEC
            while self.our_replies and self.our_replies[0][2] < cutoff:
                self.our_replies.popleft()
            cascade = any(s == sid for (s, _r, _t) in self.our_replies)
            req["state"] = "cascaded" if cascade else "orphaned"
            req["decision"] = reply
            req["decidedBy"] = "cascade" if cascade else "unknown"
            req["decidedAt"] = now()
            sess = self.sessions.get(sid, {"sessionID": sid})
            self.pending.pop(rid, None)
            self._quarantine(req, "cascade" if cascade else "answered-elsewhere")
            self.save()
        if cascade:
            # Expected and normal: rejecting one pending request makes the
            # server bare-reject every other pending request in that session.
            # Do NOT alert, and do NOT send a retraction per victim — one line
            # in the ack of the vote that caused it is enough.
            log("info", f"[{req['code']}] cascade-rejected with its sibling (server-side, bare)")
            self._fanout_cancel(req, sess, None)
        else:
            self.alert(
                "orphan",
                f"approval [{req['code']}] ({req.get('permission')} {self.describe(req)}) was "
                f"answered '{reply}' by something that is not this broker, on brokered session "
                f"{sid[:16]}. Nothing legitimate answers an unattended run. Check "
                f"`journalctl -M horus -u opencode-server` and who can reach :4096.",
            )
            self._fanout_cancel(
                req, sess, f"[{req['code']}] was answered ({reply}) by something other than you — check the host."
            )

    def on_session_deleted(self, props):
        sid = props.get("info", {}).get("id") if isinstance(props.get("info"), dict) else props.get("sessionID")
        if sid and sid in self.sessions:
            log("info", f"session {sid[:16]} deleted server-side")
            self.unregister(sid, DENY_SESSION_END)

    # ---------------- voting ----------------

    def _contacts(self):
        try:
            with open(CONTACTS_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def kurt_jids(self):
        # ONLY kurt* keys. iz and every other allowlisted contact can never vote,
        # here and (from phase 3) in the bridge as well.
        return {str(v) for k, v in self._contacts().items() if str(k).startswith("kurt")}

    def default_jid(self):
        """Where an approval goes when the caller did not name a recipient.

        The exact "kurt" key, NOT just any kurt* one: whatsapp-contacts.json also
        holds "kurt-phone" (his own number, a different chat), and sorting the
        values would silently pick that one.
        """
        contacts = self._contacts()
        if contacts.get("kurt"):
            return str(contacts["kurt"])
        jids = self.kurt_jids()
        return sorted(jids)[0] if jids else None

    def parse_vote(self, text):
        """-> (code_or_None, decision) or None. Case-insensitive, punctuation
        and whitespace stripped, <= 4 characters."""
        if not isinstance(text, str):
            return None
        t = re.sub(r"[^a-z0-9]", "", text.strip().lower())
        if not t or len(t) > 4:
            return None
        # Bare verdicts first: these ARE words, deliberately, and are only ever
        # honoured when exactly one request is pending (checked by the caller).
        if t in ("y", "yes", "j", "ja"):
            return (None, "once")
        if t in ("n", "no", "nein"):
            return (None, "reject")
        if len(t) >= 2 and t[0] in ALPHABET:
            # Guard for the code-prefixed tokens only. The current alphabet
            # produces nothing that is a word in English or German; this stops a
            # future alphabet edit from quietly reintroducing one (e.g. adding
            # 'b' would make "by" a live approve token).
            if t in BLOCKED_TOKENS:
                log("warn", f"refusing to treat {t!r} as a vote — it is a real word")
                return None
            rest = t[1:]
            if rest == "y":
                return (t[0], "once")
            if rest == "n":
                return (t[0], "reject")
            if rest == "ya":
                return (t[0], "always")
        return None

    def handle_vote(self, sender, text, when=None):
        """-> {consumed, ack, reason}. The bridge pre-filters on sender and
        length only to save a round trip; the grammar lives HERE because only
        the broker knows which codes are live."""
        jids = self.kurt_jids()
        if jids and sender not in jids:
            return {"consumed": False, "reason": "not-kurt"}
        if when is not None:
            age = now() - when
            if age > VOTE_MAX_AGE_SEC:
                # WhatsApp replays offline messages hours later; a resurrected
                # "cy" must never approve anything.
                return {"consumed": False, "reason": "stale"}
        parsed = self.parse_vote(text)
        if not parsed:
            return {"consumed": False, "reason": "not-a-vote"}
        code, decision = parsed
        with self.lock:
            if code is None:
                live = [r for r in self.pending.values() if r["state"] == "pending"]
                if len(live) != 1:
                    # A bare yes/no is only unambiguous with exactly one pending.
                    return {"consumed": False, "reason": f"bare-vote-with-{len(live)}-pending"}
                req = live[0]
            else:
                req = next(
                    (r for r in self.pending.values() if r["code"] == code and r["state"] == "pending"),
                    None,
                )
                if req is None:
                    q = self.quarantine.get(code)
                    if q:
                        # Idempotent by construction: a late or duplicate vote
                        # lands here and gets told what actually happened.
                        return {
                            "consumed": True,
                            "reason": "already-resolved",
                            "ack": f"[{code}] was already {_outcome_word(q['outcome'])} at "
                                   f"{hhmm(q['at'])} — {q['summary']}",
                        }
                    # Unknown code: do NOT consume. A false positive here eats a
                    # real message; letting it through only means the agent sees
                    # a stray "cy". Cheaper mistake.
                    return {"consumed": False, "reason": "unknown-code"}
            rid = req["id"]
            summary = self.describe(req)
            shown = req["code"]
            sess = self.sessions.get(req["sessionID"], {"sessionID": req["sessionID"]})
            siblings = [
                r["code"] for r in self.pending.values()
                if r["sessionID"] == req["sessionID"] and r["id"] != rid and r["state"] == "pending"
            ]
        message = DENY_VOTE if decision == "reject" else None
        ok = self.decide(rid, decision, f"kurt:{sender}", message, cancel_text=None)
        if not ok:
            with self.lock:
                q = self.quarantine.get(shown)
            if q:
                return {
                    "consumed": True,
                    "reason": "already-resolved",
                    "ack": f"[{shown}] was already {_outcome_word(q['outcome'])} — {summary}",
                }
            return {
                "consumed": True,
                "reason": "reply-failed",
                "ack": f"couldn't reach the opencode server to record [{shown}] — try again",
            }
        word = {"once": "approved", "always": "approved (and future matching calls this run)",
                "reject": "denied"}[decision]
        ack = f"ok — {word} [{shown}] {req.get('permission')}: {summary}"
        if decision == "reject" and siblings:
            # Honest about the cascade rather than letting Kurt discover that
            # denying one thing silently denied four others.
            ack += f"\n(that also cancelled {len(siblings)} other pending request(s) in the same run: " \
                   + ", ".join(f"[{c}]" for c in siblings) + ")"
        return {"consumed": True, "reason": "voted", "ack": ack, "code": shown, "decision": decision,
                "sessionID": req["sessionID"]}

    # ---------------- expiry / housekeeping ----------------

    def tick(self):
        with self.lock:
            due = [
                r for r in self.pending.values()
                if r["state"] == "pending" and now() >= r["expiresAt"]
            ]
            for code in [c for c, q in self.quarantine.items() if now() >= q["until"]]:
                del self.quarantine[code]
            # bounded bookkeeping: both of these are only meaningful for a few
            # seconds after a reply, and this process runs for weeks
            cutoff = now() - CASCADE_WINDOW_SEC
            while self.our_replies and self.our_replies[0][2] < cutoff:
                self.our_replies.popleft()
            for rid in [k for k, t in self.recent_reply_ids.items() if now() - t > 120]:
                del self.recent_reply_ids[rid]
            stale = [
                sid for sid, s in self.sessions.items()
                if now() - s.get("registeredAt", 0) > SESSION_MAX_AGE_SEC
                and not any(r["sessionID"] == sid for r in self.pending.values())
            ]
            for sid in stale:
                del self.sessions[sid]
                log("info", f"pruned stale session registration {sid[:16]}")
            if stale:
                self.save()
        for req in due:
            mins = max(1, int(round((req["expiresAt"] - req["createdAt"]) / 60)))
            note = (
                f"[{req['code']}] timed out with no answer — auto-denied: "
                f"{req.get('permission')}: {self.describe(req)}"
            )
            # cancel_text is the "so Kurt is not surprised" note; the model gets
            # the far more detailed DENY_EXPIRY_TMPL.
            self.decide(
                req["id"], "reject", "expiry",
                DENY_EXPIRY_TMPL.format(mins=mins),
                cancel_text=note,
            )
            log("info", f"EXPIRED [{req['code']}] after {mins} min")

    # ---------------- reconciliation ----------------

    def reconcile(self, only_session=None):
        """Re-sync with the server. Runs at startup and after every SSE
        reconnect — those are exactly the windows in which a permission can be
        asked or answered without us seeing the event."""
        perms = self.opencode.permissions()
        if perms is None:
            log("warn", "reconcile: cannot read GET /permission (server down?)")
            return
        statuses = self.opencode.session_status() or {}
        server = {p["id"]: p for p in perms if isinstance(p, dict) and p.get("id")}

        with self.lock:
            tracked = dict(self.pending)
            sessions = dict(self.sessions)

        # 1. tracked but gone from the server -> resolved while we were away.
        for rid, req in tracked.items():
            if only_session and req["sessionID"] != only_session:
                continue
            if rid in server:
                continue
            with self.lock:
                if rid in self.pending:
                    req["state"] = "resolved-elsewhere"
                    self.pending.pop(rid, None)
                    self._quarantine(req, "resolved-while-away")
                    self.save()
            log("info", f"reconcile: [{req['code']}] vanished server-side — dropping")

        # 2/3. on the server.
        for rid, p in server.items():
            sid = p.get("sessionID")
            if only_session and sid != only_session:
                continue
            sess = sessions.get(sid)
            if not sess:
                continue  # not ours; an interactive TUI or another tool owns it
            busy = (statuses.get(sid) or {}).get("type")
            with self.lock:
                known = self.pending.get(rid)
            if known:
                if known["state"] != "pending":
                    continue
                if busy != "busy":
                    # Tracked, but the run behind it is gone (aborted, crashed).
                    # Without this the broker would keep re-notifying Kurt about
                    # a dead run for the whole TTL. See the zombie note below.
                    log("warn", f"reconcile: [{known['code']}] belongs to a {busy or 'dead'} "
                                f"session — cleaning up silently")
                    self.decide(rid, "reject", "zombie", DENY_ZOMBIE, cancel_text=None)
                    continue
                if now() - known.get("lastNotifyAt", 0) > RENOTIFY_AFTER_SEC:
                    log("info", f"reconcile: re-notifying [{known['code']}] (still waiting)")
                    self._fanout_notify(known, sess)
                    self.save()
                continue
            if busy != "busy":
                # A pending permission on a session that is NOT busy is a zombie:
                # POST /session/{id}/abort does not clear pending permissions, so
                # they sit in GET /permission forever. Pinging Kurt about a dead
                # run is the exact false alarm this cross-check exists to stop.
                # We clean it up quietly, with a message, and never notify.
                log("warn", f"reconcile: zombie permission {rid[:16]} on {busy or 'unknown'} "
                            f"session {sid[:16]} — cleaning up silently")
                with self.lock:
                    req = self._make_req(p, sess, code="-")
                    self.pending[rid] = req
                self.decide(rid, "reject", "zombie", DENY_ZOMBIE, cancel_text=None)
                continue
            with self.lock:
                code = self._free_code()
                if code is None:
                    log("warn", f"reconcile: no free code for {rid[:16]}")
                    continue
                req = self._make_req(p, sess, code=code)
                self.pending[rid] = req
                self.save()
            log("info", f"reconcile: adopted [{code}] {req.get('permission')} {self.describe(req)!r}")
            self._fanout_notify(req, sess)
            self.save()

    # ---------------- SSE ----------------

    def sse_loop(self):
        backoff = 1
        while not self.stop.is_set():
            try:
                req = urllib.request.Request(
                    OPENCODE_URL + "/event",
                    headers={
                        "Authorization": self.opencode._auth_header(),
                        "Accept": "text/event-stream",
                    },
                )
                with urllib.request.urlopen(req, timeout=SSE_READ_TIMEOUT) as resp:
                    log("info", f"SSE connected to {OPENCODE_URL}/event")
                    with self.lock:
                        self.sse_connected = True
                        self.sse_last_event = now()
                    backoff = 1
                    self.reconcile()
                    for raw in resp:
                        if self.stop.is_set():
                            break
                        line = raw.decode("utf-8", "replace").strip()
                        with self.lock:
                            self.sse_last_event = now()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload:
                            continue
                        try:
                            event = json.loads(payload)
                        except Exception:
                            continue
                        try:
                            self.dispatch(event)
                        except Exception as exc:
                            log("error", f"event handler raised: {exc!r}")
            except Exception as exc:
                if not self.stop.is_set():
                    log("warn", f"SSE disconnected: {exc}")
            with self.lock:
                if self.sse_connected:
                    self.sse_down_since = now()
                self.sse_connected = False
            if self.stop.is_set():
                return
            down = now() - self.sse_down_since
            if down > SSE_DOWN_ALERT_SEC:
                self.alert(
                    "sse-down",
                    f"approval broker has had no event stream from opencode for {int(down)}s — "
                    f"remote approvals are NOT working and pending ones cannot be answered.",
                )
            self.stop.wait(backoff)
            backoff = min(backoff * 2, 30)

    def dispatch(self, event):
        etype = event.get("type")
        props = event.get("properties") or {}
        if etype == "permission.asked":
            self.on_asked(props)
        elif etype == "permission.replied":
            self.on_replied(props)
        elif etype == "session.deleted":
            self.on_session_deleted(props)

    # ---------------- ticker ----------------

    def tick_loop(self):
        last_sweep = now()
        while not self.stop.wait(1.0):
            try:
                self.tick()
            except Exception as exc:
                log("error", f"tick raised: {exc!r}")
            # Zombie sweep. POST /session/{id}/abort does NOT clear that
            # session's pending permissions — verified: the request is still in
            # GET /permission afterwards and the session drops straight to idle.
            # Nothing emits an event for that, so polling is the only way to
            # notice, and without it the broker would nag Kurt for the whole TTL
            # about a run that no longer exists.
            if now() - last_sweep >= ZOMBIE_SWEEP_SEC:
                last_sweep = now()
                try:
                    self.sweep_zombies()
                except Exception as exc:
                    log("error", f"zombie sweep raised: {exc!r}")

    def sweep_zombies(self):
        with self.lock:
            live = [r for r in self.pending.values() if r["state"] == "pending"]
        if not live:
            return
        statuses = self.opencode.session_status()
        if statuses is None:
            return  # server unreachable: assume nothing, touch nothing
        for req in live:
            if (statuses.get(req["sessionID"]) or {}).get("type") == "busy":
                continue
            log("warn", f"[{req['code']}] {req.get('permission')} outlived its run "
                        f"(session {req['sessionID'][:16]} is idle) — cleaning up silently")
            self.decide(req["id"], "reject", "zombie", DENY_ZOMBIE, cancel_text=None)

    # ---------------- WhatsApp inbound tail (phase-2 stand-in) ----------------

    def wa_tail_loop(self):
        """TEMPORARY. Phase 3 moves inbound votes into bridge/server.js, which
        can consume them before the agent ever sees them (and advance the
        responder cursor so catchUp() cannot resurrect a vote hours later).
        Until then the bridge is untouched, so the only way Kurt can actually
        vote from his watch is for the broker to read the message log the
        bridge already writes.

        Consequence to know about while this is the inbound path: the bridge
        still forwards the vote to the agent as an ordinary message, so Horus
        will also see a stray "cy" and may reply to it. Harmless, and it
        disappears with phase 3. Set HORUS_BROKER_WA_TAIL=0 to turn it off.
        """
        offset = None
        while not self.stop.wait(2.0):
            try:
                size = os.path.getsize(WA_LOG)
            except OSError:
                continue
            if offset is None:
                offset = size  # never replay history on startup
                continue
            if size < offset:
                offset = 0  # truncated/rotated
            if size == offset:
                continue
            try:
                with open(WA_LOG, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                    offset = fh.tell()
            except OSError:
                continue
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("fromMe"):
                    continue
                text = entry.get("text") or ""
                # Cheap pre-filter only. The grammar, the sender check and the
                # staleness check all live in handle_vote.
                if len(text.strip()) > 4:
                    continue
                try:
                    res = self.handle_vote(entry.get("from"), text, _parse_ts(entry.get("ts")))
                    if res.get("consumed"):
                        log("info", f"vote from messages.jsonl: {text!r} -> {res.get('reason')}")
                        sess = {"jid": entry.get("from"), "channels": ["whatsapp"]}
                        if res.get("ack"):
                            self.notifiers["whatsapp"].ack(sess, res["ack"])
                except Exception as exc:
                    # This runs in its own thread; an unhandled exception here
                    # would kill the only inbound vote path without a trace.
                    log("error", f"wa-tail vote handling raised: {exc!r}")

    # ---------------- introspection ----------------

    def snapshot(self):
        with self.lock:
            pend = []
            for r in sorted(self.pending.values(), key=lambda r: r["createdAt"]):
                pend.append({
                    "code": r["code"],
                    "sessionID": r["sessionID"],
                    "permission": r.get("permission"),
                    "summary": self.describe(r),
                    "state": r["state"],
                    "ageSec": int(now() - r["createdAt"]),
                    "expiresInSec": int(r["expiresAt"] - now()),
                })
            return {
                "ok": True,
                "version": VERSION,
                "sse": {
                    "connected": self.sse_connected,
                    "lastEventSecAgo": int(now() - self.sse_last_event) if self.sse_last_event else None,
                },
                "sessions": [
                    {"sessionID": s["sessionID"], "label": s.get("label"),
                     "channels": s.get("channels"), "ttlSec": s.get("ttlSec")}
                    for s in self.sessions.values()
                ],
                "pending": pend,
                "quarantine": [
                    {"code": q["code"], "outcome": q["outcome"], "at": hhmm(q["at"])}
                    for q in self.quarantine.values()
                ],
            }


def _outcome_word(outcome):
    return {
        "once": "approved",
        "always": "approved",
        "reject": "denied",
        "cascade": "cancelled with the rest of that run",
        "answered-elsewhere": "answered elsewhere",
        "resolved-while-away": "resolved",
        "already-resolved": "resolved",
    }.get(outcome, outcome)


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

BROKER: Broker = None  # set in main()


def _read_token():
    try:
        with open(BROKER_TOKEN_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "horus-approval-broker"

    def log_message(self, fmt, *args):  # quieter journal
        pass

    # -- helpers --

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return None

    def _authed(self):
        """The token is hygiene, not a boundary — it lives in ~/horus/.secrets/
        which is bind-mounted into the container. It stops accidents and
        stray localhost clients, nothing more. See the module docstring."""
        token = _read_token()
        if not token:
            self._json(503, {
                "error": f"no broker token at {BROKER_TOKEN_FILE} — create it "
                         f"(see approval.nix) and the authenticated routes will start working"
            })
            log("error", f"rejected an authenticated request: {BROKER_TOKEN_FILE} missing/empty")
            return False
        got = self.headers.get("X-Horus-Broker") or ""
        if not hmac.compare_digest(got, token):
            self._json(403, {"error": "bad or missing X-Horus-Broker"})
            return False
        return True

    # -- routes --

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._json(200, BROKER.snapshot())
        if path == "/pending":
            if not self._authed():
                return
            return self._json(200, {"pending": BROKER.snapshot()["pending"]})
        self._json(404, {"error": "unknown endpoint"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authed():
            return
        body = self._body()
        if body is None:
            return self._json(400, {"error": "invalid JSON body"})

        if path == "/session":
            sid = str(body.get("sessionID") or "")
            if not sid.startswith("ses"):
                return self._json(400, {"error": "sessionID must be an opencode session id (ses...)"})
            channels = body.get("channels") or ["whatsapp"]
            if not isinstance(channels, list) or not channels:
                return self._json(400, {"error": "channels must be a non-empty array"})
            jid = body.get("jid") or BROKER.default_jid()
            ttl = int(body.get("ttlSec") or DEFAULT_TTL_SEC)
            ttl = max(30, min(ttl, 3600))
            BROKER.register(sid, channels, jid, body.get("label"), ttl)
            return self._json(200, {"ok": True, "sessionID": sid, "ttlSec": ttl,
                                    "channels": channels, "jid": jid})

        if path == "/vote":
            res = BROKER.handle_vote(body.get("from"), body.get("text"), _parse_ts(body.get("ts")))
            return self._json(200, res)

        if path == "/cancel-all":
            with BROKER.lock:
                reqs = list(BROKER.pending.values())
                sessions = {r["sessionID"] for r in reqs}
            n = 0
            for r in reqs:
                if BROKER.decide(r["id"], "reject", "cancel-all", DENY_CANCEL,
                                 cancel_text=f"[{r['code']}] cancelled by `horus cancel` — "
                                             f"{r.get('permission')}: {BROKER.describe(r)}"):
                    n += 1
            log("info", f"cancel-all rejected {n} pending approval(s) across {len(sessions)} session(s)")
            return self._json(200, {"ok": True, "cancelled": n, "sessions": len(sessions)})

        self._json(404, {"error": "unknown endpoint"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self._authed():
            return
        m = re.fullmatch(r"/session/([^/]+)", path)
        if not m:
            return self._json(404, {"error": "unknown endpoint"})
        sid = unquote(m.group(1))
        sess, n = BROKER.unregister(sid)
        return self._json(200, {"ok": True, "wasRegistered": bool(sess), "rejected": n})


def _parse_ts(value):
    """Epoch seconds from whatever the caller had to hand.

    bridge/server.js writes `new Date(...).toISOString()`, i.e. UTC with a
    trailing Z. Parsing that as LOCAL time (the obvious mistake) shifts every
    vote by the UTC offset and makes each one look two hours old — which the
    staleness guard then silently drops. Assume UTC when no offset is given.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# --------------------------------------------------------------------------


def main():
    global BROKER
    log("info", f"horus-approval-broker {VERSION} starting")
    if BIND_HOST not in ("127.0.0.1", "::1", "localhost"):
        # The host firewall is disabled. Binding anything but loopback would put
        # the approval API on every WiFi Kurt joins. Phase 5 publishes /approve
        # via `tailscale serve`, which only ever proxies a loopback listener.
        log("ERROR", f"refusing to bind {BIND_HOST}: this service must stay on loopback")
        sys.exit(1)

    BROKER = Broker()
    if not _read_token():
        log("ERROR", f"{BROKER_TOKEN_FILE} is missing or empty — authenticated routes will "
                     f"return 503 until it exists. GET /health still works.")

    threads = [
        threading.Thread(target=BROKER.sse_loop, name="sse", daemon=True),
        threading.Thread(target=BROKER.tick_loop, name="tick", daemon=True),
    ]
    if WA_TAIL:
        threads.append(threading.Thread(target=BROKER.wa_tail_loop, name="wa-tail", daemon=True))
    for t in threads:
        t.start()

    httpd = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    httpd.daemon_threads = True

    def shutdown(signum, _frame):
        # Deliberately does NOT reject pending approvals. A broker restart (a
        # config tweak, a rebuild) must not kill in-flight runs; startup
        # reconciliation re-adopts them from GET /permission instead.
        log("info", f"signal {signum} — shutting down, {len(BROKER.pending)} approval(s) left pending")
        BROKER.stop.set()
        BROKER.save()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log("info", f"listening on http://{BIND_HOST}:{BIND_PORT} (opencode at {OPENCODE_URL})")
    httpd.serve_forever()
    log("info", "stopped")


if __name__ == "__main__":
    main()


# --------------------------------------------------------------------------
# Hand-testing recipes (phase 2 — nothing is migrated, so this is how it runs)
#
#   T=$(cat ~/horus/.secrets/broker-local)
#   pw=$(sed -n 's/^OPENCODE_SERVER_PASSWORD=//p' ~/horus/.secrets/opencode-server.env)
#   A=(-u "opencode:$pw"); S=http://127.0.0.1:4096
#
#   # a scratch session that pings the journal only (no WhatsApp to Kurt):
#   sid=$(curl -s "${A[@]}" -X POST $S/session -H 'content-type: application/json' \
#         -d '{"title":"broker test","permission":'"$(cat ~/horus/.opencode/broker-session-permission.json)"'}' \
#       | jq -r .id)
#   curl -s -H "X-Horus-Broker: $T" -X POST localhost:8790/session \
#        -H 'content-type: application/json' \
#        -d "{\"sessionID\":\"$sid\",\"channels\":[\"log\"],\"label\":\"test\",\"ttlSec\":120}"
#   curl -s "${A[@]}" -X POST $S/session/$sid/prompt_async -H 'content-type: application/json' \
#        -d '{"parts":[{"type":"text","text":"Run this shell command and show the output: curl --version"}]}'
#   curl -s localhost:8790/health | jq .pending
#   curl -s -H "X-Horus-Broker: $T" -X POST localhost:8790/vote \
#        -H 'content-type: application/json' -d '{"from":"<kurt jid>","text":"cy"}'
#
#   # for the real thing, register with channels ["whatsapp"] and answer from
#   # the watch — the messages.jsonl tailer picks the reply up.
# --------------------------------------------------------------------------
