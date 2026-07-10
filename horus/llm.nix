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
				'';
				ttl = 14400; # unload after 4h idle — fewer cold starts across a day of on/off use; heavy GPU work = `horus pause` (browsing/music/YouTube coexist fine with it loaded)
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
		serviceConfig = {
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
		serviceConfig = {
			Type = "oneshot";
			ExecStart = "${horusWarmup}/bin/horus-warmup";
			# priming does a full cold prefill of the ~10k-token prefix
			TimeoutStartSec = 300;
		};
	};

	systemd.timers.horus-warmup = {
		wantedBy = [ "timers.target" ];
		# let the desktop settle first, then warm the model in the background
		timerConfig.OnBootSec = "45s";
	};
}
