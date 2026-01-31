{ pkgs }:
with pkgs; [
  # Do not forget to add an editor to edit configuration.nix! The Nano editor is also installed by default.
  solaar
  usbutils
  alacritty
  git
  bash
  fish
  lm_sensors

	lenovo-legion
  pkgs.linuxKernel.packages.linux_6_12.lenovo-legion-module
  
  # could maybe be moved to flakes?
  postgresql
]
