# Horus self-scheduled wake-ups: a minutely user timer drains the queue the
# agent writes via its `wakeup` tool (~/horus/memory/reminders/queue.jsonl) —
# fires the agent in the container and delivers the answer over speakers
# (horus-tts) or WhatsApp (bridge /send). User service: speaking needs the
# user's pipewire session. Drain logic is impure (./horus-wakeup-drain.py),
# same pattern as the other voice scripts — tuning needs no rebuild.
{ config, pkgs, lib, ... }:

{
	systemd.user.services.horus-wakeup-drain = {
		description = "Fire due Horus self-scheduled wake-ups";
		unitConfig = {
			OnFailure = [ "horus-alert@%n.service" ];
			ConditionUser = "a3chron"; # see music.nix
		};
		serviceConfig = {
			Type = "oneshot";
			ExecStart = "${pkgs.python3}/bin/python /home/a3chron/nixos-config/horus/horus-wakeup-drain.py";
			# a job runs the agent (up to ~5min) and may speak afterwards
			TimeoutStartSec = 600;
		};
	};

	systemd.user.timers.horus-wakeup-drain = {
		wantedBy = [ "timers.target" ];
		timerConfig = {
			OnCalendar = "*-*-* *:*:00"; # every minute; the drain exits instantly when nothing is due
			Persistent = false; # missed while off → handled by the drain's own staleness rules
		};
	};
}
