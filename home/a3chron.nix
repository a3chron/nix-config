{ config, pkgs, ... }:

{
  home.username = "a3chron";
  home.homeDirectory = "/home/a3chron";
  home.stateVersion = "25.11";

  programs.home-manager.enable = true;

  # Git
  programs.git = {
    enable = true;

    settings.user = {
      name  = "a3chron";
      email = "kurt.schambach@gmail.com";
    };
  };

  # Shell
  programs.starship = {
    enable = true;

    settings = pkgs.lib.importTOML ./starship-themes/starship-active.toml;

    enableBashIntegration = true;
    enableFishIntegration = true;
  };
  programs.fish.enable = true;
  programs.bash.enable = true;

  home.file.".config/starship-themes/red.toml".source = ./starship-themes/red.toml;
  home.file.".config/starship-themes/blue.toml".source = ./starship-themes/blue.toml;
  home.file.".config/starship-themes/green.toml".source = ./starship-themes/green.toml;

  # Create initial symlink (only runs if file doesn't exist)
  home.activation.setupStarshipTheme = config.lib.dag.entryAfter ["writeBoundary"] ''
    if [ ! -e "$HOME/.config/starship.toml" ]; then
      $DRY_RUN_CMD ln -s "$HOME/.config/starship-themes/blue.toml" "$HOME/.config/starship.toml"
    fi
  '';

  # Theme
  gtk = {
    enable = true;

    theme = {
      name = "Catppuccin-GTK-Dark";
      package = pkgs.magnetic-catppuccin-gtk;
    };

    iconTheme = {
      name = "Flat-Remix-Blue-Dark";
      package = pkgs.flat-remix-icon-theme;
    };
  };

  home.pointerCursor = {
    name = "Vimix-cursors";
    package = pkgs.vimix-cursors;
    size = 24;
    gtk.enable = true;
    x11.enable = true;
  };


  # Custom window buttons
  home.file.".config/gtk-3.0/custom-window-buttons.css".source = ./window-buttons.css;
  home.file.".config/gtk-4.0/custom-window-buttons.css".source = ./window-buttons.css;

  # Tell GTK to load them
  home.sessionVariables = {
    GTK_CSS  = "$HOME/.config/gtk-3.0/custom-window-buttons.css";
    GTK_CSS4 = "$HOME/.config/gtk-4.0/custom-window-buttons.css";
  };

  # User Packages
  home.packages = with pkgs; [
    # basics
    starship
    vscodium
    vlc

    # gnome
    gnome-tweaks
    gnome-shell
    gnome-shell-extensions
    gnome-extension-manager

    # other
    #bambu-studio //TODO: currently installed via flatpak, somehow move to nix config
    blender
    neofetch

    # custom bin scripts TODO: move to extra file
    (pkgs.writeShellScriptBin "starship-theme" ''
      THEME="$1"
      THEMES="$HOME/.config/starship-themes"
      ACTIVE="$HOME/.config/starship.toml"

      if [[ ! "$THEME" =~ ^(red|blue|green)$ ]]; then
          echo "Usage: starship-theme [red|blue|green]"
          exit 1
      fi

      ln -sf "$THEMES/$THEME.toml" "$ACTIVE"
      echo "✨ Starship theme switched to $THEME"
    '')
  ];
}
