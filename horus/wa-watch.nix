# Watchdog for the WhatsApp bridge's one unrecoverable state.
#
# Why this exists separately from alert.nix: every other Horus unit reports its
# own failure through `OnFailure=horus-alert@`, which delivers over the bridge's
# /send endpoint. That path structurally cannot report a bridge logout — the
# messenger is the thing that's broken. Worse, a logged-out bridge is not a
# systemd failure at all: the process stays up serving /status (deliberately, so
# "needs re-pairing" stays distinguishable from "container down"), so OnFailure
# never fires and nothing at all reaches Kurt.
#
# That gap cost 33h of silence on 2026-08-06/07: the pairing was revoked at
# 07:29, two messages were sent to a deaf bridge, and the first sign of trouble
# was Kurt noticing he'd had no reply.
#
# So: poll from the host (shared netns — localhost reaches the container) and
# raise a DESKTOP notification, the one channel that doesn't depend on WhatsApp.
{ config, pkgs, lib, ... }:

let
	watchScript = pkgs.writeShellApplication {
		name = "horus-wa-watch";
		runtimeInputs = [ pkgs.curl pkgs.jq pkgs.libnotify pkgs.systemd ];
		text = ''
			state="''${XDG_RUNTIME_DIR:-/tmp}/horus-wa-watch.state"
			misses="''${XDG_RUNTIME_DIR:-/tmp}/horus-wa-watch.misses"

			# `horus pause` stops the container on purpose — never nag about that
			if ! systemctl is-active -q container@horus.service; then
				rm -f "$misses"
				exit 0
			fi

			body=$(curl -sf -m 5 http://127.0.0.1:8765/status || true)

			if [ -z "$body" ]; then
				n=$(( $(cat "$misses" 2>/dev/null || echo 0) + 1 ))
				echo "$n" > "$misses"
				# the bridge is briefly unreachable on every container start;
				# only shout once it has missed three polls (~15 min)
				if [ "$n" -lt 3 ]; then exit 0; fi
				key="unreachable"
				msg="Bridge unreachable for ~$(( n * 5 )) min while the container is up. Check: journalctl -M horus -u wa-bridge"
			else
				rm -f "$misses"
				st=$(printf '%s' "$body" | jq -r '.status // "?"' 2>/dev/null || echo "?")
				case "$st" in
				logged-out)
					# key on the episode so a re-pair + later re-logout alerts again
					at=$(printf '%s' "$body" | jq -r '.loggedOutAt // "?"' 2>/dev/null || echo "?")
					key="logged-out:$at"
					msg="Pairing revoked — Horus can't hear you and won't retry. Re-pair: rm -rf ~/horus/wa-auth && sudo systemctl restart container@horus.service, then scan the QR in ~/horus/bridge/bridge.log"
					;;
				*)
					exit 0
					;;
				esac
			fi

			# one notification per episode. State lives in XDG_RUNTIME_DIR, so a
			# reboot with the problem still present re-alerts — which is right:
			# that is exactly when Kurt is back at the machine to fix it.
			prev=$(cat "$state" 2>/dev/null || true)
			if [ "$prev" = "$key" ]; then exit 0; fi
			printf '%s' "$key" > "$state"

			echo "horus-wa-watch: $msg"
			notify-send -u critical "Horus WhatsApp" "$msg" || true
		'';
	};
in
{
	systemd.user.services.horus-wa-watch = {
		description = "Watch the Horus WhatsApp bridge for an unrecoverable logout";
		unitConfig = {
			OnFailure = [ "horus-alert@%n.service" ];
			ConditionUser = "a3chron"; # see music.nix
		};
		serviceConfig = {
			Type = "oneshot";
			ExecStart = "${watchScript}/bin/horus-wa-watch";
			TimeoutStartSec = 60;
		};
	};

	systemd.user.timers.horus-wa-watch = {
		wantedBy = [ "timers.target" ];
		timerConfig = {
			OnBootSec = "5min";
			OnUnitActiveSec = "5min";
			Persistent = false;
		};
	};
}
