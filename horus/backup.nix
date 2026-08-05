# Daily auto-commit of ~/horus: the agent edits its own memory/ and skills/
# but only commits when it remembers to — this guarantees a restorable git
# state regardless. Commit-only (the repo has no remote).
{ config, pkgs, lib, ... }:

{
	systemd.services.horus-backup = {
		description = "Auto-commit ~/horus (agent memory/skills)";
		onFailure = [ "horus-alert@%n.service" ];
		serviceConfig = {
			Type = "oneshot";
			User = "a3chron";
		};
		path = [ pkgs.git pkgs.sqlite ];
		script = ''
			cd /home/a3chron/horus
			# snapshot the opencode session DB out of the container's private root —
			# it's the only memory layer with no other backup (WAL-safe .backup)
			mkdir -p backups
			db=/var/lib/nixos-containers/horus/home/horus/.local/share/opencode/opencode-stable.db
			if [ -r "$db" ]; then
				sqlite3 "$db" ".backup backups/opencode-stable.db" \
					|| echo "session DB snapshot failed (agent mid-write?) — keeping yesterday's"
			fi
			# stamp BEFORE committing so the stamp itself is part of the commit and
			# the briefing can assert on backup freshness
			date '+%Y-%m-%d %H:%M' > memory/.last-backup
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
