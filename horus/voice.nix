# Voice: headphone-button push-to-talk -> whisper.cpp STT -> agent -> Piper TTS.
# Only runs while the Nothing Headphone (1) (3C:B0:ED:A7:8B:42) is connected:
# a BlueZ D-Bus watcher starts/stops horus-voice.service.
{ config, pkgs, lib, inputs, ... }:

let
	unstable = inputs.nixpkgs-unstable.legacyPackages.${pkgs.stdenv.hostPlatform.system};
	whisper-cpp = unstable.whisper-cpp.override { vulkanSupport = true; };

	headphoneMac = "3C:B0:ED:A7:8B:42";
	whisperModel = "/var/lib/llm/models/ggml-large-v3-turbo.bin";
	piperVoice = "/var/lib/llm/models/piper-en_US-lessac-medium.onnx";

	pythonEnv = pkgs.python3.withPackages (ps: [ ps.evdev ps.dbus-python ps.pygobject3 ]);

	# PTT daemon: grabs the headphone AVRCP input device, uses one key as
	# talk-toggle, re-injects everything else so normal media keys keep working.
	pttDaemon = pkgs.writeText "horus-ptt.py" ''
		import subprocess, sys, time
		import evdev
		from evdev import InputDevice, UInput, ecodes

		DEVICE_NAME = "Nothing Headphone (1) (AVRCP)"
		PTT_KEY = ecodes.KEY_NEXTSONG  # paddle-right; TODO test dedicated AI button
		RECORD_CMD = ["pw-record", "--rate", "16000", "--channels", "1"]
		WAV = "/tmp/horus-voice.wav"

		def find_device():
		    for path in evdev.list_devices():
		        d = InputDevice(path)
		        if d.name == DEVICE_NAME:
		            return d
		    return None

		def transcribe_and_respond():
		    r = subprocess.run(
		        ["horus-voice-respond", WAV],
		        timeout=600,
		    )

		def main():
		    dev = None
		    for _ in range(30):
		        dev = find_device()
		        if dev:
		            break
		        time.sleep(2)
		    if not dev:
		        print("headphone input device not found", file=sys.stderr)
		        sys.exit(1)

		    print(f"grabbing {dev.path} ({dev.name})")
		    dev.grab()
		    ui = UInput.from_device(dev, name="horus-ptt-passthrough")
		    rec = None
		    try:
		        for ev in dev.read_loop():
		            if ev.type == ecodes.EV_KEY and ev.code == PTT_KEY:
		                if ev.value == 1:  # key down = toggle
		                    if rec is None:
		                        subprocess.run(["pw-play", "/run/current-system/sw/share/sounds/freedesktop/stereo/audio-volume-change.oga"], check=False)
		                        rec = subprocess.Popen(RECORD_CMD + [WAV])
		                        print("recording...")
		                    else:
		                        rec.terminate(); rec.wait(); rec = None
		                        print("transcribing...")
		                        transcribe_and_respond()
		            else:
		                ui.write_event(ev)  # pass through play/pause etc.
		                ui.syn()
		    finally:
		        if rec: rec.terminate()
		        dev.ungrab()

		main()
	'';

	# STT -> agent -> TTS, one shot per utterance
	voiceRespond = pkgs.writeShellApplication {
		name = "horus-voice-respond";
		runtimeInputs = [ whisper-cpp pkgs.pipewire pkgs.jq ];
		text = ''
			wav="$1"
			text=$(whisper-cli -m ${whisperModel} -f "$wav" --language en --no-timestamps 2>/dev/null | sed 's/^ *//' | tr '\n' ' ')
			echo "heard: $text"
			[ -z "''${text// /}" ] && exit 0
			reply=$(sudo -n machinectl shell horus@horus /run/current-system/sw/bin/bash -c \
				"cd /home/horus/work && opencode run $(printf '%q' "$text")" \
				| grep -v '^Connected to machine\|^Connection to machine' || true)
			echo "reply: $reply"
			[ -z "''${reply// /}" ] && exit 0
			echo "$reply" | piper --model ${piperVoice} --output_file /tmp/horus-reply.wav
			pw-play /tmp/horus-reply.wav
		'';
	};

	# Watches BlueZ over D-Bus; starts/stops the voice service when the
	# headphones (dis)connect. Runs always (near-zero cost).
	btWatcher = pkgs.writeText "horus-bt-watch.py" ''
		import subprocess
		import dbus
		from dbus.mainloop.glib import DBusGMainLoop
		from gi.repository import GLib

		MAC = "${headphoneMac}"
		PATH_SUFFIX = "dev_" + MAC.replace(":", "_")

		def set_voice(active):
		    action = "start" if active else "stop"
		    subprocess.run(["systemctl", "--user", action, "horus-voice.service"], check=False)
		    print(f"voice {action}")

		def props_changed(iface, changed, invalidated, path=None):
		    if iface != "org.bluez.Device1" or "Connected" not in changed:
		        return
		    if path and path.endswith(PATH_SUFFIX):
		        set_voice(bool(changed["Connected"]))

		DBusGMainLoop(set_as_default=True)
		bus = dbus.SystemBus()
		bus.add_signal_receiver(
		    props_changed,
		    dbus_interface="org.freedesktop.DBus.Properties",
		    signal_name="PropertiesChanged",
		    path_keyword="path",
		)

		# initial state
		try:
		    obj = bus.get_object("org.bluez", f"/org/bluez/hci0/{PATH_SUFFIX}")
		    props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
		    set_voice(bool(props.Get("org.bluez.Device1", "Connected")))
		except dbus.exceptions.DBusException:
		    pass

		GLib.MainLoop().run()
	'';
in
{
	# PTT daemon runs as a user service and needs raw evdev access
	users.users.a3chron.extraGroups = [ "input" ];

	environment.systemPackages = [
		whisper-cpp
		unstable.piper-tts
		voiceRespond
		pkgs.sound-theme-freedesktop
	];

	systemd.user.services.horus-bt-watch = {
		description = "Start/stop Horus voice when Nothing headphones (dis)connect";
		wantedBy = [ "default.target" ];
		serviceConfig = {
			ExecStart = "${pythonEnv}/bin/python ${btWatcher}";
			Restart = "on-failure";
			RestartSec = 5;
		};
	};

	systemd.user.services.horus-voice = {
		description = "Horus push-to-talk voice pipeline";
		# started/stopped by horus-bt-watch, never at login
		path = [ pkgs.pipewire voiceRespond "/run/wrappers" ];
		serviceConfig = {
			ExecStart = "${pythonEnv}/bin/python ${pttDaemon}";
			Restart = "on-failure";
			RestartSec = 5;
		};
	};
}
