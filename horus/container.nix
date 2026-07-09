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

			# Always-available public projects (no `horus grant` needed), read-write
			# so Horus can edit/format. Commits and pushes are gated in opencode.json
			# (bash: git commit -> ask so Kurt approves after reviewing the diff and
			# unattended runs auto-deny; git push -> deny; push is impossible anyway,
			# no creds in the container). Personal projects are NOT here — they stay
			# invisible until `horus grant` (see cli.nix).
			"/home/horus/projects/portfolio" = {
				hostPath = "/home/a3chron/Projects/portfolio";
				isReadOnly = false;
			};
			# kaeru is a monorepo of three independent git repos (root has no .git)
			"/home/horus/projects/kaeru" = {
				hostPath = "/home/a3chron/Projects/kaeru";
				isReadOnly = false;
			};
			# stellar: read-only — a public repo Kurt wants Horus able to read and
			# analyse (lint/type-check work; edits/formatting fail on the RO mount)
			"/home/horus/projects/stellar" = {
				hostPath = "/home/a3chron/Projects/stellar";
				isReadOnly = true;
			};
		};

		config = { pkgs, lib, ... }: {
			system.stateVersion = "25.11";

			# containers default to UTC; the nanoleaf tool's day/night white uses
			# Europe/Berlin explicitly anyway, this keeps everything else in sync
			time.timeZone = "Europe/Berlin";

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
				pkgs.poppler-utils # pdftotext/pdfinfo/pdftohtml for the pdf tool
			];

			# static DNS instead of copying the host's resolv.conf: the copy happens
			# once at container start, and at boot that's BEFORE WiFi/DHCP has
			# written any nameservers — leaving the container without DNS until the
			# next restart (bit us 2026-07-06: bridge stuck on ENOTFOUND for hours).
			# networking.nameservers alone is NOT enough: with no DHCP client in
			# the container, resolvconf has no source and generates an EMPTY
			# resolv.conf (bit us again same night) — write the file directly.
			# MUST be the fritz routers: both boxes REJECT external DNS (port 53
			# to 1.1.1.1 etc. → ECONNREFUSED). .180.1 = own LAN (cable, always
			# there, resolves even while its internet is parental-blocked),
			# .178.1 = house net via WiFi as fallback. timeout:2 caps the stall
			# when the first one is unreachable (cable unplugged).
			networking.useHostResolvConf = lib.mkForce false;
			networking.resolvconf.enable = lib.mkForce false;
			# plain string, not '': tab indentation would survive inside '' and
			# glibc ignores resolv.conf lines that don't start with the keyword
			environment.etc."resolv.conf".text =
				"nameserver 192.168.180.1\nnameserver 192.168.178.1\noptions timeout:2 attempts:2\n";

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
