#!/usr/bin/env bash
# STT -> agent -> TTS, one shot per utterance. Invoked by horus-ptt.py with
# the recorded wav as $1. PATH (whisper-cli, piper, pw-play, pactl, jq) is
# provided by the voiceRespond wrapper in voice.nix.
#
# Replies STREAM: each text part the agent emits is spoken as soon as it
# arrives — so a "let me check..." acknowledgment plays while tools still run,
# and long answers start speaking after the first chunk.
set -uo pipefail

sounds=/run/current-system/sw/share/sounds/freedesktop/stereo
whisper_model=/var/lib/llm/models/ggml-large-v3-turbo.bin
piper_voice=/var/lib/llm/models/piper-en_US-lessac-medium.onnx
wav="$1"
tmpdir=$(mktemp -d /tmp/horus-voice.XXXXXX)

# Music integration (horus-music daemon, music.nix): two rules keep songs and
# spoken replies from talking over each other.
#  1. A song that STARTED during this round IS the answer — every later TTS
#     part (and the no-answer fallbacks) is skipped.
#  2. Music that was already playing BEFORE the round is paused for the first
#     spoken part ("ducked") and resumed when the round ends.
music=http://127.0.0.1:8877
round_start=$(date +%s)
music_state() { curl -sf -m 1 "$music/status" 2>/dev/null | jq -r '.state // "stopped"'; }
music_started_this_round() {
	local at
	at=$(curl -sf -m 1 "$music/status" 2>/dev/null \
		| jq -r 'if .state != "stopped" then (.started_at // 0 | floor) else empty end')
	[ -n "$at" ] && [ "$at" -ge "$round_start" ]
}

cleanup() {
	if [ -f "$tmpdir/ducked" ]; then
		curl -sf -m 1 -X POST "$music/resume" >/dev/null 2>&1
	fi
	rm -rf "$tmpdir"
}
trap cleanup EXIT
# paddle-cancel SIGTERMs our process group (horus-ptt.py, with a grace period
# before SIGKILL) — route it through exit so cleanup still resumes ducked music
trap 'exit 143' TERM INT

# voice rounds share one opencode session while they come <30 min apart, so
# follow-up questions keep the previous exchange in context (and the prompt
# cache warm). The session id is captured from the event stream below.
sess_file=/tmp/horus-voice-session
sess_args=""
if [ -f "$sess_file" ] && [ -n "$(find "$sess_file" -mmin -30 2>/dev/null)" ]; then
	sess_args="--session $(cat "$sess_file")"
else
	rm -f "$sess_file"
fi

# play to the headphones explicitly: right after the HFP->A2DP flip the
# *default* sink can briefly point elsewhere (e.g. easyeffects) and the
# reply would go there silently
play() {
	local sink
	# grep/cut, not awk: awk is not on the voice unit's PATH
	sink=$(pactl list sinks short 2>/dev/null | grep -m1 bluez_output | cut -f2)
	if [ -n "$sink" ]; then
		pw-play --target "$sink" "$1"
	else
		pw-play "$1"
	fi
}

# markdown -> speakable text, then synthesize + play (blocking, so parts queue)
speak() {
	# a song the agent just started is the answer — don't talk over it
	if music_started_this_round; then
		echo "music started this round — skipping TTS"
		return 0
	fi
	# duck pre-existing music while Horus speaks (resumed in cleanup)
	if [ ! -f "$tmpdir/ducked" ] && [ "$(music_state)" = "playing" ]; then
		curl -sf -m 1 -X POST "$music/pause" >/dev/null 2>&1 && touch "$tmpdir/ducked"
	fi
	local spoken
	spoken=$(echo "$1" | sed -E \
		-e 's/\*\*([^*]+)\*\*/\1/g' \
		-e 's/\*([^*]+)\*/\1/g' \
		-e 's/__([^_]+)__/\1/g' \
		-e 's/`+([^`]+)`+/\1/g' \
		-e 's/^#+ +//' \
		-e 's/^ *[-*+] +//' \
		-e 's/\[([^]]+)\]\([^)]*\)/\1/g' \
		-e 's~https?://[^ )]+~ link ~g' \
		-e 's/([0-9]+) *- *([0-9]+)/\1 to \2/g')
	[ -z "${spoken// /}" ] && return 0
	# Kokoro (am_michael); Piper stays as audible fallback if it ever fails
	local t0
	t0=$(date +%s%3N)
	if ! horus-tts --out "$tmpdir/part.wav" "$spoken" 2>/dev/null; then
		echo "kokoro failed, falling back to piper"
		echo "$spoken" | piper --model "$piper_voice" --output_file "$tmpdir/part.wav" 2>/dev/null
	fi
	echo "synth: $(( $(date +%s%3N) - t0 ))ms"
	# First spoken part of the round: the A2DP link was just re-created by the
	# HFP->A2DP profile switch and sat idle through STT+thinking, so BT drops the
	# first ~300ms on resume — which is exactly Horus's opening word ("On it,").
	# Prepend 0.5s of silence (matching the wav's own format) so the ramp eats
	# silence instead. Only the first part per round pays it; later parts play on
	# the already-warm stream.
	if [ ! -f "$tmpdir/primed" ]; then
		touch "$tmpdir/primed"
		python3 -c 'import sys,wave; p=sys.argv[1]; r=wave.open(p,"rb"); pr=r.getparams(); fr=r.readframes(r.getnframes()); r.close(); lead=b"\x00"*(pr.sampwidth*pr.nchannels*int(pr.framerate*0.5)); w=wave.open(p,"wb"); w.setparams(pr); w.writeframes(lead+fr); w.close()' "$tmpdir/part.wav" 2>/dev/null || true
	fi
	play "$tmpdir/part.wav"
}

