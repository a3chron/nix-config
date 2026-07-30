# Music: host-side daemon that plays ~/Music via mpv, driven over a tiny
# localhost HTTP API (:8877). The agent's `music` tool in the container
# reaches it over the shared network namespace; audio comes out of the HOST
# (default sink — speakers or headphones, whatever is active).
# horus-voice-respond.sh also talks to it, to suppress TTS when a song just
# started and to pause/resume music around spoken replies.
{ pkgs, ... }:

{
	systemd.user.services.horus-music = {
		description = "Horus music playback daemon (mpv wrapper on 127.0.0.1:8877)";
		wantedBy = [ "default.target" ];
		path = [ pkgs.mpv ];
		serviceConfig = {
			# deliberately impure repo path, same pattern as the voice scripts:
			# tuning only needs a user-service restart, no rebuild
			ExecStart = "${pkgs.python3}/bin/python /home/a3chron/nixos-config/horus/horus-music.py";
			Restart = "on-failure";
			RestartSec = 5;
		};
	};
}
