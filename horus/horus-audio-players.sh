# The one place that knows the host's audio producers, for the BASH callers
# (horus-voice-respond.sh). The python callers use horus_players.py — keep the
# two in sync. Needs curl + jq on PATH (voice.nix:49 provides both).
#
# See horus_players.py's header for WHY music and read are ducked differently:
# music has no /duck (horus-music.py is deliberately untouched), read has
# /duck + /unduck with a pause_reason so a reading Kurt paused HIMSELF is not
# resumed by a voice round.
HORUS_MUSIC_URL=http://127.0.0.1:8877
HORUS_READ_URL=http://127.0.0.1:8878

# -m 3, not 1: under a cold model prefill a daemon can take >1s to answer, and
# a timed-out probe silently disabled ducking (Horus talked over the song).
horus_player_state() {
	curl -sf -m 3 "$1/status" 2>/dev/null | jq -r '.state // "stopped"'
}

# true if ANY player started something at/after $1 — "the audio IS the answer"
horus_started_since() {
	local base at
	for base in "$HORUS_MUSIC_URL" "$HORUS_READ_URL"; do
		at=$(curl -sf -m 3 "$base/status" 2>/dev/null \
			| jq -r 'if (.state != null and .state != "stopped" and .state != "idle")
			         then (.started_at // 0 | floor) else empty end')
		[ -n "$at" ] && [ "$at" -ge "$1" ] && return 0
	done
	return 1
}

# $1 = marker dir (the round's tmpdir). Records what we paused so the matching
# unduck only resumes that.
horus_duck_all() {
	if [ ! -f "$1/ducked-music" ] && [ "$(horus_player_state "$HORUS_MUSIC_URL")" = playing ]; then
		curl -sf -m 3 -X POST "$HORUS_MUSIC_URL/pause" >/dev/null 2>&1 && touch "$1/ducked-music"
	fi
	if [ ! -f "$1/ducked-read" ]; then
		curl -sf -m 3 -X POST "$HORUS_READ_URL/duck" 2>/dev/null \
			| jq -e '.ducked' >/dev/null 2>&1 && touch "$1/ducked-read"
	fi
	: # never fail the caller: ducking is best-effort, the reply matters more
}

horus_unduck_all() {
	[ -f "$1/ducked-music" ] && curl -sf -m 3 -X POST "$HORUS_MUSIC_URL/resume" >/dev/null 2>&1
	[ -f "$1/ducked-read" ] && curl -sf -m 3 -X POST "$HORUS_READ_URL/unduck" >/dev/null 2>&1
	:
}
