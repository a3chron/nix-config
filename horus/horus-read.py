#!/usr/bin/env python3
# Horus read-aloud daemon: fetches a web article on the HOST, extracts its
# readable text, synthesizes it with Kokoro in chunks and plays them through
# mpv on the host's DEFAULT sink. The agent's `read_aloud` tool in the
# container reaches this on 127.0.0.1:8878 over the shared network namespace —
# same pattern as horus-music (:8877) and horus-studium (:8899). Runs as a user
# service (read.nix), impure repo path: the extractor and the chunker WILL need
# per-site tuning and that must never need a rebuild, only
# `systemctl --user restart horus-read`.
#
# WHY THE FETCH LIVES HERE AND NOT IN THE TOOL: a 40k-char article must never
# enter the container. If read_aloud.ts fetched it, the whole body would sit in
# the Bun process and then be POSTed here anyway — and the model's 64k context
# would be one careless `return` away from being eaten by an article nobody
# asked it to read. Host-side extraction means the text is never in the
# sandbox's address space at all; the tool only ever sees a head/tail sample.
#
# Unlike horus-music's mpv, THIS mpv loads mpv-mpris (read.nix bakes the script
# in), so a reading appears as a normal media player on the desktop and the
# headphones' play/pause button reaches it. See read.nix for the whole key
# chain and why playerctld is part of it.
#
# stdlib only — mpv comes from the service PATH, horus-tts by absolute path.
#
# API (JSON; every POST returns the fresh /status body, like horus-music):
#   GET  /status            GET  /jobs            GET /text?from=&len= (debug)
#   POST /preview {url?, skip_head_chars?, trim_tail_chars?, start_after?,
#                  stop_before?, force?}
#   POST /speak   {voice?, speed?, volume?}
#   POST /pause /resume /toggle /duck /unduck /stop /seek /volume /load
#
# started_at is load-bearing, exactly as in horus-music.py: unix ts of the last
# successful /speak. horus-voice-respond.sh compares it against the round start
# to decide "the article IS the answer -> skip TTS". Don't drop it.
import html
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import wave
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = "/home/a3chron"
PORT = int(os.environ.get("HORUS_READ_PORT", "8878"))
CACHE = Path(os.environ.get("HORUS_READ_CACHE", f"{HOME}/.cache/horus-read"))
TTS = "/run/current-system/sw/bin/horus-tts"
MUSIC_URL = "http://127.0.0.1:8877"

_runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
MPV_SOCK = os.path.join(_runtime, "horus-read-mpv.sock")
TTS_SOCK = os.path.join(_runtime, "horus-tts.sock")

DEFAULT_VOICE = "am_michael"
DEFAULT_SPEED = 1.15  # matches horus-voice-respond.sh's tts_speed
DEFAULT_VOLUME = 85

# --- chunking, driven by three MEASURED constraints -------------------------
# 1. horus_kokoro_core.MAX_TOKENS = 510 truncates a long sentence SILENTLY
#    (only a stderr line), so we hard-split over-long sentences ourselves.
# 2. The warm Kokoro daemon is a plain UnixStreamServer — one request at a
#    time — and horus-kokoro-client.py sets a 30s socket timeout. A chunk must
#    synthesize well inside that, both to avoid the timeout and because a voice
#    reply queued behind a chunk waits for it.
# 3. MEASURED realtime factor on this CPU (2026-08-08, speed 1.15) — the
#    number every chunking decision depends on, and which nobody had before:
#                    warm daemon              cold one-shot
#       350 chars    2.39s -> 21.3s  8.9x     2.89s -> 21.3s  7.4x
#       900 chars    5.98s -> 55.5s  9.3x     6.38s -> 55.5s  8.7x
#      1400 chars    9.21s -> 86.8s  9.4x     9.81s -> 86.8s  8.8x
#    ~= 61.6 ms of audio per character. So 900 chars costs ~6s (safe under the
#    client's 30s timeout) and synthesis outruns playback ~9:1 — chunk N+1 is
#    ready long before chunk N finishes playing. Observed in a real read:
#    26/26 chunks synthesized while playback was still on chunk 2.
#    Note the cold one-shot is only ~0.5s slower per call, NOT the 10-20s the
#    original plan assumed — the model load is cheap here. The tts-down pause
#    is therefore about correctness (don't blast an article at the speakers
#    when the headphones vanish), not about avoiding a stall.
CHUNK_CHARS = 900
CHUNK_CHARS_FIRST = 350  # first audio in ~2.4s instead of ~6.3s
# Derived from a MEASURED phoneme-token density, not guessed. Worst ratio seen
# over the long sentences of the Wikipedia speech-synthesis article (which is
# unusually dense — IPA, citations, abbreviations) was 1.40 tokens/char
# (780 chars -> 1095 tokens); typical English prose is ~1.15. At 300 chars the
# worst case is ~420 tokens against MAX_TOKENS = 510, an 18% margin. Going
# higher risks silent truncation; going lower splits more sentences, and every
# split puts the engine's 0.15s end-of-sentence pause in the MIDDLE of a
# sentence, which is audible.
MAX_SENTENCE_CHARS = 300
SENTENCE_SPLIT_AT = 280
SEC_PER_CHAR = 0.0616  # measured above; used only for the not-yet-synthesized tail
PREBUFFER = 2
SILENCE_LEAD_S = 0.5  # see horus-voice-respond.sh:118-124 — BT eats the first ~300ms
# NO extra silence at the end of a chunk. This used to be 0.35s "for prosody"
# and it was the stutter Kurt reported on 2026-08-08: "30s to a minute of
# smooth speech, then it just paused for a sec or two, then continued".
#
# The engine already ends every sentence with 0.15s (horus_kokoro_core.py:103)
# plus trim_silence's 0.05s margin, so a chunk boundary landed on 0.55s of dead
# air while a paragraph break INSIDE a chunk got only 0.15s. Chunks are 40-55s
# long, so the listener heard an unexplained pause at exactly that cadence —
# i.e. the chunking itself became audible, which is the one thing it must not
# be. Boundaries must be acoustically indistinguishable from any other sentence
# break, so we add nothing and let the engine's own spacing carry through.
#
# Worth knowing: a mechanical gap meter CANNOT see this — silence is still
# audio advancing at wall-clock rate. Measured playback gaps were 0.00s while
# this bug was fully present. Diagnose boundary complaints by inspecting the
# WAVs, not by timing the player.
SILENCE_PARA_S = 0.0

HEAD_CHARS = 1200
TAIL_CHARS = 800
SHORT_TEXT = 2200  # below this, preview returns the whole text as `head`

FETCH_TIMEOUT = 20
MAX_FETCH_BYTES = 8 * 1024 * 1024
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

GRACE_S = 2 * 3600       # a finished article is kept this long, then discarded
KEEP_S = 7 * 86400
MAX_JOBS = 3
MAX_CACHE_BYTES = 3 * 1024 * 1024 * 1024

_lock = threading.RLock()
_mpv: subprocess.Popen | None = None
JOB: "Job | None" = None
_started_at = 0.0
_fetching = False


def log(msg):
    print(msg, flush=True)


# =============================== extraction =================================

SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "footer",
             "aside", "form", "iframe", "button", "select", "textarea",
             "figure", "figcaption"}
PARA_TAGS = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "pre", "dd"}
CONTAINER_TAGS = {"div", "article", "main", "section", "body", "td", "ul", "ol",
                  "dl", "html", "table", "tbody", "tr", "header"}
VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "col"}

# short lines that are navigation/chrome rather than prose
NAV_RE = re.compile(
    r"^(share|subscribe|sign in|sign up|log ?in|read more|continue reading|"
    r"comments?|leave a comment|advertisement|related( posts?)?|next|previous|"
    r"prev|home|menu|search|newsletter|follow|tweet|like|donate|support us|"
    r"cookie[s]?|accept|privacy|terms|copyright|all rights reserved|"
    r"\d+ comments?|©.*)$", re.I)


class _Node:
    __slots__ = ("tag", "parent", "kids", "text_len", "link_len", "paras")

    def __init__(self, tag, parent):
        self.tag = tag
        self.parent = parent
        self.kids = []
        self.text_len = 0
        self.link_len = 0
        self.paras = []   # (order, text, link_len)


class _Reader(HTMLParser):
    """Builds a shallow tree of block containers, accumulating per-node text
    length, link-text length and the paragraphs found directly inside it. That
    is enough for both the structural pass (<article>/<main>) and the
    link-density fallback, and it is the part of this file most likely to need
    tuning for a specific site — which is exactly why it is impure."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("html", None)
        self.cur = self.root
        self.skip = 0
        self.a_depth = 0
        self.para_tag = None
        self.para_buf = []
        self.para_link = 0
        self.order = 0
        self.title = None
        self._in_title = False
        self.nodes = [self.root]

    # -- helpers
    def _close_para(self):
        if self.para_tag is None:
            return
        text = re.sub(r"\s+", " ", "".join(self.para_buf)).strip()
        link_len = self.para_link
        self.para_tag = None
        self.para_buf = []
        self.para_link = 0
        if text:
            self.cur.paras.append((self.order, text, link_len))
            self.order += 1

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in VOID_TAGS:
            if tag == "br" and self.para_tag is not None:
                self.para_buf.append(" ")
            if tag == "meta":
                d = dict(attrs)
                prop = (d.get("property") or d.get("name") or "").lower()
                if prop in ("og:title", "twitter:title") and not self.title:
                    self.title = (d.get("content") or "").strip() or None
            return
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "a":
            self.a_depth += 1
            return
        if tag in PARA_TAGS:
            self._close_para()
            self.para_tag = tag
            self.para_buf = []
            self.para_link = 0
            return
        if tag in CONTAINER_TAGS:
            self._close_para()
            node = _Node(tag, self.cur)
            self.cur.kids.append(node)
            self.nodes.append(node)
            self.cur = node

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "a":
            self.a_depth = max(0, self.a_depth - 1)
            return
        if tag in PARA_TAGS:
            if self.para_tag == tag:
                self._close_para()
            return
        if tag in CONTAINER_TAGS:
            self._close_para()
            # walk up to the nearest open node with this tag; malformed markup
            # is the norm, so never unwind past the root
            n = self.cur
            while n is not None and n.tag != tag:
                n = n.parent
            self.cur = n.parent if n is not None and n.parent is not None else self.root

    def handle_data(self, data):
        if self._in_title and not self.title:
            t = data.strip()
            if t:
                self.title = t
        if self.skip or not data:
            return
        n = len(data.strip())
        if n:
            self.cur.text_len += n
            if self.a_depth:
                self.cur.link_len += n
        if self.para_tag is not None:
            self.para_buf.append(data)
            if self.a_depth:
                self.para_link += n

    def close(self):
        super().close()
        self._close_para()


def _subtree(node, cache):
    """(text_len, link_len, paras) for a node's whole subtree."""
    if node in cache:
        return cache[node]
    tl, ll = node.text_len, node.link_len
    paras = list(node.paras)
    for k in node.kids:
        ktl, kll, kparas = _subtree(k, cache)
        tl += ktl
        ll += kll
        paras.extend(kparas)
    cache[node] = (tl, ll, paras)
    return cache[node]


def _clean_paras(paras):
    """Document-ordered, de-junked paragraph list.

    The per-paragraph link ratio is what removes navigation the STRUCTURAL pass
    cannot: Wikipedia's interwiki language list lives inside <main>, so picking
    <main> drags in 300 language names before the first sentence. They are
    short and ~100% link text, which no whole-node density score catches once
    the article's own prose is averaged in with them."""
    out = []
    for item in sorted(paras, key=lambda p: p[0]):
        text, link_len = item[1], item[2]
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        if len(text) < 25 and NAV_RE.match(text):
            continue
        if len(text) < 120 and link_len >= 0.8 * len(text):
            continue  # a link in a list = navigation, not prose
        out.append(text)
    return out


def extract_html(markup):
    """(title, text). Three tiers: <article> -> <main> -> link-density."""
    p = _Reader()
    try:
        p.feed(markup)
        p.close()
    except Exception as e:
        log(f"html parse degraded ({type(e).__name__}: {e}) — using what we got")
    cache = {}
    best = None

    def pick(tag):
        cands = [n for n in p.nodes if n.tag == tag]
        if not cands:
            return None
        return max(cands, key=lambda n: _subtree(n, cache)[0])

    for tag in ("article", "main"):
        n = pick(tag)
        if n is not None and _subtree(n, cache)[0] >= 200:
            best = n
            break

    if best is None:
        # link-density fallback: the densest block of real prose
        scored = []
        for n in p.nodes:
            tl, ll, _ = _subtree(n, cache)
            if tl < 200:
                continue
            density = ll / max(1, tl)
            if density > 0.35:
                continue
            scored.append((tl * (1 - density), n))
        if scored:
            best = max(scored, key=lambda s: s[0])[1]

    paras = _clean_paras(_subtree(best, cache)[2] if best is not None
                         else _subtree(p.root, cache)[2])
    return p.title, "\n\n".join(paras)


def _feed_text(raw_bytes):
    """(title, html_fragment) from the newest entry of an RSS/Atom feed, or
    None if this isn't a feed. Substack/WordPress `/feed` URLs carry the FULL
    article in content:encoded — no nav, no ads — which is why the tool
    description tells the model to prefer a feed URL when it knows one."""
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError:
        return None
    tag = root.tag.split("}")[-1].lower()
    CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
    ATOM = "{http://www.w3.org/2005/Atom}"
    if tag == "rss":
        chan = root.find("channel")
        items = chan.findall("item") if chan is not None else []
        if not items:
            return None
        it = items[0]
        for key in (CONTENT, "content:encoded", "description"):
            el = it.find(key)
            if el is not None and (el.text or "").strip():
                title = (it.findtext("title") or "").strip() or None
                return title, el.text
        return None
    if tag == "feed":
        entries = root.findall(f"{ATOM}entry")
        if not entries:
            return None
        e = entries[0]
        for key in (CONTENT, f"{ATOM}content", f"{ATOM}summary"):
            el = e.find(key)
            if el is not None:
                body = el.text or "".join(ET.tostring(c, encoding="unicode") for c in el)
                if body.strip():
                    title = (e.findtext(f"{ATOM}title") or "").strip() or None
                    return title, body
        return None
    return None


