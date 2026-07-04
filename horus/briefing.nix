# Saturday briefing: timer wakes the stack and lets the agent compose+send
# the WhatsApp briefing itself (instructions live in ~/horus/skills/saturday-briefing.md).
{ config, pkgs, lib, ... }:

{
	systemd.services.horus-briefing = {
		description = "Horus Saturday briefing";
		serviceConfig.Type = "oneshot";
		script = ''
			systemctl start llama-swap.service
			systemctl start container@horus.service
			sleep 10
			${pkgs.systemd}/bin/machinectl shell horus@horus /run/current-system/sw/bin/bash -c \
				'cd /home/horus/work && opencode run "Run the saturday briefing (see skills/saturday-briefing.md) and send it to Kurt via WhatsApp."' \
				|| true
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
