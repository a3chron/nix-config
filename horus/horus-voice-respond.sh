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

# play to the headphones explicitly: right after the HFP->A2DP flip the
# *default* sink can briefly point elsewhere (e.g. easyeffects) and the
# reply would go there silently
play() {
	local sink
	sink=$(pactl list sinks short 2>/dev/null | awk '/bluez_output/ {print $2; exit}')
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
	echo "$spoken" | piper --model "$piper_voice" --output_file "$tmpdir/part.wav" 2>/dev/null
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

# frame the query: STT mishears, answers get read aloud, long tasks announced
prompt="[Voice message from Kurt, speech-to-text may have misheard words — interpret \
phonetically similar words from context (check memory/INDEX.md for topics). Keep answers \
SHORT and conversational (1-3 sentences), no lists or markdown — they are read aloud by TTS. \
MANDATORY: if you are going to use ANY tool (API call, file access, search, anything that \
takes time), the VERY FIRST thing in your reply — before the first tool call — must be one \
short sentence saying what you are doing, like 'On it, checking Linear.' It is spoken to \
Kurt immediately while you work. Only skip this for instant, tool-free answers.] $text"

# absolute machinectl path: the NOPASSWD sudoers rule matches exactly this.
# JSON events stream line-by-line; speak each text part as it arrives.
/run/wrappers/bin/sudo -n /run/current-system/sw/bin/machinectl shell horus@horus /run/current-system/sw/bin/bash -c \
	"cd /home/horus/work && timeout 240 opencode run --format json $(printf '%q' "$prompt") 2>/dev/null" \
	| tr -d '\r' | grep --line-buffered '^{' \
	| jq --unbuffered -rc 'select(.type=="text") | .part.text | gsub("\n"; " ")' 2>/dev/null \
	| while IFS= read -r part; do
		[ -z "${part// /}" ] && continue
		echo "reply part: $part"
		touch "$tmpdir/spoke"
		speak "$part"
	done

if [ ! -f "$tmpdir/spoke" ]; then
	echo "no reply text received"
	speak "Sorry, something went wrong — I didn't get an answer back."
fi