def fetch(url):
    """(final_url, title, text, source). Raises ValueError for anything the
    caller (i.e. the model) can fix by passing a different URL."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"only http(s) URLs can be read aloud, got {url!r}")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        # identity: skips gzip handling entirely, stdlib does not do it for us
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if not ("text/" in ctype or "xml" in ctype or "json" in ctype or not ctype):
                raise ValueError(f"{url} is {ctype.split(';')[0] or 'an unknown type'}, not a readable page")
            body = b""
            while len(body) < MAX_FETCH_BYTES:
                block = r.read(65536)
                if not block:
                    break
                body += block
            final_url = r.geturl()
            charset = None
            if "charset=" in ctype:
                charset = ctype.split("charset=")[1].split(";")[0].strip()
    except ValueError:
        raise
    except Exception as e:
        # a dead host / 404 / TLS problem is the model's to fix (wrong URL), so
        # surface it as a 400 with the real reason rather than a 500
        raise ValueError(f"could not fetch {url}: {type(e).__name__}: {e}")

    if not charset:
        m = re.search(rb'charset=["\']?([\w-]+)', body[:4096], re.I)
        charset = m.group(1).decode("ascii", "replace") if m else "utf-8"
    try:
        markup = body.decode(charset, errors="replace")
    except LookupError:
        markup = body.decode("utf-8", errors="replace")

    source = "html"
    title = None
    feed = None
    if "xml" in ctype or re.match(r"\s*(<\?xml|<rss|<feed)", markup[:200], re.I):
        feed = _feed_text(body)
    if feed:
        source = "feed"
        title, fragment = feed
        _, text = extract_html(fragment)
    else:
        title, text = extract_html(markup)

    if not text.strip():
        raise ValueError(
            f"no readable text found at {final_url} — the page may be JavaScript-rendered "
            "or paywalled; try the site's /feed URL or a different source")
    return final_url, (title or final_url), text, source


# ================================ trimming ==================================

def _norm_index(raw):
    """Whitespace-normalized lowercase copy of `raw` plus a map from each of
    its characters back to an index in `raw`. Lets markers be matched
    forgivingly (case, line wrapping) while cutting the ORIGINAL text."""
    chars = []
    idx = []
    prev_ws = True
    for i, ch in enumerate(raw):
        if ch.isspace():
            if prev_ws:
                continue
            chars.append(" ")
            idx.append(i)
            prev_ws = True
        else:
            chars.append(ch.lower())
            idx.append(i)
            prev_ws = False
    return "".join(chars), idx


def marker_span(raw, marker):
    """(start, end) in `raw` for the first occurrence of `marker`, or None."""
    norm, idx = _norm_index(raw)
    needle = re.sub(r"\s+", " ", marker).strip().lower()
    if not needle:
        return None
    p = norm.find(needle)
    if p < 0:
        return None
    return idx[p], idx[p + len(needle) - 1] + 1


def apply_trims(raw, trims):
    """(text, warnings). A marker that isn't found is a WARNING, never a 400:
    a typo'd marker is a model-input problem and the model needs to SEE the
    head/tail again to pick a better one. A throw gives it nothing to work
    with. skip_head_chars >= len IS a 400 — that's incoherent, not a near-miss."""
    warnings = []
    text = raw
    after = trims.get("start_after")
    if after:
        span = marker_span(text, after)
        if span:
            text = text[span[1]:].lstrip()
        else:
            warnings.append(
                f"start_after marker {after[:60]!r} not found — ignored; "
                "the head sample below is untrimmed, pick a phrase you can see in it")
    before = trims.get("stop_before")
    if before:
        span = marker_span(text, before)
        if span:
            text = text[:span[0]].rstrip()
        else:
            warnings.append(
                f"stop_before marker {before[:60]!r} not found — ignored; "
                "the tail sample below is untrimmed, pick a phrase you can see in it")
    skip = int(trims.get("skip_head_chars") or 0)
    if skip:
        if skip < 0:
            raise ValueError("skip_head_chars must be >= 0")
        if skip >= len(text):
            raise ValueError(
                f"skip_head_chars={skip} would drop the whole article ({len(text)} chars left)")
        text = text[skip:].lstrip()
    tail = int(trims.get("trim_tail_chars") or 0)
    if tail:
        if tail < 0:
            raise ValueError("trim_tail_chars must be >= 0")
        if tail >= len(text):
            raise ValueError(
                f"trim_tail_chars={tail} would drop the whole article ({len(text)} chars left)")
        text = text[:-tail].rstrip()
    return text.strip(), warnings


# ================================ chunking ==================================

# the punctuation horus_kokoro_core.py:91 splits sentences on, allowing for a
# trailing quote/bracket
ENDS_SENTENCE = re.compile(r"[.!?;:][\"'’”)\]]*$")


def hard_split(sentence):
    """Kokoro truncates >510 tokens per inference SILENTLY. Split anything long
    enough to risk it, preferring a comma/dash/space boundary.

    Returns [(piece, is_fragment)]. `is_fragment` marks a piece that does NOT
    carry the sentence's terminating punctuation, i.e. one we created by
    cutting a sentence in half. Only those force a chunk break — an ordinary
    unpunctuated line such as a heading does not, so headings still ride along
    with the paragraph that follows them instead of each becoming its own
    3-character chunk."""
    out = []
    s = sentence
    while len(s) > MAX_SENTENCE_CHARS:
        window = s[:SENTENCE_SPLIT_AT]
        cut = max(window.rfind(", "), window.rfind(" — "), window.rfind("; "))
        if cut < SENTENCE_SPLIT_AT // 3:
            cut = window.rfind(" ")
        if cut < SENTENCE_SPLIT_AT // 3:
            cut = SENTENCE_SPLIT_AT
        else:
            cut += 1
        out.append((s[:cut].strip(), True))
        s = s[cut:].strip()
    if s:
        out.append((s, False))
    return out


