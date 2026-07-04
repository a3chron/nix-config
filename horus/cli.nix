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
					printf 'whatsapp:   %s\n' "$(echo "$wa" | jq -r '.status + (if .running then " — answering now" elif (.pending // 0) > 0 then " — \(.pending) queued" else "" end)')"
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
			*)
				echo "usage: horus [chat|pause|resume|status|log]" >&2
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
			];
		}
	];
}
