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
		unitConfig = {
			OnFailure = [ "horus-alert@%n.service" ];
			StartLimitIntervalSec = 0;
			# NixOS installs systemd.user units into /etc/systemd/user, so EVERY
			# user session starts them — including gdm-greeter's, which can't read
			# /home/a3chron (0700) and so dies on the impure ExecStart path above.
			# With Restart=on-failure + StartLimitIntervalSec=0 that became an
			# endless restart loop, each iteration firing OnFailure: 16 "horus-music
			# failed" notifications at boot on 2026-08-07, all of them bogus — the
			# real session started the daemon fine seconds later. A failed condition
			# SKIPS the unit (not a failure), so OnFailure stays silent elsewhere.
			ConditionUser = "a3chron";
		};
		serviceConfig = {
			# deliberately impure repo path, same pattern as the voice scripts:
			# tuning only needs a user-service restart, no rebuild
			ExecStart = "${pkgs.python3}/bin/python /home/a3chron/nixos-config/horus/horus-music.py";
			Restart = "on-failure";
			RestartSec = 5;
		};
	};
}