def build_chunks(text):
    """[(text, ends_paragraph)] — split on the SAME regex the Kokoro engine
    uses (horus_kokoro_core.py:91) so we never hand it a unit it would split
    differently, and end a chunk on a paragraph boundary when one is near the
    target (better prosody, and the trailing silence lands where a reader would
    pause)."""
    units = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        sents = [s for s in re.split(r"(?<=[.!?;:])\s+", para) if s.strip()]
        pieces = []
        for s in sents:
            pieces.extend(hard_split(s.strip()))
        for i, (piece, is_fragment) in enumerate(pieces):
            units.append((piece, i == len(pieces) - 1, is_fragment))

    chunks = []
    cur, cur_len = [], 0
    run = 0  # chars since the last sentence-ending punctuation IN THIS CHUNK
    target = CHUNK_CHARS_FIRST
    for piece, ends_para, is_fragment in units:
        plen = len(piece) + 1
        # Flush BEFORE appending when this piece would extend an unterminated
        # run past the cap. Checking afterwards is too late — the oversized run
        # is already in the chunk by then — and resetting the counter on the
        # piece that carries the punctuation without counting that piece was
        # the specific mistake that let a 523-char "sentence" reach the engine.
        # What the engine sees as one sentence is everything since the previous
        # punctuation UP TO AND INCLUDING the punctuated piece, so the cap has
        # to be tested against run + this piece, before committing to it.
        if cur and run > 0 and run + plen > MAX_SENTENCE_CHARS:
            chunks.append((" ".join(cur), False))
            cur, cur_len, run = [], 0, 0
            target = CHUNK_CHARS
        cur.append(piece)
        cur_len += plen
        run = 0 if ENDS_SENTENCE.search(piece) else run + plen
        # The engine re-splits our chunk text on sentence punctuation
        # (horus_kokoro_core.py:91), so anything we join with a plain space and
        # that carries no such punctuation is handed to it as ONE sentence.
        # Two ways that can exceed MAX_TOKENS, both of which we must prevent
        # because the engine's response is to truncate SILENTLY (and, until the
        # clamp fix in horus_kokoro_core.py, to crash — four missing passages
        # in one Wikipedia article):
        #   - a fragment we created by cutting a long sentence, which would be
        #     glued straight back onto its own continuation, and
        #   - an accumulated run of unpunctuated lines (headings, list items).
        # Ending the chunk makes the run the chunk's last sentence, bounded by
        # MAX_SENTENCE_CHARS. Extra boundaries used to cost 0.55s of dead air
        # each; since SILENCE_PARA_S went to 0 they are inaudible, so this is
        # free.
        if is_fragment or cur_len >= target or (ends_para and cur_len >= target * 0.7):
            chunks.append((" ".join(cur), ends_para))
            cur, cur_len, run = [], 0, 0
            target = CHUNK_CHARS
    if cur:
        chunks.append((" ".join(cur), True))
    return chunks


# ================================== mpv =====================================

def mpv_alive():
    return _mpv is not None and _mpv.poll() is None


