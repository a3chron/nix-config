# The `horus` CLI: chat (default) / pause / resume / status.
# Narrow NOPASSWD sudo rules make pause/resume instant for a3chron.
{ config, pkgs, lib, ... }:

let
	horus = pkgs.writeShellApplication {
		name = "horus";
		runtimeInputs = [ pkgs.curl pkgs.jq pkgs.python3 ];
		text = ''
			cmd="''${1:-chat}"

			resume() {
				sudo systemctl start llama-swap.service
				if ! systemctl is-active -q container@horus.service; then
					sudo systemctl start container@horus.service
				fi
			}

			case "$cmd" in
			chat)
				resume
				exec sudo machinectl shell horus@horus /run/current-system/sw/bin/bash -c \
					'cd /home/horus/work && exec opencode'
				;;
			resume)
				resume
				echo "horus: stack up (model loads on first request)"
				# wait for the WhatsApp bridge to reconnect, then report whether
				# messages arrived while paused (the bridge answers them on its own)
				st=""
				for _ in $(seq 1 40); do
					st=$(curl -sf --max-time 2 http://127.0.0.1:8765/status | jq -r '.status' || true)
					if [ "$st" = "connected" ]; then break; fi
					sleep 1
				done
				if [ "$st" != "connected" ]; then
					echo "whatsapp: ''${st:-bridge unreachable} — offline? bridge keeps retrying and catches up once online"
				else
					sleep 8 # WhatsApp replays while-away messages just after connect; responder debounce is 5s
					info=$(curl -sf --max-time 2 http://127.0.0.1:8765/status || echo '{}')
					pending=$(echo "$info" | jq -r '.pending // 0')
					running=$(echo "$info" | jq -r '.running // false')
					unknown=$(echo "$info" | jq -r '.unknownCount // 0')
					if [ "$running" = "true" ] || [ "$pending" -gt 0 ]; then
						echo "whatsapp: connected — messages arrived while away, agent is answering them now"
					else
						echo "whatsapp: connected — nothing waiting"
					fi
					if [ "$unknown" -gt 0 ]; then
						echo "whatsapp: note: $unknown message(s) from unknown senders held back"
					fi
				fi
				;;
			pause)
				sudo systemctl stop container@horus.service
				sudo systemctl stop llama-swap.service
				echo "horus: paused — container stopped, model unloaded, VRAM freed"
				;;
			status)
				printf 'container:  %s\n' "$(systemctl is-active container@horus.service)"
				printf 'llama-swap: %s\n' "$(systemctl is-active llama-swap.service)"
				if systemctl is-active -q llama-swap.service; then
					loaded=$(curl -sf --max-time 2 http://127.0.0.1:8080/running \
						| jq -r '.running[]?.model' 2>/dev/null | paste -sd, -)
					printf 'loaded:     %s\n' "''${loaded:-none}"
				fi
				if wa=$(curl -sf --max-time 2 http://127.0.0.1:8765/status); then
					printf 'whatsapp:   %s\n' "$(echo "$wa" | jq -r '.status
						+ (if .running then " — answering now" elif (.pending // 0) > 0 then " — \(.pending) queued" else "" end)
						+ (if .status != "connected" and .lastCloseReason then " — last close \(.lastCloseReason)" else "" end)
						+ (if .status != "connected" and .lastConnectedAt then ", last connected \(.lastConnectedAt)" else "" end)')"
				else
					printf 'whatsapp:   %s\n' "unreachable (container down?)"
				fi
				for d in /sys/class/drm/card*/device; do
					if [ -f "$d/mem_info_vram_used" ] && [ "$(cat "$d/mem_info_vram_total")" -gt 4000000000 ]; then
						used=$(( $(cat "$d/mem_info_vram_used") / 1024 / 1024 ))
						total=$(( $(cat "$d/mem_info_vram_total") / 1024 / 1024 ))
						printf 'vram:       %s/%s MiB\n' "$used" "$total"
					fi
				done
				;;
			log)
				# live voice-pipeline view: what whisper heard, what horus replied.
				# runs from the repo path (impure by design) so tweaks need no rebuild
				exec python3 /home/a3chron/nixos-config/horus/horus-log.py
				;;
			grant)
				# live-bind one of my ~/Projects into the running container so horus
				# can work on it. machinectl bind is transient: it vanishes on the
				# next container restart, so grants are session-scoped for free.
				# opencode still prompts me to approve each edit (external_directory
				# "ask" on /home/horus/projects/*). horus can't do this itself — it's
				# unprivileged inside the sandbox; running this IS the approval.
				proj="''${2:-}"
				if [ -z "$proj" ]; then
					echo "usage: horus grant <project>" >&2; exit 1
				fi
				case "$proj" in
					*/*|*..*) echo "horus: invalid project name '$proj'" >&2; exit 1 ;;
				esac
				src="/home/a3chron/Projects/$proj"
				if [ ! -d "$src" ]; then
					echo "horus: no such project: $src" >&2; exit 1
				fi
				if ! systemctl is-active -q container@horus.service; then
					echo "horus: container not running — 'horus resume' first" >&2; exit 1
				fi
				sudo machinectl bind --mkdir horus "$src" "/home/horus/projects/$proj"
				echo "horus: granted '$proj' for this session (clears on 'horus pause')"
				;;
			revoke)
				# drop a grant mid-session; 'horus pause'/'resume' clears ALL grants anyway,
				# so this is just occasional cleanup. umount must run as root INSIDE the
				# container (the bind lives in the container's mount namespace) — this is
				# NOT in the NOPASSWD set on purpose (a wildcard root shell is too broad to
				# hand out passwordless), so revoke prompts for the sudo password. grant,
				# the frequent path, stays passwordless. machinectl doesn't reliably
				# propagate the inner exit code, so we key off a stdout marker.
				proj="''${2:-}"
				if [ -z "$proj" ]; then
					echo "usage: horus revoke <project>" >&2; exit 1
				fi
				case "$proj" in
					*/*|*..*) echo "horus: invalid project name '$proj'" >&2; exit 1 ;;
				esac
				out=$(sudo machinectl shell root@horus /run/current-system/sw/bin/bash -c \
					"if grep -q ' /home/horus/projects/$proj ' /proc/mounts; then umount /home/horus/projects/$proj && rmdir /home/horus/projects/$proj && echo REVOKED; else echo NOTMOUNTED; fi")
				case "$out" in
					*REVOKED*)    echo "horus: revoked '$proj'" ;;
					*NOTMOUNTED*) echo "horus: '$proj' was not granted" ;;
					*)            echo "horus: revoke failed" >&2; exit 1 ;;
				esac
				;;
			*)
				echo "usage: horus [chat|pause|resume|status|log|grant <project>|revoke <project>]" >&2
				exit 1
				;;
			esac
		'';
	};
in
{
	environment.systemPackages = [ horus ];

	security.sudo.extraRules = [
		{
			users = [ "a3chron" ];
			commands = map (c: { command = c; options = [ "NOPASSWD" ]; }) [
				"/run/current-system/sw/bin/systemctl start llama-swap.service"
				"/run/current-system/sw/bin/systemctl stop llama-swap.service"
				"/run/current-system/sw/bin/systemctl start container@horus.service"
				"/run/current-system/sw/bin/systemctl stop container@horus.service"
				"/run/current-system/sw/bin/machinectl shell horus@horus *"
				# `horus grant <project>`: single-segment names only (wrapper rejects
				# '/' and '..'), so a grant can't escape ~/Projects
				"/run/current-system/sw/bin/machinectl bind --mkdir horus /home/a3chron/Projects/* /home/horus/projects/*"
			];
		}
	];
}
