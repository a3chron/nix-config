# Horus — local LLM agent stack (see ~/horus and the plan in the nixos-config repo)
{ ... }:

{
	imports = [
		./llm.nix
		./container.nix
		./searxng.nix
		./cli.nix
		./voice.nix
		./music.nix
		./read.nix
		./studium.nix
		./briefing.nix
		./backup.nix
		./wakeup.nix
		./morning.nix
		./alert.nix
		./wa-watch.nix
	];
}
