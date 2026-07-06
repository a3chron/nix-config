# Daily auto-commit of ~/horus: the agent edits its own memory/ and skills/
# but only commits when it remembers to — this guarantees a restorable git
# state regardless. Commit-only (the repo has no remote).
{ config, pkgs, lib, ... }:

{
	systemd.services.horus-backup = {
		description = "Auto-commit ~/horus (agent memory/skills)";
		serviceConfig = {
			Type = "oneshot";
			User = "a3chron";
		};
		path = [ pkgs.git ];
		script = ''
			cd /home/a3chron/horus
			git add -A
			git diff --cached --quiet || git -c user.name="horus-backup" -c user.email="horus@localhost" \
				commit -m "auto-backup: $(date '+%Y-%m-%d %H:%M')"
		'';
	};

	systemd.timers.horus-backup = {
		wantedBy = [ "timers.target" ];
		timerConfig = {
			OnCalendar = "daily";
			Persistent = true;
		};
	};
}
