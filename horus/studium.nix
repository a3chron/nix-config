# Studium: host-side daemon that starts/stops the studium-progress dev server
# (Kurt's TUM dashboard, ~/Projects/studium-progress, port 3005) over a tiny
# localhost HTTP API (:8899). The agent's `studium` tool in the container
# reaches it over the shared network namespace — same pattern as music.nix.
{ pkgs, ... }:

{
	systemd.user.services.horus-studium = {
		description = "Horus studium dashboard control daemon (127.0.0.1:8899)";
		wantedBy = [ "default.target" ];
		# nix + git for `nix develop` (flake in a git worktree); the dev shell
		# provides node/pnpm, the sync script runs on the host's python3
		path = [ pkgs.nix pkgs.git pkgs.python3 pkgs.coreutils pkgs.bash ];
		serviceConfig = {
			# deliberately impure repo path, same pattern as horus-music:
			# tuning only needs a user-service restart, no rebuild
			ExecStart = "${pkgs.python3}/bin/python /home/a3chron/nixos-config/horus/horus-studium.py";
			Restart = "on-failure";
			RestartSec = 5;
		};
	};
}
