{ pkgs }:
with pkgs; [
  # Do not forget to add an editor to edit configuration.nix! The Nano editor is also installed by default.
  alacritty
  git
  bash
  fish
  lm_sensors

	# hyperland
	wl-clipboard
	wofi
	hyprshot
	hyprlock
	hyprpaper
  
  # could maybe be moved to flakes?
  postgresql
]
