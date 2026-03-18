{ config, pkgs, lib, ... }:

{
  home.username = "a3chron";
  home.homeDirectory = "/home/a3chron";
  home.stateVersion = "25.11";
	home.sessionVariables = {
		GTK_IM_MODULE = "simple"; # Fix ghostty dead-key (`)
	};
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
			zen-mode-nvim
			nvim-autopairs
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

			-- Exit insert mode with jk
		  vim.keymap.set("i", "jk", "<Esc>")

			-- Enable Treesitter highlighting
			require('nvim-treesitter.configs').setup({
				highlight = {
					enable = true,
				},
			})

			-- ZenMode setup
			require("zen-mode").setup({
				window = {
					width = 90,
				},
			})
			vim.keymap.set("n", "<leader>z", "<cmd>ZenMode<cr>")
			vim.api.nvim_create_user_command("Zen", "ZenMode", {})
			vim.cmd("cabbrev zen ZenMode")

			-- Auto-enter ZenMode for markdown files
			vim.api.nvim_create_autocmd("FileType", {
				pattern = "markdown",
				callback = function()
					vim.schedule(function()
						require("zen-mode").toggle()
					end)
				end,
			})

			-- Auto-closing brackets
			require("nvim-autopairs").setup({
				check_ts = true,
			})
		'';
	};

	programs.vscode = {
		enable = true;
		package = pkgs.vscodium;

		profiles.default = {
			extensions = (with pkgs.vscode-extensions; [
				anthropic.claude-code
				biomejs.biome
				bradlc.vscode-tailwindcss
				catppuccin.catppuccin-vsc
				golang.go
				jnoortheen.nix-ide
				unifiedjs.vscode-mdx
			]) ++ (pkgs.vscode-utils.extensionsFromVscodeMarketplace [
				{
					name = "catppuccin-noctis-icons";
					publisher = "alexdauenhauer";
					version = "0.3.0";
					sha256 = "sha256-fubzcWxEZ7zSLbJKqbmto+tNg9W7i0x3zI9LJHB4OcQ=";
				}
				{
					name = "qt-core";
					publisher = "theqtcompany";
					version = "1.12.0";
					sha256 = "sha256-X8YzpmZbMWAfLv3YjBr/jDqEMakzUBNQViiJLXah+3I=";
				}
				{
					name = "qt-qml";
					publisher = "theqtcompany";
					version = "1.12.0";
					sha256 = "sha256-LNfVsmM4Wiv5RWk5ne2Z0lOonPEFH2405xKX/D3eCgY=";
				}
				{
					name = "vscode-todo-highlight";
					publisher = "wayou";
					version = "1.0.5";
					sha256 = "sha256-CQVtMdt/fZcNIbH/KybJixnLqCsz5iF1U0k+GfL65Ok=";
				}
			]);

			userSettings = {
				"workbench.colorTheme" = "Catppuccin Mocha";
				"catppuccin.accentColor" = "blue";
				"workbench.iconTheme" = "catppuccin noctis icons";
				"catppuccin-noctis-icons.hidesExplorerArrows" = false;
				"workbench.editorAssociations" = {
					"{git,gitlens,chat-editing-snapshot-text-model,copilot,git-graph,git-graph-3}:/**/*.qrc" = "default";
					"*.qrc" = "qt-core.qrcEditor";
				};
				"window.controlsStyle" = "custom";
			};
		};
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

	# Populate hyperland config
		home.file.".config/hypr/hyprland.conf".source = ./hyprland.conf;

	# Ambxst config - conditional copy (only if dir doesn't exist)
	# This keeps config declarative but allows in-app changes
	home.activation.ambxstConfig = lib.hm.dag.entryAfter ["writeBoundary"] ''
		if [ ! -d "$HOME/.config/ambxst" ]; then
			mkdir -p "$HOME/.config/ambxst"
			cp -r ${./ambxst}/* "$HOME/.config/ambxst/"
			chmod -R u+w "$HOME/.config/ambxst"
		fi
	'';

	# For Nerd fonts
	fonts.fontconfig.enable = true;

  # User Packages
  home.packages = with pkgs; [
    # basics
    starship
    vscodium
    vlc
    vicinae
		obs-studio
		claude-code
		prismlauncher
		obsidian
		openrgb

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
		nerd-fonts.jetbrains-mono
  ];
}