# strip whisper noise markers like [BLANK_AUDIO], (bell)
text=$(whisper-cli -m "$whisper_model" -f "$wav" --language en --no-timestamps 2>/dev/null \
	| sed -E 's/\[[^]]*\]//g; s/\([^)]*\)//g; s/^ +| +$//g' | tr '\n' ' ')
text=$(echo "$text" | sed -E 's/^ +| +$//g')
echo "heard: $text"
if [ -z "${text// /}" ]; then
	play "$sounds/dialog-warning.oga" # didn't catch anything
	exit 0
fi

# frame the query. The announce mandate sits AFTER the transcript: tested
# (2026-07-04) that trailing placement makes the model reliably emit the
# announce as its own text step BEFORE tool calls, instead of pasting it
# retroactively onto the final answer.
prompt="[Voice message from Kurt, speech-to-text may have misheard words — interpret \
phonetically similar words from context (check memory/INDEX.md for topics).] \
$text \
[IMPORTANT: This is voice — your words are read aloud by TTS. If your first tool call will take \
a moment — web search, Linear, PDF, history, web fetch — BEGIN with one short spoken sentence \
saying what you're doing (like 'On it, checking Linear.'), as its own text step before that tool \
call, so Kurt isn't left in silence. For instant local actions (lights, sending a message, a \
quick status) skip the preamble entirely — just do it and give the result. Answer SHORT and \
conversational: 1-3 spoken sentences, absolutely no lists, no markdown, no issue-ID dumps. \
Every word costs TTS synth time and Kurt's listening time. After an action, confirm in a few \
words ('Done.', 'Lights are white.') — do NOT read back parameters, numbers or settings Kurt \
did not ask about. Save the details for when the request was an actual question.]"

# absolute machinectl path: the NOPASSWD sudoers rule matches exactly this.
# JSON events stream line-by-line; speak each text part as it arrives.
# Tool/error events become markers so a run that dies mid-tools (e.g. an
# oversized fetch blowing the context) is detected instead of ending silent:
# "answered" = some text arrived AFTER the last tool call.
/run/wrappers/bin/sudo -n /run/current-system/sw/bin/machinectl shell horus@horus /run/current-system/sw/bin/bash -c \
	"cd /home/horus/work && timeout 480 opencode run --format json $sess_args $(printf '%q' "$prompt") 2>/dev/null" \
	| stdbuf -oL tr -d '\r' | grep --line-buffered '^{' \
	| jq --unbuffered -rc '
		if .type=="text" then "T " + (.part.text | gsub("\n"; " "))
		elif .type=="tool_use" then "U " + (.part.tool // "?") + (if (.part.state.input.filePath // "") != "" then "\t" + .part.state.input.filePath else "" end)
		elif .type=="error" then "E " + (tostring | .[0:200])
		elif .type=="step_start" then "S " + (.sessionID // empty)
		else empty end' 2>/dev/null \
	| while IFS= read -r line; do
		kind="${line:0:1}"
		payload="${line:2}"
		case "$kind" in
		T)
			[ -z "${payload// /}" ] && continue
			echo "reply part: $payload"
			touch "$tmpdir/spoke" "$tmpdir/answered"
			speak "$payload"
			;;
		U)
			echo "tool: $payload"
			rm -f "$tmpdir/answered"
			;;
		E)
			echo "agent error: $payload"
			;;
		S)
			[ -n "$payload" ] && printf '%s' "$payload" > "$sess_file"
			;;
		esac
	done

if music_started_this_round; then
	# the song is the answer — a round with no (spoken) text is expected here,
	# so neither fallback applies
	echo "round ended with music playing"
elif [ ! -f "$tmpdir/spoke" ]; then
	echo "no reply text received"
	# a stale/broken session id would keep failing every round — drop it
	[ -n "$sess_args" ] && rm -f "$sess_file"
	speak "Sorry, something went wrong — I didn't get an answer back."
elif [ ! -f "$tmpdir/answered" ]; then
	echo "round died mid-tools, no final answer"
	speak "Sorry — something broke while I was working on that, and I didn't get a result back."
fi
