{ config, pkgs, lib, ... }:

let
	# Claude Code status line. Kept as a plain script in the repo so it can be
	# run/edited standalone (`echo '{}' | ./home/claude/statusline.sh`), but
	# wrapped here so jq/git/tput resolve from the store instead of $PATH —
	# Claude spawns it with a minimal environment, and a missing jq degrades
	# silently into a blank line rather than an error.
	#
	# NOTE: deliberately writeShellScriptBin and not writeShellApplication.
	# The latter injects `set -euo pipefail`, and the script leans on trailing
	# `[ test ] && assignment` idioms whose non-zero exit is meaningful, not
	# fatal — under `set -e` the first colour threshold that doesn't match
	# would kill the whole status line.
	claudeStatusline = pkgs.writeShellScriptBin "claude-statusline" ''
		export PATH=${lib.makeBinPath (with pkgs; [ jq git ncurses coreutils ])}''${PATH:+:}$PATH
		${builtins.readFile ./claude/statusline.sh}
	'';
in
{
  home.username = "a3chron";
  home.homeDirectory = "/home/a3chron";
  home.stateVersion = "25.11";
	# NOTE: home.sessionVariables only lands in hm-session-vars.{sh,fish}, i.e. it
	# reaches interactive shells only. GUI apps are started by
	# systemd --user -> uwsm -> Hyprland, which never sources those, so they used
	# to get GTK_IM_MODULE unset -> GTK4 picked its Wayland text-input-v3 context
	# -> nothing composed dead keys / Multi_key (they arrived as a bare ESC).
	# systemd.user.sessionVariables writes ~/.config/environment.d/, which the
	# user manager and therefore every GUI app does inherit. Needs a re-login.
	systemd.user.sessionVariables = {
		GTK_IM_MODULE = "simple"; # compose key support in ghostty / zen
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
				"terminal.integrated.defaultProfile.linux" = "fish";
				"editor.lineNumbers" = "relative";
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

	# ambxst (the quickshell desktop shell: bar, launcher, wallpaper, lockscreen)
	# as a supervised unit instead of a Hyprland exec at startup.
	#
	# Why: 2026-09-03, monitor powered off and back on. This Fujitsu drops the
	# HDMI link when it powers off, Hyprland removes its only output, and the
	# Wayland protocol error that followed was fatal for quickshell (and Zen).
	# Hyprland itself survived, so the result was the bare grey "hypr just
	# better" fallback: mouse only, no bar, no wallpaper, and every SUPER bind
	# apparently dead because they all `ambxst run ...`. Restart=always brings
	# it back within seconds. Stop it for real with `systemctl --user stop ambxst`
	# (`ambxst quit` alone will just be undone by the restart).
	#
	# ambxst is installed imperatively (`nix profile install github:a3chron/ambxst-a3`),
	# hence the ~/.nix-profile path rather than a pkgs reference.
	# home/hyprland.lua no longer execs it and filters the exec that ambxst's
	# generated config insists on, so this unit is the single launcher.
	# The environment (WAYLAND_DISPLAY, HYPRLAND_INSTANCE_SIGNATURE) reaches the
	# user manager through uwsm, same as vicinae above; the Condition keeps it
	# from starting under a non-Hyprland session (e.g. the GNOME fallback).
	systemd.user.services.ambxst = {
		Unit = {
			Description = "ambxst desktop shell (quickshell)";
			After = [ "graphical-session.target" ];
			PartOf = [ "graphical-session.target" ];
			ConditionEnvironment = "HYPRLAND_INSTANCE_SIGNATURE";
			# A broken ambxst/quickshell config crashes within a second; cap the
			# loop instead of hammering the compositor forever.
			StartLimitIntervalSec = 60;
			StartLimitBurst = 5;
		};

		Service = {
			ExecStart = "%h/.nix-profile/bin/ambxst";
			Restart = "always";
			RestartSec = 2;
			Slice = "app-graphical.slice";
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

	# Populate hyprland config.
	#
	# Lua, not .conf: Hyprland 0.56 dropped the legacy hyprland.conf format
	# entirely (it is Lua-only), and 0.55 already prefers hyprland.lua whenever
	# one exists. Letting home-manager own this path also stops Hyprland from
	# autogenerating a stock hyprland.lua over it -- which is exactly what
	# happened on 2026-09-03: the 0.56 bump wrote its own default config into
	# ~/.config/hypr/hyprland.lua, that file shadowed hyprland.conf, and because
	# it lives in $HOME rather than the system generation, rolling back to 0.55
	# did not get rid of it. The store symlink is read-only, so it can't recur.
		home.file.".config/hypr/hyprland.lua".source = ./hyprland.lua;

	# Claude Code status line: stable path, store-backed content.
	# Symlinking to a fixed ~/.claude path (rather than pointing settings.json
	# straight at the store) keeps the store hash out of settings.json, so a
	# rebuild + GC can't leave the setting dangling.
	home.file.".claude/statusline.sh".source = "${claudeStatusline}/bin/claude-statusline";

	# settings.json is read-write for Claude itself (/config writes model, theme,
	# effort back to it), so it can't be a read-only nix symlink. Merge just the
	# statusLine key in and leave every other key untouched.
	home.activation.claudeStatusline = lib.hm.dag.entryAfter ["writeBoundary"] ''
		settings="$HOME/.claude/settings.json"
		mkdir -p "$HOME/.claude"
		[ -s "$settings" ] || echo '{}' > "$settings"

		tmp=$(mktemp)
		if ${pkgs.jq}/bin/jq --arg cmd "$HOME/.claude/statusline.sh" \
				'.statusLine = { type: "command", command: $cmd, padding: 0 }' \
				"$settings" > "$tmp" 2>/dev/null; then
			cat "$tmp" > "$settings"
		else
			echo "warning: $settings is not valid JSON, leaving statusLine unset" >&2
		fi
		rm -f "$tmp"
	'';

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
    jq          # was only ever in ~/.nix-profile; the Claude status line needs it
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
		freecad
    neofetch
		bagels
    # steam is NOT listed here on purpose: programs.steam (configuration.nix)
    # installs it system-wide with the libdrm fix for the FHS sandbox. A plain
    # pkgs.steam here lands in /etc/profiles/per-user, which shadows the fixed
    # one on PATH -- that is exactly what kept Steam crashing on 2026-09-03
    # after the fix was already active in the system generation.
		kdePackages.kdenlive
		nerd-fonts.jetbrains-mono
  ];
}
