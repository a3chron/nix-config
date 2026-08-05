# Saturday briefing: timer wakes the stack and lets the agent compose+send
# the WhatsApp briefing itself (instructions live in ~/horus/skills/saturday-briefing.md).
{ config, pkgs, lib, ... }:

{
	systemd.services.horus-briefing = {
		description = "Horus Saturday briefing";
		onFailure = [ "horus-alert@%n.service" ];
		serviceConfig.Type = "oneshot";
		# no `|| true` and no blind sleep anymore: a died run must FAIL the unit
		# (OnFailure pings Kurt) — the 08-01 briefing shipped a wrong two-week
		# delta and nothing anywhere noticed the 07-25 run had been skipped.
		script = ''
			systemctl start llama-swap.service
			systemctl start container@horus.service
			# readiness probe instead of sleep 10
			for _ in $(seq 1 30); do
				if ${pkgs.systemd}/bin/machinectl shell horus@horus /run/current-system/sw/bin/true >/dev/null 2>&1; then
					break
				fi
				sleep 2
			done
			out=$(mktemp)
			trap 'rm -f "$out"' EXIT
			${pkgs.systemd}/bin/machinectl shell horus@horus /run/current-system/sw/bin/bash -c \
				'cd /home/horus/work && OPENCODE_PERMISSION=$(cat .opencode/unattended-permission.json 2>/dev/null) timeout 900 opencode run --format json "Run the saturday briefing (see skills/saturday-briefing.md) and send it to Kurt via WhatsApp."; echo "%%EXIT $?"' \
				| tr -d '\r' > "$out" || true
			ec=$(sed -n 's/^%%EXIT //p' "$out" | tail -n 1)
			finish=$(grep '^{' "$out" | ${pkgs.jq}/bin/jq -r 'select(.type=="step_finish") | .part.reason // empty' 2>/dev/null | tail -n 1)
			if [ "''${ec:-1}" != "0" ] || { [ -n "$finish" ] && [ "$finish" != "stop" ]; }; then
				echo "briefing run died (exit=''${ec:-none} finish=''${finish:-none})"
				tail -n 20 "$out"
				exit 1
			fi
			echo "briefing completed (finish=$finish)"
		'';
	};

	systemd.timers.horus-briefing = {
		wantedBy = [ "timers.target" ];
		timerConfig = {
			OnCalendar = "Sat 08:00";
			Persistent = true; # fire on next boot if the PC was off at 8:00
		};
	};
}