def ensure_mpv():
    """Spawn the idle mpv if it isn't running (call with _lock held)."""
    global _mpv
    if mpv_alive():
        return
    try:
        os.unlink(MPV_SOCK)
    except FileNotFoundError:
        pass
    _mpv = subprocess.Popen(
        # --gapless-audio=yes keeps the AUDIO DEVICE open across playlist
        # entries (all chunks share one format, 24kHz/mono/s16, so this is
        # safe) and --prefetch-playlist=yes opens the next chunk before the
        # current one ends. Both exist to keep chunk boundaries inaudible.
        # Measured on the speakers: 0 stalls in 150s across 3 boundaries. The
        # Bluetooth path could not be measured (headphones disconnected) — and
        # that is exactly where a dropped device costs 1-2s to re-establish,
        # which is why the device is kept open rather than merely prefetched.
        ["mpv", "--idle=yes", "--no-video", "--no-terminal",
         "--gapless-audio=yes", "--prefetch-playlist=yes",
         f"--volume={DEFAULT_VOLUME}", f"--input-ipc-server={MPV_SOCK}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if os.path.exists(MPV_SOCK):
            return
        if _mpv.poll() is not None:
            raise RuntimeError("mpv exited during startup")
        time.sleep(0.1)
    raise RuntimeError("mpv IPC socket did not appear")


def mpv_cmd(*command):
    """One mpv IPC request. Fresh connection per call, matched on request_id so
    async event lines are skipped — same as horus-music.py."""
    req_id = 1
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(MPV_SOCK)
        s.sendall((json.dumps({"command": list(command), "request_id": req_id}) + "\n").encode())
        buf = b""
        while True:
            got = s.recv(4096)
            if not got:
                raise RuntimeError("mpv closed the IPC connection")
            buf += got
            for line in buf.split(b"\n"):
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("request_id") == req_id:
                    if msg.get("error") != "success":
                        raise RuntimeError(f"mpv: {msg.get('error')}")
                    return msg.get("data")
            buf = buf.rsplit(b"\n", 1)[-1]


def get_prop(name, default=None):
    try:
        return mpv_cmd("get_property", name)
    except Exception:
        return default


# ================================== job =====================================

class Job:
    def __init__(self, job_id, url, final_url, title, source, raw_chars):
        self.id = job_id
        self.url = url
        self.final_url = final_url
        self.title = title
        self.source = source
        self.raw_chars = raw_chars
        self.dir = CACHE / job_id
        self.trims = {"skip_head_chars": 0, "trim_tail_chars": 0,
                      "start_after": None, "stop_before": None}
        self.text = ""
        self.state = "ready"
        self.pause_reason = None
        self.chunks = []          # [(text, ends_para)]
        self.durations = []       # seconds per chunk; 0.0 for a failed one
        self.chunks_ready = 0
        self.ok = []              # per-chunk success
        self.warnings = []
        self.error = None
        self.voice = DEFAULT_VOICE
        self.speed = DEFAULT_SPEED
        self.position = 0.0
        self.created_at = time.time()
        self.updated_at = time.time()
        self.finished_at = None
        self.suspended = False    # /duck suspends the synth worker
        self.cancelled = False
        self.music_ducked = False
        self.worker = None
        self.vocab_warned = 0

    # -- persistence
    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {
            "id": self.id, "url": self.url, "final_url": self.final_url,
            "title": self.title, "source": self.source, "raw_chars": self.raw_chars,
            "trims": self.trims, "state": self.state, "pause_reason": self.pause_reason,
            "chunks": self.chunks, "durations": self.durations, "ok": self.ok,
            "chunks_ready": self.chunks_ready, "warnings": self.warnings,
            "error": self.error, "voice": self.voice, "speed": self.speed,
            "position": self.position, "created_at": self.created_at,
            "updated_at": time.time(), "finished_at": self.finished_at,
            "chars": len(self.text),
        }
        tmp = self.dir / "job.json.tmp"
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.dir / "job.json")

    @classmethod
    def load(cls, path):
        d = json.loads((path / "job.json").read_text())
        j = cls(d["id"], d["url"], d.get("final_url") or d["url"], d.get("title") or d["url"],
                d.get("source") or "html", d.get("raw_chars") or 0)
        j.trims = d.get("trims") or j.trims
        j.chunks = [tuple(c) for c in (d.get("chunks") or [])]
        j.durations = d.get("durations") or []
        j.ok = d.get("ok") or []
        j.chunks_ready = d.get("chunks_ready") or 0
        j.warnings = d.get("warnings") or []
        j.error = d.get("error")
        j.voice = d.get("voice") or DEFAULT_VOICE
        j.speed = d.get("speed") or DEFAULT_SPEED
        j.position = d.get("position") or 0.0
        j.created_at = d.get("created_at") or time.time()
        j.updated_at = d.get("updated_at") or time.time()
        j.finished_at = d.get("finished_at")
        j.state = d.get("state") or "ready"
        raw = path / "raw.txt"
        if raw.exists():
            j.text = apply_trims(raw.read_text(), j.trims)[0]
        return j

    # -- derived
    def duration(self):
        known = sum(self.durations[:self.chunks_ready])
        rest = sum(len(c[0]) for c in self.chunks[self.chunks_ready:]) * SEC_PER_CHAR
        return known + rest

    def duration_final(self):
        return bool(self.chunks) and self.chunks_ready >= len(self.chunks)

    def chunk_path(self, i):
        return self.dir / "chunks" / f"{i:03d}.wav"


def raw_path(job):
    return job.dir / "raw.txt"


def slug(url):
    host = urllib.parse.urlparse(url).netloc or "page"
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")


# ============================== synthesis ===================================

def wav_duration(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / float(w.getframerate())


def pad_wav(path, lead=0.0, tail=0.0):
    """Prepend/append silence in the file's own format."""
    if lead <= 0 and tail <= 0:
        return
    with wave.open(str(path), "rb") as r:
        params = r.getparams()
        frames = r.readframes(r.getnframes())
    unit = params.sampwidth * params.nchannels
    pre = b"\x00" * (unit * int(params.framerate * lead))
    post = b"\x00" * (unit * int(params.framerate * tail))
    with wave.open(str(path), "wb") as w:
        w.setparams(params)
        w.writeframes(pre + frames + post)


def tts_up():
    return os.path.exists(TTS_SOCK)


def synth_chunk(job, i):
    """True if chunk i produced audio. Never raises for a per-chunk problem —
    a missing paragraph beats a dead read (horus-voice-respond.sh:111-114
    learned the 0-byte-wav lesson)."""
    text, ends_para = job.chunks[i]
    out = job.chunk_path(i)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            [TTS, "--speed", str(job.speed), "--voice", job.voice, "--out", str(out), text],
            capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        with _lock:
            job.warnings.append(f"chunk {i}: TTS timed out — that passage is missing")
        return False
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()[-160:]
        with _lock:
            job.warnings.append(f"chunk {i}: TTS produced no audio — that passage is missing"
                                + (f" ({err})" if err else ""))
        return False
    # Kokoro shouts dropped phonemes into stderr and nothing ever read it —
    # surface it once the problem is widespread enough to be audible.
    if b"not in VOCAB" in (r.stderr or b""):
        with _lock:
            job.vocab_warned += 1
    # only chunk 0 is padded, and only at the FRONT (the BT ramp-up). Never pad
    # the tail — see SILENCE_PARA_S.
    pad_wav(out, lead=SILENCE_LEAD_S if i == 0 else 0.0,
            tail=SILENCE_PARA_S if ends_para else 0.0)
    return True


def synth_worker(job):
    """One background worker: synthesize chunk N+1 while mpv plays chunk N, and
    append each finished chunk to the playlist. Progressive, not up-front — a
    40k-char article is ~45 minutes of speech and even at the measured 9x
    realtime that is ~5 minutes of synthesis before the first word."""
    while True:
        with _lock:
            if job.cancelled:
                return
            i = job.chunks_ready
            if i >= len(job.chunks):
                break
            suspended = job.suspended
        if suspended:
            time.sleep(0.3)
            continue
        if not tts_up():
            # horus-kokoro is PartOf horus-voice: it dies when the headphones
            # disconnect and on `horus pause`. Falling through to the cold
            # one-shot would load the ONNX model PER CHUNK (~10-20s each) and
            # the read would look hung — and blasting the article out of the
            # speakers is the wrong answer to "headphones went away" anyway.
            with _lock:
                if job.state == "playing":
                    _pause(job, "tts-down")
                    log("TTS engine gone — reading paused until it returns")
            time.sleep(5)
            with _lock:
                if job.cancelled:
                    return
                if tts_up() and job.pause_reason == "tts-down":
                    _resume(job)
                    log("TTS engine back — resuming")
            continue

        ok = synth_chunk(job, i)
        with _lock:
            if job.cancelled:
                return
            dur = 0.0
            if ok:
                try:
                    dur = wav_duration(job.chunk_path(i))
                except Exception as e:
                    ok = False
                    job.warnings.append(f"chunk {i}: unreadable wav ({e})")
            job.durations.append(dur)
            job.ok.append(ok)
            job.chunks_ready = i + 1
            if ok and job.state in ("playing", "paused", "synthesizing"):
                try:
                    mpv_cmd("loadfile", str(job.chunk_path(i)), "append")
                except Exception as e:
                    log(f"could not append chunk {i}: {e}")
            failed = sum(1 for o in job.ok if not o)
            if failed > max(3, 0.3 * len(job.chunks)):
                job.state = "failed"
                job.error = f"{failed} of {job.chunks_ready} chunks failed to synthesize"
                job.cancelled = True
                stop_mpv()
                job.save()
                log(f"read failed: {job.error}")
                return
            if job.chunks_ready % 5 == 0:
                job.save()

    with _lock:
        if job.vocab_warned > 0.2 * max(1, len(job.chunks)):
            job.warnings.append(
                "this text has a lot of non-English characters (umlauts?) — "
                "parts of it will sound mangled")
        job.save()
    log(f"synthesis complete: {job.chunks_ready} chunks, {job.duration()/60:.1f} min")


# ============================ playback control ==============================

def stop_mpv():
    if mpv_alive():
        try:
            mpv_cmd("stop")
        except Exception:
            pass


def cum_offsets(job):
    off, total = [], 0.0
    for d in job.durations:
        off.append(total)
        total += d
    return off, total


def current_position(job):
    """mpv only knows the current playlist entry, so add the durations of the
    entries already played."""
    if not mpv_alive():
        return job.position
    idx = get_prop("playlist-pos")
    tp = get_prop("time-pos")
    if idx is None or idx < 0:
        return job.position
    off, _ = cum_offsets(job)
    base = off[idx] if idx < len(off) else (off[-1] if off else 0.0)
    return base + (tp or 0.0)


def _pause(job, reason):
    if mpv_alive():
        try:
            mpv_cmd("set_property", "pause", True)
        except Exception:
            pass
    job.position = current_position(job)
    job.state = "paused"
    job.pause_reason = reason
    job.suspended = reason in ("voice",)
    job.save()


def _resume(job):
    if mpv_alive():
        try:
            mpv_cmd("set_property", "pause", False)
        except Exception:
            pass
    job.state = "playing"
    job.pause_reason = None
    job.suspended = False
    job.save()


def music_call(path, method="GET"):
    try:
        req = urllib.request.Request(f"{MUSIC_URL}{path}", method=method)
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r)
    except Exception:
        return None


def seek_to(job, seconds):
    """Seek across chunk boundaries: pick the target playlist entry, jump to
    it, then seek inside it."""
    off, total = cum_offsets(job)
    ready = job.chunks_ready
    if not ready:
        raise ValueError("nothing synthesized yet")
    playable = sum(job.durations[:ready])
    target = max(0.0, min(seconds, max(0.0, playable - 1.0)))
    idx = 0
    for i in range(ready):
        if off[i] <= target:
            idx = i
        else:
            break
    inner = target - off[idx]
    mpv_cmd("set_property", "playlist-pos", idx)
    # the new entry needs a moment before a seek lands
    for _ in range(20):
        if get_prop("playlist-pos") == idx and get_prop("duration") is not None:
            break
        time.sleep(0.05)
    try:
        mpv_cmd("seek", inner, "absolute")
    except Exception:
        pass
    job.position = target
    job.save()
    return target


def rebuild_playlist(job, from_time=0.0):
    """Load every already-synthesized chunk into mpv, then seek. Used by
    /resume after a daemon restart and by /load."""
    ensure_mpv()
    mpv_cmd("playlist-clear")
    try:
        mpv_cmd("stop")
    except Exception:
        pass
    first = True
    for i in range(job.chunks_ready):
        if i < len(job.ok) and not job.ok[i]:
            continue
        p = job.chunk_path(i)
        if not p.exists():
            continue
        mpv_cmd("loadfile", str(p), "replace" if first else "append")
        first = False
    if first:
        raise RuntimeError("no synthesized audio left on disk for this article")
    for _ in range(80):
        if get_prop("duration") is not None and get_prop("idle-active") is False:
            break
        time.sleep(0.05)
    set_media_title(job)
    if from_time > 0:
        try:
            seek_to(job, from_time)
        except Exception as e:
            log(f"could not restore position: {e}")


def set_media_title(job):
    """One global force-media-title so EVERY chunk reports the article title
    over MPRIS — without this the desktop player widget and the headphones
    would show `003.wav`."""
    try:
        mpv_cmd("set_property", "force-media-title", job.title)
    except Exception as e:
        log(f"could not set media title: {e}")


def start_speaking(job, voice, speed, volume):
    global _started_at
    if not job.text.strip():
        raise ValueError("nothing to read — preview an article first")
    job.voice = voice or job.voice
    job.speed = speed or job.speed
    job.chunks = build_chunks(job.text)
    job.durations, job.ok, job.chunks_ready = [], [], 0
    job.warnings, job.error, job.vocab_warned = [], None, 0
    job.position = 0.0
    job.cancelled = False
    job.suspended = False
    job.state = "synthesizing"
    shutil.rmtree(job.dir / "chunks", ignore_errors=True)
    job.save()
    log(f"speaking {job.title!r}: {len(job.text)} chars in {len(job.chunks)} chunks")

    if not tts_up():
        raise RuntimeError(
            "the Kokoro TTS engine is not running (horus-kokoro is PartOf horus-voice, "
            "so it is only warm while the headphones are connected)")

    # duck music, symmetric with the voice pattern; resumed on done/stop
    st = music_call("/status")
    if st and st.get("state") == "playing":
        job.music_ducked = music_call("/pause", "POST") is not None

    ensure_mpv()
    try:
        mpv_cmd("set_property", "volume", max(0, min(100, volume or DEFAULT_VOLUME)))
    except Exception:
        pass

    job.worker = threading.Thread(target=synth_worker, args=(job,), daemon=True)
    job.worker.start()

    # wait for chunk 0, then for mpv to CONFIRM it is playing — the same
    # discipline as horus-music.py:196-209. Claiming "playing" on faith made
    # the voice pipeline suppress TTS for audio that never came out.
    deadline = time.time() + 60
    loaded = False
    while time.time() < deadline:
        with _lock:
            if job.cancelled or job.state == "failed":
                raise RuntimeError(job.error or "synthesis failed before any audio")
            ready = job.chunks_ready
            ok0 = bool(job.ok) and job.ok[0]
        if ready >= 1:
            if not ok0:
                raise RuntimeError(
                    "the first chunk of this article produced no audio — TTS or text problem")
            with _lock:
                mpv_cmd("loadfile", str(job.chunk_path(0)), "replace")
                mpv_cmd("set_property", "pause", False)
            loaded = True
            break
        time.sleep(0.1)
    if not loaded:
        raise RuntimeError("TTS did not produce the first chunk within 60s")

    started = False
    while time.time() < deadline:
        if get_prop("duration") is not None and get_prop("idle-active") is False:
            started = True
            break
        time.sleep(0.25)
    if not started:
        raise RuntimeError("mpv never started the reading — TTS or audio problem")

    with _lock:
        set_media_title(job)
        job.state = "playing"
        job.pause_reason = None
        _started_at = time.time()
        job.save()
    return job


# ============================== monitor loop ================================

def monitor():
    """One poll thread: position, underrun, completion, and the music
    interlock. Everything that has to happen without an HTTP request."""
    last_save = 0.0
    while True:
        time.sleep(1.0)
        try:
            with _lock:
                job = JOB
                if job is None or job.state not in ("playing", "paused"):
                    continue
                if job.state == "playing":
                    job.position = current_position(job)

                done_synth = job.chunks_ready >= len(job.chunks)

                # mpv dying is NOT "finished". Before this check a crashed (or
                # externally killed) player made the playlist look exhausted and
                # a half-read article was reported `done` — which also threw
                # away the resume position. Park it instead; /resume rebuilds
                # the playlist from the chunks still on disk.
                if not mpv_alive():
                    if job.state == "playing":
                        job.state = "paused"
                        job.pause_reason = "player-gone"
                        job.save()
                        log("mpv is gone — reading parked, /resume will rebuild it")
                    continue

                idle = get_prop("idle-active", False)

                if job.state == "playing" and idle:
                    if done_synth:
                        job.state = "done"
                        job.position = sum(job.durations)
                        job.finished_at = time.time()
                        job.pause_reason = None
                        if job.music_ducked:
                            music_call("/resume", "POST")
                            job.music_ducked = False
                        job.save()
                        log(f"finished reading {job.title!r}")
                        threading.Thread(target=evict, daemon=True).start()
                        continue
                    # playlist ran dry with chunks still coming: without this
                    # mpv goes idle and we would report `done` for half an
                    # article
                    _pause(job, "underrun")
                    log("playback outran synthesis — pausing until it catches up")
                    continue

                # Reconcile with mpv's ACTUAL pause state. Kurt's headphone
                # play/pause button and the desktop player widget reach mpv
                # directly over MPRIS — we never see a request for them. Without
                # this the daemon keeps reporting "playing" for a reading he
                # paused himself, /status lies to the agent, and (the one that
                # actually bites) /duck would treat it as playing, duck it, and
                # /unduck would then resume an article Kurt had deliberately
                # stopped. Attributing an outside pause to "user" is what makes
                # it survive a voice round.
                if not idle:
                    mpv_paused = bool(get_prop("pause", False))
                    if job.state == "playing" and mpv_paused:
                        job.position = current_position(job)
                        job.state = "paused"
                        job.pause_reason = "user"
                        job.suspended = False
                        job.save()
                        log("paused from outside (media key / desktop player)")
                        continue
                    if job.state == "paused" and not mpv_paused:
                        job.state = "playing"
                        job.pause_reason = None
                        job.suspended = False
                        job.save()
                        log("resumed from outside (media key / desktop player)")
                        continue

                if job.pause_reason == "underrun":
                    if done_synth or job.chunks_ready >= _underrun_target(job):
                        try:
                            rebuild_playlist(job, job.position)
                            _resume(job)
                            log("synthesis caught up — resuming")
                        except Exception as e:
                            log(f"could not resume after underrun: {e}")
                    continue

                if job.pause_reason == "tts-down" and tts_up():
                    _resume(job)
                    log("TTS engine back — resuming")
                    continue

                # music interlock: Kurt asked for a song mid-article
                if job.state == "playing" and not job.music_ducked:
                    st = music_call("/status")
                    if st and st.get("state") == "playing":
                        _pause(job, "music")
                        log("music started — pausing the reading")
                elif job.pause_reason == "music":
                    st = music_call("/status")
                    if not st or st.get("state") != "playing":
                        _resume(job)
                        log("music stopped — resuming the reading")

                if time.time() - last_save > 5:
                    job.save()
                    last_save = time.time()
        except Exception as e:
            log(f"monitor: {type(e).__name__}: {e}")


def _underrun_target(job):
    off, _ = cum_offsets(job)
    played = 0
    for i, o in enumerate(off):
        if o <= job.position:
            played = i
    return min(len(job.chunks), played + PREBUFFER + 1)


# =============================== eviction ===================================

def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def evict():
    """Startup + on `done` + hourly. The ticket's "discard after a full listen,
    with a grace period" is the GRACE_S rule."""
    try:
        with _lock:
            keep_id = JOB.id if JOB else None
        entries = []
        for d in CACHE.glob("*/"):
            meta = d / "job.json"
            if not meta.exists():
                continue
            try:
                data = json.loads(meta.read_text())
            except Exception:
                shutil.rmtree(d, ignore_errors=True)
                continue
            entries.append((d, data))

        def drop(d, why):
            if d.name == keep_id:
                return False
            shutil.rmtree(d, ignore_errors=True)
            log(f"evicted {d.name} ({why})")
            return True

        now = time.time()
        survivors = []
        for d, data in entries:
            fin = data.get("finished_at")
            if data.get("state") == "done" and fin and now - fin > GRACE_S:
                if drop(d, "listened, past the grace period"):
                    continue
            if now - (data.get("updated_at") or 0) > KEEP_S:
                if drop(d, "older than a week"):
                    continue
            survivors.append((d, data))

        survivors.sort(key=lambda e: e[1].get("updated_at") or 0, reverse=True)
        for d, _ in survivors[MAX_JOBS:]:
            drop(d, f"more than {MAX_JOBS} articles kept")
        survivors = survivors[:MAX_JOBS]

        while sum(dir_size(d) for d, _ in survivors) > MAX_CACHE_BYTES and len(survivors) > 1:
            d, _ = survivors.pop()
            drop(d, "cache size cap")
    except Exception as e:
        log(f"eviction: {type(e).__name__}: {e}")


def evict_loop():
    while True:
        time.sleep(3600)
        evict()


def adopt_newest():
    """After a restart, adopt the newest unfinished article as current, paused
    at its saved position. /resume then rebuilds the playlist from the chunks
    still on disk, so `systemctl --user restart horus-read` mid-article is
    seamless instead of losing 45 minutes of synthesis."""
    global JOB
    best = None
    for d in CACHE.glob("*/"):
        meta = d / "job.json"
        if not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text())
        except Exception:
            continue
        if data.get("state") in ("done", "idle"):
            continue
        if best is None or (data.get("updated_at") or 0) > (best[1].get("updated_at") or 0):
            best = (d, data)
    if not best:
        return
    try:
        job = Job.load(best[0])
        if job.chunks_ready > 0:
            job.state = "paused"
            job.pause_reason = "restart"
        else:
            job.state = "ready"
            job.pause_reason = None
        JOB = job
        job.save()
        log(f"adopted {job.id!r} ({job.state}, position {job.position:.0f}s)")
    except Exception as e:
        log(f"could not adopt a previous job: {type(e).__name__}: {e}")


