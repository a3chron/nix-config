#!/usr/bin/env bash
# STT -> agent -> TTS, one shot per utterance. Invoked by horus-ptt.py with
# the recorded wav as $1. PATH (whisper-cli, piper, pw-play, jq) is provided
# by the voiceRespond wrapper in voice.nix.
set -uo pipefail

sounds=/run/current-system/sw/share/sounds/freedesktop/stereo
whisper_model=/var/lib/llm/models/ggml-large-v3-turbo.bin
piper_voice=/var/lib/llm/models/piper-en_US-lessac-medium.onnx
wav="$1"

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

# strip whisper noise markers like [BLANK_AUDIO], (bell)
text=$(whisper-cli -m "$whisper_model" -f "$wav" --language en --no-timestamps 2>/dev/null \
	| sed -E 's/\[[^]]*\]//g; s/\([^)]*\)//g; s/^ +| +$//g' | tr '\n' ' ')
text=$(echo "$text" | sed -E 's/^ +| +$//g')
echo "heard: $text"
if [ -z "${text// /}" ]; then
	play "$sounds/dialog-warning.oga" # didn't catch anything
	exit 0
fi

# frame the query: STT mishears words, and the answer gets read aloud by TTS
prompt="[Voice message from Kurt, speech-to-text may have misheard words — interpret \
phonetically similar words from context (check memory/INDEX.md for topics). Answer in \
1-3 short conversational sentences, no lists or markdown — it will be read aloud.] $text"

# absolute machinectl path: the NOPASSWD sudoers rule matches exactly this
# JSON events -> just the assistant text parts, ANSI-free by construction
reply=$(/run/wrappers/bin/sudo -n /run/current-system/sw/bin/machinectl shell horus@horus /run/current-system/sw/bin/bash -c \
	"cd /home/horus/work && opencode run --format json $(printf '%q' "$prompt") 2>/dev/null" \
	| grep '^{' | jq -rs 'map(select(.type=="text") | .part.text) | join(" ")' 2>/dev/null || true)
echo "reply: $reply"
if [ -z "${reply// /}" ]; then
	play "$sounds/dialog-error.oga"
	exit 0
fi

# strip markdown so the TTS doesn't say "asterisk asterisk"
spoken=$(echo "$reply" | sed -E \
	-e 's/\*\*([^*]+)\*\*/\1/g' \
	-e 's/\*([^*]+)\*/\1/g' \
	-e 's/__([^_]+)__/\1/g' \
	-e 's/`+([^`]+)`+/\1/g' \
	-e 's/^#+ +//' \
	-e 's/^ *[-*+] +//' \
	-e 's/\[([^]]+)\]\([^)]*\)/\1/g' \
	-e 's~https?://[^ )]+~ link ~g')

echo "$spoken" | piper --model "$piper_voice" --output_file /tmp/horus-reply.wav
play /tmp/horus-reply.wav
