{ config, pkgs, ... }:

{
  home.username = "a3chron";
  home.homeDirectory = "/home/a3chron";
  home.stateVersion = "25.11";
	home.sessionPath = [
		"$HOME/.local/bin"
	];

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
	programs.ghostty.enable = true;
  
	# TODO: move to extra file
  xdg.configFile."ghostty/config".text = ''
    theme = dark:Catppuccin Mocha,light:Catppuccin Latte
		background-opacity = 1.00
		selection-foreground = #000
		selection-background = #FEE
		cursor-style = block
		shell-integration-features = no-cursor
		cursor-text = #000
		window-padding-x = 4
		window-padding-y = 2
		window-padding-balance = true
		window-theme = ghostty
    resize-overlay-position = bottom-right
  '';

  # Shell Prompt
  programs.starship = {
    enable = true;

    enableBashIntegration = true;
    enableFishIntegration = true;
  };
  programs.bash.enable = true;
  programs.fish = {
		enable = true;

		# Fish
		functions.fish_greeting = {
			body = ''
				set hour (date +%H)

				if test $hour -lt 12
					set greeting "Good morning"
				else if test $hour -lt 18
					set greeting "Good afternoon"
				else if test $hour -lt 21
					set greeting "Good evening"
				else
					set greeting "Stayin up late, ain't we"
				end

				echo "$greeting a3chron"
			'';
		};
	};
  
  # Neovim
	programs.neovim = {
		enable = true;
		plugins = with pkgs.vimPlugins; [
			catppuccin-nvim
			nvim-treesitter.withAllGrammars
			telescope-nvim
			nvim-lspconfig
		];
		extraLuaConfig = ''
			-- Set Catppuccin colorscheme
			vim.cmd.colorscheme "catppuccin"
			require("catppuccin").setup({
				flavour = "mocha",
				transparent_background = true,
			})
			vim.cmd.colorscheme "catppuccin"
			vim.opt.number = true
			vim.opt.relativenumber = true
			vim.opt.ts = 2
			vim.opt.shiftwidth = 2

			-- Enable Treesitter highlighting
			require('nvim-treesitter.configs').setup({
				highlight = {
					enable = true,
				},
			})
		'';
	};


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

		gtk3.extraCss = ''
			@import url("custom-window-buttons.css");
		'';

		gtk4.extraCss = ''
			@import url("custom-window-buttons.css");
		'';
  };

  home.pointerCursor = {
    name = "Vimix-cursors";
    package = pkgs.vimix-cursors;
    size = 24;
    gtk.enable = true;
    x11.enable = true;
  };

  systemd.user.services.vicinae = {
		Unit = {
			Description = "Vicinae server";
			After = [ "graphical-session.target" ];
		};

		Service = {
			ExecStart = "${pkgs.vicinae}/bin/vicinae server";
			Restart = "always";
			RestartSec = 2;
		};

		Install = {
			WantedBy = [ "graphical-session.target" ];
		};
	};

  dconf.settings = {
		"org/gnome/settings-daemon/plugins/media-keys" = {
			custom-keybindings = [
				"/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/terminal/"
				"/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/vicinae/"
			];
		};

		"org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/terminal" = {
			name = "Terminal";
			command = "${pkgs.ghostty}/bin/ghostty";
			binding = "<Ctrl><Alt>t";
		};

		"org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/vicinae" = {
			name = "Vicinae Toggle";
			command = "${pkgs.vicinae}/bin/vicinae toggle";
			binding = "<Ctrl>space";
		};
	};

  # Import the CSS file for both GTK 3 and GTK 4
	home.file.".config/gtk-3.0/custom-window-buttons.css".source = ./window-buttons.css;
	home.file.".config/gtk-4.0/custom-window-buttons.css".source = ./window-buttons.css;

  # User Packages
  home.packages = with pkgs; [
    # basics
    starship
    vscodium
    vlc
    vicinae
		obs-studio
		claude-code

    # gnome
    gnome-tweaks
    gnome-shell
    gnome-shell-extensions
    gnome-extension-manager

    # other
    #bambu-studio //TODO: currently installed via flatpak, somehow move to nix config
    blender
    neofetch
		bagels
    steam
  ];
}
