# Horus — local LLM agent stack (see ~/horus and the plan in the nixos-config repo)
{ ... }:

{
	imports = [
		./llm.nix
		./container.nix
		./searxng.nix
		./cli.nix
	];
}
