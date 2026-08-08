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
# Kokoro speaking rate. 1.0 is the model default and reads a touch slow; tune
# by ear (1.1 / 1.15 / 1.25) — this file is impure, so a change takes effect on
# the next voice round with no rebuild and no restart.
tts_speed=1.15
wav="$1"
tmpdir=$(mktemp -d /tmp/horus-voice.XXXXXX)

# Audio integration (horus-music :8877, horus-read :8878): two rules keep
# playing audio and spoken replies from talking over each other.
#  1. Audio that STARTED during this round IS the answer — every later TTS
#     part (and the no-answer fallbacks) is skipped. That is true of a song and
#     equally of an article Horus started reading aloud.
#  2. Audio that was already playing BEFORE the round is paused for the first
#     spoken part ("ducked") and resumed when the round ends.
# The player table (and the fact that music has no /duck while read does) lives
# in one place, shared with the python callers' horus_players.py:
source /home/a3chron/nixos-config/horus/horus-audio-players.sh
round_start=$(date +%s)

cleanup() {
	horus_unduck_all "$tmpdir"
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
# reply would go there silently.
# This explicit targeting is deliberate and is the ONE exception to the
# "un-initiated audio goes to the default sink" rule — a voice round means the
# headphones are on by definition. See horus_deliver.py's header.
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
	# audio the agent just started (a song, or an article being read aloud) is
	# the answer — don't talk over it
	if horus_started_since "$round_start"; then
		echo "audio started this round — skipping TTS"
		return 0
	fi
	# duck pre-existing audio while Horus speaks (resumed in cleanup)
	horus_duck_all "$tmpdir"
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
	# Kokoro (am_michael); Piper stays as audible fallback if it ever fails.
	# Delete the previous part first: a stale part.wav from an earlier part
	# would otherwise be replayed and look like a successful synth.
	rm -f "$tmpdir/part.wav"
	local t0
	t0=$(date +%s%3N)
	if ! horus-tts --speed "$tts_speed" --out "$tmpdir/part.wav" "$spoken" 2>&1; then
		echo "kokoro failed, falling back to piper"
		if ! echo "$spoken" | piper --model "$piper_voice" --output_file "$tmpdir/part.wav" 2>&1; then
			echo "piper failed too"
		fi
	fi
	echo "synth: $(( $(date +%s%3N) - t0 ))ms"
	if [ ! -s "$tmpdir/part.wav" ]; then
		echo "TTS produced no audio — part NOT spoken"
		return 1
	fi
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
	if ! play "$tmpdir/part.wav"; then
		echo "audio playback failed (pw-play) — part NOT spoken"
		return 1
	fi
	return 0
}

# harness internals must never be spoken: opencode's compaction summary and
# its "Continue if you have next steps..." nudge arrive as normal text parts
# (same filter as bridge/server.js dropReason — keep the two in sync)
is_harness_internal() {
	case "$1" in "Continue if you have next steps"*) return 0 ;; esac
	local hits
	hits=$(printf '%s' "$1" | grep -oE '## (Goal|Constraints & Preferences|Progress|Key Decisions|Next Steps|Critical Context|Relevant Files)' 2>/dev/null | wc -l)
	[ "${hits:-0}" -ge 2 ]
}

# STT. Keep whisper's rc and stderr: a missing/corrupt model must sound and
# log different from "you said nothing" — Kurt would otherwise debug his mic.
if ! whisper-cli -m "$whisper_model" -f "$wav" --language en --no-timestamps \
		>"$tmpdir/whisper.txt" 2>"$tmpdir/whisper.err"; then
	echo "whisper FAILED: $(tr '\n' ' ' < "$tmpdir/whisper.err" | tail -c 300)"
	play "$sounds/dialog-error.oga" # error tone, not the "didn't catch it" one
	exit 1
