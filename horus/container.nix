# The Horus sandbox: a declarative nixos-container (systemd-nspawn).
# Shares the host network (reaches llama-swap on localhost:8080, has internet),
# but only sees the two bind-mounted directories of the host filesystem.
{ config, pkgs, lib, inputs, ... }:

let
	unstable = inputs.nixpkgs-unstable.legacyPackages.${pkgs.stdenv.hostPlatform.system};
in
{
	containers.horus = {
		autoStart = false; # started on demand via `horus resume`

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

			# shared network namespace: use the host's resolv.conf for DNS
			networking.useHostResolvConf = lib.mkForce true;

			# agent always works from ~/work (bind-mounted ~/horus on the host),
			# where opencode.json + AGENTS.md live
			environment.loginShellInit = ''
				if [ "$USER" = "horus" ]; then cd /home/horus/work; fi
			'';

			# WhatsApp bridge (Baileys) — queues messages even while the agent
			# is not in use; pairs via QR printed to the journal / bridge.log
			systemd.services.wa-bridge = {
				description = "Horus WhatsApp bridge";
				wantedBy = [ "multi-user.target" ];
				after = [ "network.target" ];
				serviceConfig = {
					User = "horus";
					WorkingDirectory = "/home/horus/work/bridge";
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
}
