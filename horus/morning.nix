# Horus morning status: the first time Horus is loaded on a given day, before
# noon, he says one short spoken line (weather + due/urgent personal todos).
# A minutely user timer polls; horus-morning.py owns every gate and exits on a
# stamp compare once the day is done. User service, like the wake-up drain:
# speaking needs the user's pipewire session. Logic is impure
# (./horus-morning.py), so tuning needs no rebuild.
{ config, pkgs, lib, ... }:

{
	systemd.user.services.horus-morning = {
		description = "Horus morning status (first load of the day, before noon)";
		unitConfig = {
			OnFailure = [ "horus-alert@%n.service" ];
			ConditionUser = "a3chron"; # see music.nix — gdm-greeter otherwise runs
			                           # this too and dies on the impure path
		};
		serviceConfig = {
			Type = "oneshot";
			ExecStart = "${pkgs.python3}/bin/python /home/a3chron/nixos-config/horus/horus-morning.py";
			# cold model load + agent run (<=5min) + cold Kokoro + playback
			TimeoutStartSec = 600;
		};
	};

	systemd.user.timers.horus-morning = {
		wantedBy = [ "timers.target" ];
		timerConfig = {
			OnCalendar = "*-*-* 05..11:*:00"; # every minute 05:00-11:59
			Persistent = false; # a missed morning is simply not delivered
		};
	};
}
