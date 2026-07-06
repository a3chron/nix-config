# SearXNG meta-search on localhost:8888 — the agent's web-search backend.
# (25.11 module name is still `services.searx`; it runs the searxng package.)
# JSON format enabled so the search tool/MCP can consume results.
{ config, pkgs, lib, ... }:

{
	services.searx = {
		enable = true;
		# SEARXNG_SECRET lives outside git (this repo is public on GitHub);
		# regenerate with: echo "SEARXNG_SECRET=$(openssl rand -hex 32)" > ~/horus/.secrets/searxng.env
		environmentFile = "/home/a3chron/horus/.secrets/searxng.env";
		settings = {
			server = {
				bind_address = "127.0.0.1";
				port = 8888;
				# localhost-only instance; key only guards CSRF-style tokens
				secret_key = "$SEARXNG_SECRET";
			};
			search.formats = [ "html" "json" ];
		};
	};
}
