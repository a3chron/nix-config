# Failure notification path (there was none): `OnFailure=horus-alert@%n.service`
# on every Horus unit. The alert pings Kurt over WhatsApp via the bridge's
# localhost API (works from host — shared netns) and, for user units, also
# raises a desktop notification. Best-effort: if the bridge itself is down the
# journal entry is all that remains, but at least `horus status` now lists all
# units too (cli.nix).
#
# ONE MESSAGE PER EPISODE (2026-08-11). Every repeat-alert bug so far was fixed
# at its source — the greeter restart loop (music.nix), the bare `pgrep` in a
# minutely timer (horus-morning.py) — but the alert path itself would happily
# send a WhatsApp per failure forever, and with `Restart=always` +
# `StartLimitIntervalSec=0` on most daemons that is unbounded. A broken thing is
# still broken after the first message, so:
#
#   * first failure of an episode -> WhatsApp + desktop notification
#   * repeats -> journal only, silent
#   * a NEW episode (= the unit reached `active` again after the last alert, and
#     the cooldown has passed) -> alerts again
#
# Persistently-broken-and-never-recovered therefore alerts exactly ONCE: the
# missing-EnvironmentFile crash loop that started this (opencode-server, hence
# the approval broker) sent one message every 10 minutes for an hour otherwise.
# State is monotonic (/proc/uptime) in tmpfs (XDG_RUNTIME_DIR or /run), so a
# reboot re-arms everything — a problem that survives a reboot is worth one
# fresh message.
{ config, pkgs, lib, ... }:

let
	alertScript = pkgs.writeShellApplication {
		name = "horus-alert";
		runtimeInputs = [ pkgs.curl pkgs.jq pkgs.libnotify pkgs.systemd ];
		text = ''
			# usage: horus-alert <unit> [--user]   (--user = user manager + desktop notification)
			unit="''${1:-unknown-unit}"
			mode="''${2:-}"
			cooldown="''${HORUS_ALERT_COOLDOWN:-3600}"

			echo "horus-alert: $unit failed"   # the journal ALWAYS gets every failure

			# --- dedup ------------------------------------------------------
			# Monotonic seconds since boot, matching ActiveEnterTimestampMonotonic;
			# no wall-clock parsing and immune to a clock jump.
			now=$(cut -d. -f1 /proc/uptime)
			statedir="''${XDG_RUNTIME_DIR:-/run/horus-alert}"
			mkdir -p "$statedir" 2>/dev/null || true
			state="$statedir/horus-alert.''${unit//\//_}"
			last=$(cat "$state" 2>/dev/null || true)
			case "$last" in ""|*[!0-9]*) last="" ;; esac   # ignore junk/first run

			if [ -n "$last" ]; then
				if [ "$(( now - last ))" -lt "$cooldown" ]; then
					echo "horus-alert: already reported $(( now - last ))s ago — journal only"
					exit 0
				fi
				# systemctl resolves a bare name to .service, and prints 0 for a
				# unit that never came up — which is exactly the "still the same
				# broken episode" case we want to stay quiet about.
				active_us=$(systemctl ''${mode:+--user} show -p ActiveEnterTimestampMonotonic --value "$unit" 2>/dev/null || echo 0)
				case "$active_us" in ""|*[!0-9]*) active_us=0 ;; esac
				if [ "$(( active_us / 1000000 ))" -le "$last" ]; then
					echo "horus-alert: same episode (never recovered since the last alert) — journal only"
					exit 0
				fi
			fi
			printf '%s' "$now" > "$state" 2>/dev/null || true

			# --- deliver ----------------------------------------------------
			if [ -n "$mode" ]; then
				notify-send -u critical "Horus" "$unit failed — check journalctl --user -u $unit" || true
			fi
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
			# --user: query the user manager for the recovery check, and raise the
			# desktop notification. Both live in the script now so the dedup
			# covers the notification too — it used to fire on every repeat.
			ExecStart = "${alertScript}/bin/horus-alert %i --user";
		};
	};
}
