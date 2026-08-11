# The one place that knows the host's audio producers, for the BASH callers
# (horus-voice-respond.sh). The python callers use horus_players.py — keep the
# two in sync. Needs curl + jq on PATH (voice.nix:49 provides both).
#
# See horus_players.py's header for WHY music and read are ducked differently:
# music has no /duck (horus-music.py is deliberately untouched), read has
# /duck + /unduck with a pause_reason so a reading Kurt paused HIMSELF is not
# resumed by a voice round.
#
# The MPRIS half (2026-08-10) mirrors horus_players.mpris_* — see that file for
# WHY busctl and not playerctl, and why playerctld/mpv are skipped. It exists
# because Kurt's music is usually a browser tab, which neither daemon knows.
HORUS_MUSIC_URL=http://127.0.0.1:8877
HORUS_READ_URL=http://127.0.0.1:8878
HORUS_BUSCTL=/run/current-system/sw/bin/busctl  # absolute: voice.nix's PATH has no systemd
HORUS_MPRIS_PATH=/org/mpris/MediaPlayer2
HORUS_MPRIS_IFACE=org.mpris.MediaPlayer2.Player

# bus names of the generic players running right now (see MPRIS_SKIP in
# horus_players.py for the two exclusions)
horus_mpris_players() {
	"$HORUS_BUSCTL" --user list --acquired --no-legend --no-pager 2>/dev/null \
		| awk '$1 ~ /^org\.mpris\.MediaPlayer2\./ {
			rest = substr($1, length("org.mpris.MediaPlayer2.") + 1)
			split(rest, p, ".")
			if (p[1] != "playerctld" && p[1] != "mpv") print $1
		}'
}

horus_mpris_playing() {
	"$HORUS_BUSCTL" --user get-property "$1" "$HORUS_MPRIS_PATH" \
		"$HORUS_MPRIS_IFACE" PlaybackStatus 2>/dev/null | grep -q Playing
}

horus_mpris_do() {
	"$HORUS_BUSCTL" --user call "$1" "$HORUS_MPRIS_PATH" \
		"$HORUS_MPRIS_IFACE" "$2" >/dev/null 2>&1
}

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
	if [ ! -f "$1/ducked-mpris" ]; then
		# one bus name per line, so the unduck resumes exactly these
		for player in $(horus_mpris_players); do
			horus_mpris_playing "$player" && horus_mpris_do "$player" Pause \
				&& printf '%s\n' "$player" >>"$1/ducked-mpris"
		done
	fi
	: # never fail the caller: ducking is best-effort, the reply matters more
}

horus_unduck_all() {
	[ -f "$1/ducked-music" ] && curl -sf -m 3 -X POST "$HORUS_MUSIC_URL/resume" >/dev/null 2>&1
	[ -f "$1/ducked-read" ] && curl -sf -m 3 -X POST "$HORUS_READ_URL/unduck" >/dev/null 2>&1
	if [ -f "$1/ducked-mpris" ]; then
		# Play, not PlayPause: a player Kurt restarted himself must not be
		# stopped again (same reasoning as horus_players.mpris_unduck)
		while read -r player; do
			[ -n "$player" ] && horus_mpris_do "$player" Play
		done <"$1/ducked-mpris"
		rm -f "$1/ducked-mpris"
	fi
	:
}
