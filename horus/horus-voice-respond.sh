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
trap 'rm -rf "$tmpdir"' EXIT

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
[IMPORTANT: This is voice — your words are read aloud by TTS. ORDER OF OPERATIONS: if you will \
use ANY tool, your turn must BEGIN with a text-only step: one short sentence saying what you're \
doing (like 'On it, checking Linear.') — send that sentence FIRST, before your first tool call, \
never merged into the final answer. Kurt hears it immediately; silence while tools run feels \
broken. Then call tools. Then answer SHORT and conversational: 1-3 spoken sentences, absolutely \
no lists, no markdown, no issue-ID dumps.]"

# absolute machinectl path: the NOPASSWD sudoers rule matches exactly this.
# JSON events stream line-by-line; speak each text part as it arrives.
# Tool/error events become markers so a run that dies mid-tools (e.g. an
# oversized fetch blowing the context) is detected instead of ending silent:
# "answered" = some text arrived AFTER the last tool call.
/run/wrappers/bin/sudo -n /run/current-system/sw/bin/machinectl shell horus@horus /run/current-system/sw/bin/bash -c \
	"cd /home/horus/work && timeout 240 opencode run --format json $sess_args $(printf '%q' "$prompt") 2>/dev/null" \
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

if [ ! -f "$tmpdir/spoke" ]; then
	echo "no reply text received"
	# a stale/broken session id would keep failing every round — drop it
	[ -n "$sess_args" ] && rm -f "$sess_file"
	speak "Sorry, something went wrong — I didn't get an answer back."
elif [ ! -f "$tmpdir/answered" ]; then
	echo "round died mid-tools, no final answer"
	speak "Sorry — something broke while I was working on that, and I didn't get a result back."
fi
