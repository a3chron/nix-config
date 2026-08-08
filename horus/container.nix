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
			# ...but the agent's own CONTROL SURFACE is re-bound read-only on top of
			# it. Everything below decides what the agent is allowed to do, or runs
			# with no sandbox at all, so a prompt-injected run must not be able to
			# rewrite it and self-grant:
			#   opencode.json      — the permission rules themselves (and `shell`,
			#                        i.e. the netless-bash shim above)
			#   .opencode/         — tools/ and plugin/ are TypeScript loaded
			#                        IN-PROCESS by the Bun server, with full network
			#                        and no permission check at all; also
			#                        unattended-permission.json, the ask->deny override
			#   AGENTS.md          — the standing instructions
			#   bridge/server.js   — runs as a systemd service, outside opencode
			# `edit` is {"*": "allow"} under /home/horus/work, so before this the
			# agent could simply edit any of them. Writes now fail EROFS.
			#
			# Kurt still edits all of it normally from the host — only the container's
			# view is read-only. Everything else under work/ stays writable, which is
			# everything the agent legitimately writes: memory/, skills/, backups/,
			# bridge/*.jsonl, bridge/bridge.log, bridge/responder-state.json,
			# wa-auth/, workspace/.
			#
			# Nesting works: nixos-containers' mkBindFlags is
			# `concatMapStrings mkBindFlag (lib.attrValues bs)` and attrValues sorts by
			# attribute name — which IS the mount point (mountPoint = mkDefault name) —
			# so a child always follows its parent; nspawn also re-sorts custom mounts
			# by destination. --bind-ro of a single FILE is fine.
			"/home/horus/work/opencode.json" = {
				hostPath = "/home/a3chron/horus/opencode.json";
				isReadOnly = true;
			};
			# git hooks + git config: NOT in the original hardening plan, found while
			# implementing it, and the sharpest edge of the lot — this one escapes the
			# container entirely. horus-backup.service (backup.nix) runs `git add -A`
			# and `git commit` in /home/a3chron/horus daily, as a3chron, ON THE HOST,
			# with no --no-verify. So a file the agent drops at .git/hooks/pre-commit
			# executes within 24h outside the container, outside the netns shim above,
			# as Kurt, with his whole home (~/.ssh, ~/nixos-config, ~/horus/.secrets)
			# and full network. git never tracks .git, so it wouldn't even show up in
			# the commit it triggers.
			# Without this, everything above is bypassable: "can't write opencode.json"
			# just becomes "plant a hook that writes it from the host tomorrow".
			# .git/config is bound too — it is an equivalent vector by other means
			# (core.hooksPath redirects hooks at a writable dir; filter.*.clean runs on
			# `git add`; core.fsmonitor, diff.*.textconv likewise).
			# Deliberately NOT the whole of .git: `git status` refreshes the index and
			# is heavily used, and Kurt-approved `git commit` from inside should keep
			# working. Only the two code-execution surfaces are frozen.
			"/home/horus/work/.git/hooks" = {
				hostPath = "/home/a3chron/horus/.git/hooks";
				isReadOnly = true;
			};
			"/home/horus/work/.git/config" = {
				hostPath = "/home/a3chron/horus/.git/config";
				isReadOnly = true;
			};
			"/home/horus/work/.opencode" = {
				hostPath = "/home/a3chron/horus/.opencode";
				isReadOnly = true;
			};
			"/home/horus/work/AGENTS.md" = {
				hostPath = "/home/a3chron/horus/AGENTS.md";
				isReadOnly = true;
			};
			"/home/horus/work/bridge/server.js" = {
				hostPath = "/home/a3chron/horus/bridge/server.js";
				isReadOnly = true;
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

			# music library, read-only: playback happens on the HOST (the
			# horus-music daemon in music.nix, reached on localhost:8877 via the
			# music tool) — this mount just lets Horus browse/search the files
			"/home/horus/music" = {
				hostPath = "/home/a3chron/Music";
				isReadOnly = true;
			};
		};

		config = { pkgs, lib, ... }: let
			# The shell every `bash` tool call runs under. opencode's bash tool does
			# spawn(cmd, { shell: <config.shell> }) -> execve("<binary>", ["-c", cmd]),
			# and accepts ANY executable, not just a known shell name (verified in the
			# 1.17.9 binary; the project opencode.json's `shell` key is honoured — spiked
			# with an argv-logging wrapper before this was written). So pointing it at a
			# wrapper puts every bash call — allowlisted, Kurt-approved or injected — into
			# a fresh EMPTY network namespace.
			#
			# Why: the agent reads and searches the web, so prompt injection is a live
			# risk, and it can otherwise reach anything listening on localhost — including
			# (from A3C-167 on) the `opencode serve` permission API, i.e. it could approve
			# its own permission prompts. privateNetwork does NOT help there: the bash
			# tool's processes are children of the server, sharing its uid, cgroup and
			# network namespace, so no bind address or firewall rule can hide a listener
			# from them. A netns of their own is the only thing that does.
			#
			# Deliberately ONLY the network namespace (no mount/pid/user-visible change):
			# the process group survives, so `horus cancel`'s pkill and opencode's own
			# timeouts still reach the children. Nothing on the bash allowlist needs the
			# network (gh and pnpm aren't even installed here), and the in-process tools
			# that DO need it — nanoleaf on the LAN, web_search, web_fetch, music,
			# linear, whatsapp_send — run inside the Bun server, never through this shim.
			#
			# NAME MATTERS: it must not be one of opencode's known shell names
			# (bash/zsh/sh/fish/nu/...), or the terminal path appends shell-specific
			# flags instead of a plain ["-c", cmd].
			#
			# unprivileged `unshare --user` works because nspawn runs PRIVATE_USERS=no.
			# Fails closed: if unshare ever stops working the bash tool errors out rather
			# than silently regaining the network.
			netlessShell = pkgs.writeShellApplication {
				name = "horus-netless-shell";
				# bash, not sh: opencode's default shell is $SHELL, which for the horus
				# user is bashInteractive, and its bash tool prompts the model for bash.
				# `sh` here would be bash in POSIX mode — a silent behaviour change for
				# every command the model writes. Pinned via runtimeInputs rather than
				# the inherited PATH because this also runs from systemd units
				# (wa-bridge) whose environment we don't control.
				runtimeInputs = [ pkgs.util-linux pkgs.bash ];
				text = ''
					exec unshare --user --map-current-user --net -- bash "$@"
				'';
			};
		in {
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
				netlessShell # opencode.json: "shell" — see the comment above
				unstable.opencode
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
			# 1.1.1.1 third: away from the fritz networks (hotspot, other WiFi)
			# both router IPs are unreachable and the container had NO working
			# DNS at all — the WA bridge was dark 2026-07-14→16 because of this.
			# At home it's never queried (the fritz answers first); away, each
			# lookup stalls ~4s on the dead router IPs, then works. glibc uses
			# at most 3 nameservers, so this fills the last slot.
			networking.useHostResolvConf = lib.mkForce false;
			networking.resolvconf.enable = lib.mkForce false;
			# plain string, not '': tab indentation would survive inside '' and
			# glibc ignores resolv.conf lines that don't start with the keyword
			environment.etc."resolv.conf".text =
				"nameserver 192.168.180.1\nnameserver 192.168.178.1\nnameserver 1.1.1.1\noptions timeout:2 attempts:2\n";

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
					# append: has no rotation — rotate on each start (container restarts
					# on every pause/resume, so this actually fires). Rotate to .1
					# instead of truncating: a truncate-in-place destroyed the evidence
					# exactly when Kurt restarted the container to investigate a failure.
					# Runs as root ("+") because systemd created the file root-owned.
					ExecStartPre = "+" + pkgs.writeShellScript "wa-bridge-logrotate" ''
						f=/home/horus/work/bridge/bridge.log
						if [ -f "$f" ] && [ "$(stat -c%s "$f")" -gt 1048576 ]; then
							mv -f "$f" "$f.1"
							tail -n 200 "$f.1" > "$f" || true
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
		onFailure = [ "horus-alert@%n.service" ];
	};
}
