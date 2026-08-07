# Failure notification path (there was none): `OnFailure=horus-alert@%n.service`
# on every Horus unit. The alert pings Kurt over WhatsApp via the bridge's
# localhost API (works from host — shared netns) and, for user units, also
# raises a desktop notification. Best-effort: if the bridge itself is down the
# journal entry is all that remains, but at least `horus status` now lists all
# units too (cli.nix).
{ config, pkgs, lib, ... }:

let
	alertScript = pkgs.writeShellApplication {
		name = "horus-alert";
		runtimeInputs = [ pkgs.curl pkgs.jq ];
		text = ''
			unit="''${1:-unknown-unit}"
			echo "horus-alert: $unit failed"
			jid=$(jq -r 'to_entries | map(select(.key | startswith("kurt")))[0].value // empty' \
				/home/a3chron/horus/memory/whatsapp-contacts.json 2>/dev/null || true)
			if [ -n "$jid" ]; then
				payload=$(jq -n --arg to "$jid" --arg text "Heads-up from the host: $unit failed. Check journalctl -u $unit (or --user -u)." '{to:$to,text:$text}')
				curl -sf -m 15 -X POST -H 'content-type: application/json' -d "$payload" \
					http://127.0.0.1:8765/send >/dev/null \
					|| echo "horus-alert: WhatsApp delivery failed (bridge down?) — journal only"
			else
				echo "horus-alert: no kurt JID found — journal only"
			fi
		'';
	};
in
{
	# system-level template (llama-swap, container, briefing, backup, warmup)
	systemd.services."horus-alert@" = {
		description = "Notify Kurt that %i failed";
		serviceConfig = {
			Type = "oneshot";
			ExecStart = "${alertScript}/bin/horus-alert %i";
		};
	};

	# user-level template (voice, kokoro, bt-watch, music, studium, wakeup-drain)
	# — same WhatsApp ping plus a desktop notification in the session
	systemd.user.services."horus-alert@" = {
		description = "Notify Kurt that user unit %i failed";
		# backstop: the alert itself must never fire from another user's session
		# (gdm-greeter's manager runs these units too and produced 16 bogus
		# notifications on 2026-08-07 — root cause fixed in music.nix et al)
		unitConfig.ConditionUser = "a3chron";
		serviceConfig = {
			Type = "oneshot";
			ExecStart = pkgs.writeShellScript "horus-alert-user" ''
				${pkgs.libnotify}/bin/notify-send -u critical "Horus" "$1 failed — check journalctl --user -u $1" || true
				exec ${alertScript}/bin/horus-alert "$1"
			'' + " %i";
		};
	};
}