# ================================ status ====================================

def status():
    job = JOB
    if job is None:
        return {"state": "idle", "job_id": None, "started_at": _started_at}
    chars = len(job.text)
    dur = job.duration()
    pos = job.position if job.state != "playing" else current_position(job)
    return {
        "state": job.state,
        "job_id": job.id,
        "url": job.url,
        "final_url": job.final_url,
        "title": job.title,
        "source": job.source,
        "chars": chars,
        "raw_chars": job.raw_chars,
        "words": len(job.text.split()),
        "position": round(pos, 1),
        "duration": round(dur, 1),
        "duration_final": job.duration_final(),
        "percent": round(100.0 * pos / dur, 1) if dur > 0 else 0.0,
        "chunk": _chunk_at(job, pos),
        "chunks_total": len(job.chunks),
        "chunks_ready": job.chunks_ready,
        "volume": get_prop("volume") if mpv_alive() else None,
        "pause_reason": job.pause_reason,
        "started_at": _started_at,
        "trims": job.trims,
        "warnings": job.warnings,
        "error": job.error,
    }


def _chunk_at(job, pos):
    off, _ = cum_offsets(job)
    idx = 0
    for i, o in enumerate(off):
        if o <= pos:
            idx = i
    return idx


def preview_body(job, warnings):
    text = job.text
    short = len(text) <= SHORT_TEXT
    return {
        "url": job.url, "final_url": job.final_url, "title": job.title,
        "source": job.source, "chars": len(text), "raw_chars": job.raw_chars,
        "words": len(text.split()),
        "estimated_minutes": round(len(text) * SEC_PER_CHAR / 60),
        "head": text if short else text[:HEAD_CHARS],
        "tail": None if short else text[-TAIL_CHARS:],
        "trims": job.trims,
        "warning": " | ".join(warnings) if warnings else None,
        "state": job.state,
    }


