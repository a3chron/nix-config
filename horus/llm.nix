# LLM serving: llama-swap proxy (OpenAI-compatible, :8080) spawning llama-server
# (Vulkan) on demand. TTL auto-unloads the model -> VRAM freed when idle.
{ config, pkgs, lib, inputs, ... }:

let
	unstable = inputs.nixpkgs-unstable.legacyPackages.${pkgs.stdenv.hostPlatform.system};
	llama-cpp = unstable.llama-cpp.override { vulkanSupport = true; };
	llama-swap = unstable.llama-swap;

	modelsDir = "/var/lib/llm/models";

	# Proactively warm the model + prompt cache (gated on GPU-heavy apps and on a
	# paused stack). Impure repo path like the voice scripts, so tuning the gate
	# needs no rebuild. Called by the boot oneshot below and by horus-bt-watch on
	# headphone connect.
	horusWarmup = pkgs.writeShellApplication {
		name = "horus-warmup";
		runtimeInputs = [ pkgs.curl pkgs.jq pkgs.procps ];
		text = ''
			exec ${pkgs.runtimeShell} /home/a3chron/nixos-config/horus/horus-warmup.sh "$@"
		'';
	};

	swapConfig = (pkgs.formats.yaml { }).generate "llama-swap.yaml" {
		healthCheckTimeout = 600; # first load reads 26GB from disk
		models = {
			"qwen3.6-35b" = {
				# ${PORT} is a llama-swap macro, not shell — escaped for Nix below
				# --n-cpu-moe 30 re-verified optimal 2026-08-05 (llama-bench, desktop using
				# ~2.2GiB VRAM): 30 → 33.4 tok/s @ 9.9GiB total; 28 → 34.1 @ 10.9 (no
				# headroom for whisper); 26 → 25.6 @ 11.8 (spills); 24 → pp COLLAPSES to
				# 90 tok/s; 32 → 32.7. -ub/-b changes: no effect. Don't lower it.
				# sampling pinned to the GGUF author's recommendation (general.sampling.*
				# metadata: temp 1.0 / top-p 0.95 / top-k 20). Without this the effective
				# values were llama-server's defaults (0.8/0.95/40) — chosen by nobody and
				# silently changeable by any flake bump. opencode sends no sampling params
				# for custom providers (capabilities.temperature=false), so this is the
				# single authoritative place.
				cmd = ''
					${llama-cpp}/bin/llama-server
					--port ''${PORT}
					-m ${modelsDir}/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf
					--jinja
					--flash-attn on
					--cache-type-k q8_0
					--cache-type-v q8_0
					--ctx-size 65536
					--n-gpu-layers 999
					--n-cpu-moe 30
					--temp 1.0
					--top-p 0.95
					--top-k 20
				'';
				ttl = 28800; # unload after 8h idle (4h evicted the model between afternoon and the 19:45-23:00 voice cluster — 7 of 9 evening rounds paid a ~60s cold start); heavy GPU work = `horus pause`
			};
		};
	};
in
{
	systemd.tmpfiles.rules = [
		"d /var/lib/llm 0755 a3chron users -"
		"d ${modelsDir} 0755 a3chron users -"
	];

	systemd.services.llama-swap = {
		description = "llama-swap LLM proxy (Horus)";
		wantedBy = [ "multi-user.target" ];
		after = [ "network.target" ];
		onFailure = [ "horus-alert@%n.service" ];
		# don't die permanently after 5 quick failures (default burst limit) —
		# alert instead, keep trying every 5s
		unitConfig.StartLimitIntervalSec = 0;
		serviceConfig = {
			RestartSec = 5;
			ExecStart = "${llama-swap}/bin/llama-swap --config ${swapConfig} --listen 127.0.0.1:8080";
			DynamicUser = true;
			# GPU access for the spawned llama-server (Vulkan/RADV)
			SupplementaryGroups = [ "video" "render" ];
			CacheDirectory = "llama-swap"; # shader cache
			Environment = [ "XDG_CACHE_HOME=/var/cache/llama-swap" ];
			Restart = "on-failure";
			# model files are mmap'd; don't let systemd OOM-score this too aggressively
			OOMScoreAdjust = 200;
		};
	};

	environment.systemPackages = [ horusWarmup ];

	# Warm the model shortly AFTER boot, off the critical path (skips itself if a
	# GPU-heavy app is running — see horus-warmup.sh). NOT wantedBy
	# multi-user.target: as a boot oneshot systemd ordered it before
	# multi-user.target, so graphical.target (gdm/Hyprland) waited out the full
	# ~60s cold model load — a long grey screen every boot. A timer decouples it:
	# the desktop comes up at once and the warmup runs behind it. The script
	# self-gates on llama-swap/container readiness, so no ExecStartPre sleep.
	systemd.services.horus-warmup = {
		description = "Warm the Horus LLM + prompt cache";
		after = [ "llama-swap.service" "container@horus.service" "network-online.target" ];
		wants = [ "network-online.target" ];
		onFailure = [ "horus-alert@%n.service" ];
		serviceConfig = {
			Type = "oneshot";
			ExecStart = "${horusWarmup}/bin/horus-warmup";
			# priming does a full cold prefill of the ~10k-token prefix
			TimeoutStartSec = 300;
		};
	};

	systemd.timers.horus-warmup = {
		wantedBy = [ "timers.target" ];
		timerConfig = {
			# let the desktop settle first, then warm the model in the background
			OnBootSec = "45s";
			# evening prime: voice usage clusters 19:45-23:05 and the idle TTL evicts
			# the model over the afternoon — warm it before the evening starts.
			# (the script self-gates: skips when loaded / paused / GPU-heavy app)
			OnCalendar = "*-*-* 19:15:00";
		};
	};
}
