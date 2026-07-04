# LLM serving: llama-swap proxy (OpenAI-compatible, :8080) spawning llama-server
# (Vulkan) on demand. TTL auto-unloads the model -> VRAM freed when idle.
{ config, pkgs, lib, inputs, ... }:

let
	unstable = inputs.nixpkgs-unstable.legacyPackages.${pkgs.stdenv.hostPlatform.system};
	llama-cpp = unstable.llama-cpp.override { vulkanSupport = true; };
	llama-swap = unstable.llama-swap;

	modelsDir = "/var/lib/llm/models";

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
}