# ================================= HTTP =====================================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(f"{self.command} {self.path} {args[1] if len(args) > 1 else ''}")

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n == 0:
            return {}
        return json.loads(self.rfile.read(n))

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        q = urllib.parse.parse_qs(qs)
        try:
            if path == "/status":
                with _lock:
                    self.send_json(status())
            elif path == "/jobs":
                self.send_json({"jobs": list_jobs()})
            elif path == "/text":
                # debug only, deliberately NOT wired into the tool: the whole
                # point of this daemon is that the article never enters the
                # container's context.
                with _lock:
                    if JOB is None:
                        raise ValueError("no article loaded")
                    start = int(q.get("from", ["0"])[0])
                    ln = min(4000, int(q.get("len", ["1000"])[0]))
                    self.send_json({"text": JOB.text[start:start + ln]})
            else:
                self.send_json({"error": "not found"}, 404)
        except (ValueError, FileNotFoundError) as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        try:
            body = self.read_body()
            if self.path == "/preview":
                self.send_json(do_preview(body))
                return
            if self.path == "/speak":
                # deliberately OUTSIDE _lock: this blocks for seconds while the
                # first chunk synthesizes, and /status must stay answerable
                with _lock:
                    job = JOB
                    if job is None:
                        raise ValueError("no article loaded — call /preview first")
                    if job.state in ("synthesizing", "playing"):
                        raise ValueError("already reading — /stop first")
                start_speaking(job, body.get("voice"), body.get("speed"), body.get("volume"))
                with _lock:
                    self.send_json(status())
                return

            with _lock:
                job = JOB
                if self.path in ("/pause", "/resume", "/toggle", "/duck", "/unduck",
                                 "/stop", "/seek", "/volume") and job is None:
                    raise ValueError("no article loaded")

                if self.path == "/pause":
                    if job.state == "playing":
                        _pause(job, "user")
                elif self.path == "/resume":
                    _do_resume(job)
                elif self.path == "/toggle":
                    if job.state == "playing":
                        _pause(job, "user")
                    elif job.state == "paused":
                        _do_resume(job)
                elif self.path == "/duck":
                    ducked = job.state == "playing"
                    if ducked:
                        _pause(job, "voice")
                    out = status()
                    out["ducked"] = ducked
                    self.send_json(out)
                    return
                elif self.path == "/unduck":
                    # only resume what WE paused for a voice round: an article
                    # Kurt paused himself with the headphone button must
                    # survive a voice round untouched. This is exactly what
                    # /duck + /unduck buy over music's blunt resume.
                    if job.state == "paused" and job.pause_reason == "voice":
                        _do_resume(job)
                elif self.path == "/stop":
                    do_stop(job, bool(body.get("discard")))
                elif self.path == "/seek":
                    do_seek(job, body)
                elif self.path == "/volume":
                    v = body.get("value")
                    if not isinstance(v, (int, float)):
                        raise ValueError("volume needs numeric 'value' 0-100")
                    ensure_mpv()
                    mpv_cmd("set_property", "volume", max(0, min(100, v)))
                elif self.path == "/load":
                    do_load(str(body.get("job_id") or ""))
                else:
                    return self.send_json({"error": "not found"}, 404)
                self.send_json(status())
        except (ValueError, FileNotFoundError) as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)