fi
# strip whisper noise markers like [BLANK_AUDIO], (bell)
text=$(sed -E 's/\[[^]]*\]//g; s/\([^)]*\)//g; s/^ +| +$//g' "$tmpdir/whisper.txt" | tr '\n' ' ')
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
conversational: 1-3 spoken sentences, absolutely no lists, no tables, no markdown, no issue-ID dumps. \
Every word costs TTS synth time and Kurt's listening time. After an action, confirm in a few \
words ('Done.', 'Lights are white.') — do NOT read back parameters, numbers or settings Kurt \
did not ask about. Save the details for when the request was an actual question.]"

# absolute machinectl path: the NOPASSWD sudoers rule matches exactly this.
# JSON events stream line-by-line; speak each text part as it arrives.
# machinectl gives a PTY (stdout+stderr merged, \r injected) and does NOT
# propagate the inner exit code — so the container wrapper keeps opencode's
# stderr in a temp file, replays it '!'-prefixed after the run, and reports
# the exit status as a %%EXIT sentinel line. jq classifies every line:
#   T text  U tool ok (name\tdetail\tms)  V tool failed  F step finish reason
#   E stream error  S session id  R stderr/garbage  X exit code
# A healthy run's last step finishes with reason "stop" — anything else
# (tool-calls after a permission auto-reject, length) means it died owing
# work; the marker files below let the post-loop check speak up about it.
# OPENCODE_PERMISSION: unattended run — "ask" rules become "deny" so the model
# gets a tool error it can explain instead of an auto-reject killing the run
/run/wrappers/bin/sudo -n /run/current-system/sw/bin/machinectl shell horus@horus /run/current-system/sw/bin/bash -c \
	"cd /home/horus/work && err=\$(mktemp); OPENCODE_PERMISSION=\$(cat .opencode/unattended-permission.json 2>/dev/null) timeout 600 opencode run --format json $sess_args $(printf '%q' "$prompt") 2>\"\$err\"; ec=\$?; sed 's/^/!/' \"\$err\" | tail -n 20; rm -f \"\$err\"; echo \"%%EXIT \$ec\"" \
	| stdbuf -oL tr -d '\r' \
	| jq --unbuffered -Rrc '
		def clean: tostring | gsub("[\t\n\r]"; " ") | .[0:120];
		def detail($t; $i):
			if $t == "bash" then ($i.description // (($i.command // "") | tostring | .[0:60]))
			elif $t == "linear_manage" then ([$i.action, $i.identifier, $i.project, $i.title] | map(select(. != null and . != "")) | join(" "))
			elif $t == "linear_issues" then ($i.filter // "assigned")
			elif $t == "glob" or $t == "grep" then ($i.pattern // "")
			elif $t == "read" or $t == "write" or $t == "edit" then ($i.filePath // "")
			elif $t == "web_search" then ($i.query // "")
			elif $t == "web_fetch" then ($i.url // "")
			elif $t == "whatsapp_send" then ($i.to // "")
			elif $t == "music" then ([$i.action, $i.query] | map(select(. != null and . != "")) | join(" "))
			elif $t == "read_aloud" then ([$i.action, $i.url] | map(select(. != null and . != "")) | join(" "))
			elif $t == "nanoleaf" or $t == "studium" then ($i.action // "")
			elif $t == "pdf" then ($i.file // "")
			elif $t == "history" then ([$i.source, $i.since] | map(select(. != null and . != "")) | join(" "))
			elif $t == "task" then ($i.description // "")
			else "" end;
		. as $raw | try (
			fromjson
			| if .type == "text" then "T " + (.part.text | gsub("[\t\n\r]"; " "))
			elif .type == "tool_use" then
				(.part.tool // "?") as $t
				| ((.part.state.input // {}) | if type == "object" then . else {} end) as $i
				| if (.part.state.status // "") == "error"
					then "V " + $t + "\t" + ((.part.state.error // "") | clean)
					else "U " + $t + "\t" + (detail($t; $i) | clean) + "\t"
						+ (.part.state.time as $tm | if $tm != null and $tm.end != null and $tm.start != null then (($tm.end - $tm.start) | tostring) else "" end)
					end
			elif .type == "step_finish" then "F " + (.part.reason // "?")
			elif .type == "error" then "E " + (tostring | .[0:200])
			elif .type == "step_start" then "S " + (.sessionID // empty)
			else empty end
		) catch (
			if ($raw | startswith("%%EXIT ")) then "X " + $raw[7:]
			elif ($raw | startswith("!")) then (($raw[1:] | clean) as $e | if ($e | gsub(" "; "")) == "" then empty else "R " + $e end)
			elif ($raw | gsub("[ \t]"; "")) == "" then empty
			else "R " + ($raw | clean) end
		)' \
	| while IFS= read -r line; do
		kind="${line:0:1}"
		payload="${line:2}"
		case "$kind" in
		T)
			[ -z "${payload// /}" ] && continue
			if is_harness_internal "$payload"; then
				echo "dropped harness internal: ${payload:0:80}"
				continue
			fi
			echo "reply part: $payload"
			# markers only AFTER audio actually played: a TTS/playback failure
			# must not count as "answered" (Kurt would sit in verified silence)
			if speak "$payload"; then
				touch "$tmpdir/spoke" "$tmpdir/answered"
			else
				echo "part was not spoken (TTS/playback failure)"
			fi
			;;
		U)
			echo "tool: $payload"
			rm -f "$tmpdir/answered"
			;;
		V)
			echo "tool failed: $payload"
			# remember the tool name so a died round can say WHAT failed
			printf '%s' "${payload%%$'\t'*}" > "$tmpdir/toolerr"
			rm -f "$tmpdir/answered"
			;;
		F)
			printf '%s' "$payload" > "$tmpdir/finish"
			;;
		X)
			printf '%s' "$payload" > "$tmpdir/exit"
			;;
		R)
			echo "agent stderr: $payload"
			;;
		E)
			echo "agent error: $payload"
			;;
		S)
			[ -n "$payload" ] && printf '%s' "$payload" > "$sess_file"
			;;
		esac
	done

ec=""
[ -f "$tmpdir/exit" ] && ec=$(cat "$tmpdir/exit")
finish=""
[ -f "$tmpdir/finish" ] && finish=$(cat "$tmpdir/finish")
if [ -z "$ec" ]; then
	# machinectl/sudo/pipe died before the container wrapper could report
	echo "agent exit status missing — machinectl pipeline died"
elif [ "$ec" = "124" ]; then
	echo "agent exited 124 (timeout 600s)"
elif [ "$ec" != "0" ]; then
	echo "agent exited $ec"
fi

# died = the run ended still owing work: last step finished wanting more
# tools (e.g. a permission auto-reject killed it mid-plan), output was
# truncated ("length"), nonzero/killed exit, or no text after the last tool
died=""
[ -f "$tmpdir/spoke" ] && [ ! -f "$tmpdir/answered" ] && died=1
[ -n "$finish" ] && [ "$finish" != "stop" ] && died=1
[ -n "$ec" ] && [ "$ec" != "0" ] && died=1

# name the failing step when we know it — a causeless "something broke" is
# what let three silent deaths go undiagnosed in early August
cause=""
[ -f "$tmpdir/toolerr" ] && cause=" The $(cat "$tmpdir/toolerr") step failed."

if horus_started_since "$round_start"; then
	# the song / the article is the answer — a round with no (spoken) text is
	# expected here, so neither fallback applies
	echo "round ended with audio playing"
elif [ ! -f "$tmpdir/spoke" ]; then
	echo "no reply text received (finish=${finish:-none} exit=${ec:-none})"
	# a stale/broken session id would keep failing every round — drop it
	[ -n "$sess_args" ] && rm -f "$sess_file"
	speak "Sorry, something went wrong — I didn't get an answer back.${cause}" || play "$sounds/dialog-error.oga"
elif [ -n "$died" ]; then
	echo "round died mid-tools (finish=${finish:-none} exit=${ec:-none})"
	speak "Sorry — something broke while I was working on that, and I didn't get a result back.${cause}" || play "$sounds/dialog-error.oga"
fi
