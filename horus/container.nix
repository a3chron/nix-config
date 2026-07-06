# The Horus sandbox: a declarative nixos-container (systemd-nspawn).
# Shares the host network (reaches llama-swap on localhost:8080, has internet),
# but only sees the two bind-mounted directories of the host filesystem.
{ config, pkgs, lib, inputs, ... }:

let
	unstable = inputs.nixpkgs-unstable.legacyPackages.${pkgs.stdenv.hostPlatform.system};
in
{
	containers.horus = {
		# up from boot so the WhatsApp bridge receives AND auto-answers unattended
		# (the model itself still only loads on demand); `horus pause` stops it
		autoStart = true;

		bindMounts = {
			"/home/horus/work" = {
				hostPath = "/home/a3chron/horus";
				isReadOnly = false;
			};
			"/home/horus/vault" = {
				hostPath = "/home/a3chron/Documents/obsidian/main";
				isReadOnly = false;
			};
		};

		config = { pkgs, lib, ... }: {
			system.stateVersion = "25.11";

			# uid 1000 matches a3chron on the host -> bind mount permissions just work
			users.users.horus = {
				isNormalUser = true;
				uid = 1000;
				home = "/home/horus";
				description = "Horus agent";
				shell = pkgs.bashInteractive;
			};

			environment.systemPackages = [
				unstable.opencode
				unstable.qwen-code # fallback harness, tuned for Qwen models
				pkgs.git
				pkgs.ripgrep
				pkgs.fd
				pkgs.jq
				pkgs.curl
				pkgs.nodejs_24 # for MCP servers (searxng, Linear, WhatsApp bridge)
			];

			# static DNS instead of copying the host's resolv.conf: the copy happens
			# once at container start, and at boot that's BEFORE WiFi/DHCP has
			# written any nameservers — leaving the container without DNS until the
			# next restart (bit us 2026-07-06: bridge stuck on ENOTFOUND for hours)
			networking.useHostResolvConf = lib.mkForce false;
			networking.nameservers = [ "1.1.1.1" "9.9.9.9" ];

			# agent always works from ~/work (bind-mounted ~/horus on the host),
			# where opencode.json + AGENTS.md live
			environment.loginShellInit = ''
				if [ "$USER" = "horus" ]; then cd /home/horus/work; fi
			'';

			# WhatsApp bridge (Baileys) — receives messages and auto-answers
			# allowlisted senders via `opencode run`; pairs via QR printed to
			# the journal / bridge.log
			systemd.services.wa-bridge = {
				description = "Horus WhatsApp bridge";
				wantedBy = [ "multi-user.target" ];
				after = [ "network.target" ];
				serviceConfig = {
					User = "horus";
					WorkingDirectory = "/home/horus/work/bridge";
					# append: has no rotation — trim on each start (container restarts
					# on every pause/resume, so this actually fires). Runs as root ("+")
					# because systemd created the file root-owned.
					ExecStartPre = "+" + pkgs.writeShellScript "wa-bridge-logrotate" ''
						f=/home/horus/work/bridge/bridge.log
						if [ -f "$f" ] && [ "$(stat -c%s "$f")" -gt 1048576 ]; then
							tail -n 1000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
						fi
					'';
					ExecStart = "${pkgs.nodejs_24}/bin/node /home/horus/work/bridge/server.js";
					Restart = "always";
					RestartSec = 5;
					# QR + logs readable from the host at ~/horus/bridge/bridge.log
					StandardOutput = "append:/home/horus/work/bridge/bridge.log";
					StandardError = "append:/home/horus/work/bridge/bridge.log";
				};
			};
		};
	};

	# belt & suspenders for the same boot race: don't start the container until
	# the network is actually up (with static DNS the bridge would recover by
	# retrying anyway; this just skips the pointless early failures)
	systemd.services."container@horus" = {
		wants = [ "network-online.target" ];
		after = [ "network-online.target" ];
	};
}