def _do_resume(job):
    """Resume regardless of pause_reason. Rebuilds the playlist first when mpv
    has nothing loaded — the restart-recovery path."""
    if job.state != "paused":
        return
    if not mpv_alive() or get_prop("idle-active", True):
        rebuild_playlist(job, job.position)
        if job.chunks_ready < len(job.chunks) and (job.worker is None or not job.worker.is_alive()):
            job.cancelled = False
            job.worker = threading.Thread(target=synth_worker, args=(job,), daemon=True)
            job.worker.start()
    _resume(job)


def do_stop(job, discard):
    global JOB
    job.cancelled = True
    stop_mpv()
    if job.music_ducked:
        music_call("/resume", "POST")
        job.music_ducked = False
    job.position = 0.0
    if discard:
        shutil.rmtree(job.dir, ignore_errors=True)
        JOB = None
        return
    job.state = "ready"
    job.pause_reason = None
    job.save()


def do_seek(job, body):
    if job.state not in ("playing", "paused"):
        raise ValueError("nothing is being read")
    if isinstance(body.get("chunks"), (int, float)):
        off, _ = cum_offsets(job)
        idx = _chunk_at(job, job.position) + int(body["chunks"])
        idx = max(0, min(len(off) - 1, idx))
        seek_to(job, off[idx])
        return
    if isinstance(body.get("absolute"), (int, float)):
        seek_to(job, float(body["absolute"]))
        return
    secs = body.get("seconds")
    if not isinstance(secs, (int, float)):
        raise ValueError("seek needs 'seconds' (+/-), 'absolute' or 'chunks'")
    if body.get("absolute") is True:
        seek_to(job, float(secs))
    else:
        seek_to(job, current_position(job) + float(secs))


def list_jobs():
    out = []
    for d in sorted(CACHE.glob("*/")):
        meta = d / "job.json"
        if not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text())
        except Exception:
            continue
        out.append({
            "job_id": data.get("id"), "title": data.get("title"),
            "url": data.get("url"), "state": data.get("state"),
            "position": round(data.get("position") or 0.0, 1),
            "duration": round(sum(data.get("durations") or []), 1),
            "updated_at": data.get("updated_at"),
        })
    out.sort(key=lambda j: j.get("updated_at") or 0, reverse=True)
    return out


def do_load(job_id):
    global JOB
    d = CACHE / job_id
    if not job_id or not (d / "job.json").exists():
        raise FileNotFoundError(f"no such article: {job_id!r} (see /jobs)")
    if JOB is not None and JOB.state in ("playing", "synthesizing"):
        do_stop(JOB, False)
    job = Job.load(d)
    job.state = "paused" if job.chunks_ready else "ready"
    job.pause_reason = "restart" if job.chunks_ready else None
    JOB = job
    job.save()


def do_preview(body):
    """Fetch (only when needed) + trim + sample. Refetches ONLY when the URL
    changes or force=true: raw.txt keeps the pre-trim extraction, so
    preview -> trim -> re-preview -> trim -> speak is network-free after the
    first call. That is what makes the mandated inspect/trim loop cheap enough
    that the model will actually use it."""
    global JOB, _fetching
    url = body.get("url")
    trims = {
        "skip_head_chars": int(body.get("skip_head_chars") or 0),
        "trim_tail_chars": int(body.get("trim_tail_chars") or 0),
        "start_after": body.get("start_after") or None,
        "stop_before": body.get("stop_before") or None,
    }
    with _lock:
        job = JOB
        need_fetch = bool(body.get("force")) or job is None or (url and url != job.url)
        if need_fetch and not url:
            raise ValueError("no article loaded — pass a url")
        if need_fetch:
            if _fetching:
                raise ValueError("already fetching an article — try again in a moment")
            _fetching = True
            if job is not None and job.state in ("playing", "synthesizing"):
                do_stop(job, False)

    if need_fetch:
        try:
            with _lock:
                if JOB is not None:
                    JOB.state = "fetching"
            log(f"fetching {url}")
            final_url, title, text, source = fetch(url)
            job_id = time.strftime("%Y%m%d-%H%M%S-") + slug(final_url)
            job = Job(job_id, url, final_url, title, source, len(text))
            job.dir.mkdir(parents=True, exist_ok=True)
            raw_path(job).write_text(text)
            log(f"extracted {len(text)} chars from {final_url} ({source})")
        finally:
            with _lock:
                _fetching = False
        with _lock:
            JOB = job
            threading.Thread(target=evict, daemon=True).start()

    with _lock:
        job = JOB
        job.trims = trims
        job.text, warnings = apply_trims(raw_path(job).read_text(), trims)
        if not job.text.strip():
            raise ValueError("those trims leave nothing to read")
        if job.state in ("fetching", "idle"):
            job.state = "ready"
        job.save()
        return preview_body(job, warnings)


def kill_stale_mpv():
    """A previous daemon instance's mpv outlives a plain SIGTERM (systemd's
    cgroup kill catches it, a hand-started daemon's does not). If one is still
    holding our IPC socket it is still PLAYING — spawning a second mpv would
    give Kurt two overlapping readings. Ask the old one to quit."""
    if not os.path.exists(MPV_SOCK):
        return
    try:
        mpv_cmd("quit")
        log("told a leftover mpv from a previous run to quit")
    except Exception:
        pass
    try:
        os.unlink(MPV_SOCK)
    except OSError:
        pass


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    kill_stale_mpv()
    evict()
    adopt_newest()
    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=evict_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"horus-read: serving on 127.0.0.1:{PORT} (cache {CACHE})")
    try:
        srv.serve_forever()
    finally:
        if mpv_alive():
            _mpv.terminate()


if __name__ == "__main__":
    main()
