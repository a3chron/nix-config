#!/usr/bin/env bash
#
# Statusline wrapper that persists Claude Code's rate-limit block for the
# chronogrid usage meter, then hands the payload to the real status line.
#
# ── Why this exists ─────────────────────────────────────────────────────────
# The 5-hour and 7-day windows are Claude Code subscription limits. They are
# NOT in the Anthropic API (the anthropic-ratelimit-* headers are per-minute
# request/token limits, a different thing), `/usage` prints text with no JSON
# mode, and nothing under ~/.claude persists them. The one place they appear is
# the JSON Claude Code pipes to a status line on stdin — which is consumed and
# thrown away. So a dashboard can only see them if something writes them down,
# and this is that something.
#
# ── Why a wrapper, and why it lives in nix ──────────────────────────────────
# It cannot be an edit to statusline.sh: that path is a store symlink and is
# read-only. So this sits in front of it and leaves it alone.
#
# It was a hand-written ~/.claude/usage-sidecar.sh with settings.json pointed
# at it by hand, and on 2026-09-22 a `home-manager switch` silently undid that:
# the claudeStatusline activation script rewrites `.statusLine` unconditionally
# and put the command back to statusline.sh. Nothing errored — the status line
# kept drawing its own bar from the same payload — so the only symptom was the
# chronogrid meter freezing at its last reading for half a day. Hence both
# halves now live here: the activation script points at this file, and this
# file is what runs.
#
# ── Correctness notes ───────────────────────────────────────────────────────
# - No jq. The real status line gets jq from a PATH it exports itself, which
#   this runs before; a bash glob is enough to test for the key and the whole
#   payload is handed to the reader to parse.
# - The `rate_limits` key is ABSENT until the first API response of a session,
#   so writing unconditionally would blank the meter every time a session
#   starts. Only a payload that actually has the block replaces the file.
# - Temp file + mv, so a reader never catches a half-written file.
# - The file's mtime is the reading's timestamp; nothing needs to inject one.
# - Every failure is swallowed and the status line still prints. A status line
#   that errors is a broken prompt, and this is a side errand.
# - Chains through the stable ~/.claude/statusline.sh path rather than a store
#   path, so this stays runnable standalone
#   (`echo '{}' | ./home/claude/usage-sidecar.sh`). Home-manager owns that
#   symlink in the same generation, so it cannot dangle.

input=$(cat)

out="$HOME/.claude/usage-limits.json"
tmp="$out.tmp.$$"

case "$input" in
*'"rate_limits"'*)
  {
    printf '%s' "$input" >"$tmp" 2>/dev/null &&
      mv -f "$tmp" "$out" 2>/dev/null
  } || rm -f "$tmp" 2>/dev/null
  ;;
esac

printf '%s' "$input" | exec "$HOME/.claude/statusline.sh"
