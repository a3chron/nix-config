# chronogrid: the todos + timeline dashboard at ~/Projects/chronogrid, served on
# :3004. Always running, so the tab is just there when Kurt opens it.
#
# Runs in PRODUCTION mode (`pnpm start`), unlike horus/studium.nix which starts
# studium-progress in dev on demand. Different job: this one is meant to be up
# permanently, and `next dev` recompiles per request and holds a much bigger RSS
# for no benefit when the code is not being edited.
#
# The server itself does NO background work. Linear is polled from the browser
# and gated on document visibility, so an idle instance with no tab open just
# holds the SQLite handle and waits — which is what makes "always on" cheap.
{ pkgs, ... }:

let
	projectDir = "/home/a3chron/Projects/chronogrid";

	# Build before serving, because `next start` refuses to run without a build,
	# and a stale build would silently serve yesterday's code after an edit.
	#
	# Next's incremental cache makes a no-change rebuild cheap, so doing this on
	# every start buys "a reboot always serves current code" for a few seconds —
	# far preferable to the failure mode where the dashboard is quietly N commits
	# behind and nothing says so.
	prestart = pkgs.writeShellScript "chronogrid-prestart" ''
		set -euo pipefail
		cd ${projectDir}

		# --frozen-lockfile so a boot can never silently resolve new versions;
		# if the lockfile and package.json disagree, fail loudly instead.
		nix develop ${projectDir} -c pnpm install --frozen-lockfile
		nix develop ${projectDir} -c pnpm build
	'';
in
{
	systemd.user.services.chronogrid = {
		description = "chronogrid todos & timeline dashboard (localhost:3004)";
		wantedBy = [ "default.target" ];
		after = [ "network-online.target" ];
		wants = [ "network-online.target" ];

		# nix + git for `nix develop` (the flake lives in a git worktree); the dev
		# shell itself provides node/pnpm. Same reasoning as horus/studium.nix.
		path = [ pkgs.nix pkgs.git pkgs.coreutils pkgs.bash ];

		unitConfig = {
			OnFailure = [ "horus-alert@%n.service" ];
			StartLimitIntervalSec = 0;
			# Without this the gdm-greeter user session also tries to start it,
			# cannot read the impure /home path, and produces a restart loop plus an
			# alert storm — the same trap documented in horus/music.nix.
			ConditionUser = "a3chron";
			ConditionPathIsDirectory = projectDir;
		};

		serviceConfig = {
			# Deliberately an impure repo path, matching horus-music/horus-studium:
			# editing the app needs a service restart, not a nixos-rebuild.
			WorkingDirectory = projectDir;
			ExecStartPre = "${prestart}";
			ExecStart = "${pkgs.nix}/bin/nix develop ${projectDir} -c pnpm start";

			Environment = [
				"NODE_ENV=production"
				"DASHBOARD_DB_PATH=${projectDir}/data/dashboard.db"
			];

			Restart = "on-failure";
			RestartSec = 5;
			# The install+build in ExecStartPre can take a while on a cold cache.
			TimeoutStartSec = "10min";

			# `next start` ignores SIGTERM on the parent under `nix develop -c`;
			# kill the whole group so a restart does not leave :3004 occupied.
			KillMode = "control-group";
		};
	};

	# ── uptime sampler ────────────────────────────────────────────────────────
	#
	# The Status tab charts uptime for the Horus stack, and that history cannot
	# come from the browser: chronogrid does no server-side background work, so
	# with no tab open there would be no samples — precisely the periods you would
	# most want recorded (overnight, or while the machine was off).
	#
	# So a timer, matching the house pattern (horus-wa-watch.timer). It is a
	# one-line curl on purpose: all the logic lives in the route, in TypeScript,
	# next to the schema and the tests.
	systemd.user.services.chronogrid-uptime = {
		description = "chronogrid uptime sampler";
		path = [ pkgs.curl ];

		unitConfig = {
			# Same gdm-greeter trap as the service above.
			ConditionUser = "a3chron";
			# Deliberately NO OnFailure = horus-alert@: a missed uptime sample is not
			# worth a WhatsApp message, and the gap is self-evident in the chart.
		};

		serviceConfig = {
			Type = "oneshot";
			# -f so a non-2xx is a unit failure in the journal rather than a silent
			# success; -m so a hung request cannot pile up against the 5min timer.
			ExecStart = "${pkgs.curl}/bin/curl -fsS -m 60 -X POST http://127.0.0.1:3004/api/uptime/sample -o /dev/null";
		};
	};

	systemd.user.timers.chronogrid-uptime = {
		description = "sample service uptime for chronogrid every 5 minutes";
		wantedBy = [ "timers.target" ];

		unitConfig.ConditionUser = "a3chron";

		timerConfig = {
			# Not OnBootSec=0: the dashboard's own ExecStartPre runs an install and a
			# build, so :3004 is not answering for the first minute or two after boot.
			OnBootSec = "3min";
			OnUnitActiveSec = "5min";
			# Persistent = false on purpose. A catch-up run after a long shutdown
			# would credit one enormous interval reaching back to the last sample,
			# and the sampler already reconstructs the current run exactly from
			# ActiveEnterTimestamp — a replay would add nothing but a distorted
			# bucket.
			Persistent = false;
		};
	};

	# `chronogrid-restart` — rebuild and restart after editing the app, without a
	# nixos-rebuild. The ExecStartPre above does the build, so this is just a
	# restart with a readable name and a tail of the log.
	environment.systemPackages = [
		(pkgs.writeShellScriptBin "chronogrid-restart" ''
			set -euo pipefail
			echo "rebuilding and restarting chronogrid…"
			systemctl --user restart chronogrid
			systemctl --user --no-pager status chronogrid | head -n 12
			echo
			echo "logs: journalctl --user -u chronogrid -f"
		'')
	];
}
