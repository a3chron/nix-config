# The `horus` CLI: chat (default) / pause / resume / status.
# Narrow NOPASSWD sudo rules make pause/resume instant for a3chron.
{ config, pkgs, lib, ... }:

let
	horus = pkgs.writeShellApplication {
		name = "horus";
		runtimeInputs = [ pkgs.curl pkgs.jq ];
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
					printf 'loaded:     %s\n' \
						"$(curl -sf --max-time 2 http://127.0.0.1:8080/running \
							| jq -r '.running[]?.model // empty' 2>/dev/null || echo none)"
				fi
				for d in /sys/class/drm/card*/device; do
					if [ -f "$d/mem_info_vram_used" ] && [ "$(cat "$d/mem_info_vram_total")" -gt 4000000000 ]; then
						used=$(( $(cat "$d/mem_info_vram_used") / 1024 / 1024 ))
						total=$(( $(cat "$d/mem_info_vram_total") / 1024 / 1024 ))
						printf 'vram:       %s/%s MiB\n' "$used" "$total"
					fi
				done
				;;
			*)
				echo "usage: horus [chat|pause|resume|status]" >&2
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
