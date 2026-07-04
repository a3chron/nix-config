# Voice: headphone-button push-to-talk -> whisper.cpp STT -> agent -> Piper TTS.
# Only runs while the Nothing Headphone (1) (3C:B0:ED:A7:8B:42) is connected:
# a BlueZ D-Bus watcher starts/stops horus-voice.service.
# Python daemons live in ./horus-ptt.py and ./horus-bt-watch.py.
{ config, pkgs, lib, inputs, ... }:

let
	unstable = inputs.nixpkgs-unstable.legacyPackages.${pkgs.stdenv.hostPlatform.system};
	whisper-cpp = unstable.whisper-cpp.override { vulkanSupport = true; };

	headphoneMac = "3C:B0:ED:A7:8B:42";
	whisperModel = "/var/lib/llm/models/ggml-large-v3-turbo.bin";
	piperVoice = "/var/lib/llm/models/piper-en_US-lessac-medium.onnx";

	pythonEnv = pkgs.python3.withPackages (ps: [ ps.evdev ps.dbus-python ps.pygobject3 ]);

	# STT -> agent -> TTS, one shot per utterance
	voiceRespond = pkgs.writeShellApplication {
		name = "horus-voice-respond";
		runtimeInputs = [ whisper-cpp unstable.piper-tts pkgs.pipewire pkgs.jq ];
		text = ''
			sounds=/run/current-system/sw/share/sounds/freedesktop/stereo
			wav="$1"
			# strip whisper noise markers like [BLANK_AUDIO], (bell), *music*
			text=$(whisper-cli -m ${whisperModel} -f "$wav" --language en --no-timestamps 2>/dev/null \
				| sed -E 's/\[[^]]*\]//g; s/\([^)]*\)//g; s/^ *//' | tr '\n' ' ')
			text=$(echo "$text" | sed -E 's/^ +| +$//g')
			echo "heard: $text"
			if [ -z "''${text// /}" ]; then
				pw-play "$sounds/dialog-warning.oga" & # didn't catch anything
				exit 0
			fi
			# absolute machinectl path: the NOPASSWD sudoers rule matches exactly this;
			# the unit's PATH would resolve to the raw nix-store path and get a password prompt
			# JSON events -> just the assistant text parts, ANSI-free by construction
			reply=$(/run/wrappers/bin/sudo -n /run/current-system/sw/bin/machinectl shell horus@horus /run/current-system/sw/bin/bash -c \
				"cd /home/horus/work && opencode run --format json $(printf '%q' "$text") 2>/dev/null" \
				| grep '^{' | jq -rs 'map(select(.type=="text") | .part.text) | join(" ")' 2>/dev/null || true)
			echo "reply: $reply"
			if [ -z "''${reply// /}" ]; then
				pw-play "$sounds/dialog-error.oga" &
				exit 0
			fi
			echo "$reply" | piper --model ${piperVoice} --output_file /tmp/horus-reply.wav
			pw-play /tmp/horus-reply.wav
		'';
	};
in
{
	# PTT daemon runs as a user service and needs raw evdev access
	users.users.a3chron.extraGroups = [ "input" ];

	# never suspend the headphone sink: a suspended BT link takes 1-2s to wake
	# and swallows the start chime / first spoken word of a reply
	services.pipewire.wireplumber.extraConfig."51-horus-bluez-no-suspend" = {
		"monitor.bluez.rules" = [
			{
				matches = [ { "node.name" = "~bluez_output.*"; } ];
				actions.update-props."session.suspend-timeout-seconds" = 0;
			}
		];
	};

	environment.systemPackages = [
		whisper-cpp
		unstable.piper-tts
		voiceRespond
		pkgs.sound-theme-freedesktop
	];

	systemd.user.services.horus-bt-watch = {
		description = "Start/stop Horus voice when Nothing headphones (dis)connect";
		wantedBy = [ "default.target" ];
		environment.HORUS_HEADPHONE_MAC = headphoneMac;
		serviceConfig = {
			ExecStart = "${pythonEnv}/bin/python ${./horus-bt-watch.py}";
			Restart = "on-failure";
			RestartSec = 5;
		};
	};

	systemd.user.services.horus-voice = {
		description = "Horus push-to-talk voice pipeline";
		# started/stopped by horus-bt-watch, never at login
		path = [ pkgs.pipewire pkgs.pulseaudio voiceRespond "/run/wrappers" ];
		environment.HORUS_HEADPHONE_MAC = headphoneMac;
		serviceConfig = {
			# deliberately impure: run the script straight from the config repo so
			# tuning (chimes, thresholds) only needs a user-service restart, no rebuild.
			# Pin back to ${./horus-ptt.py} once the voice UX has settled.
			ExecStart = "${pythonEnv}/bin/python /home/a3chron/nixos-config/horus/horus-ptt.py";
			Restart = "on-failure";
			RestartSec = 5;
		};
	};
}
