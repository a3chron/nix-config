# SearXNG meta-search on localhost:8888 — the agent's web-search backend.
# (25.11 module name is still `services.searx`; it runs the searxng package.)
# JSON format enabled so the search tool/MCP can consume results.
{ config, pkgs, lib, ... }:

{
	services.searx = {
		enable = true;
		settings = {
			server = {
				bind_address = "127.0.0.1";
				port = 8888;
				# localhost-only instance; key only guards CSRF-style tokens
				secret_key = "horus-local-searxng-4f8a2b1c9d3e5f7a";
			};
			search.formats = [ "html" "json" ];
		};
	};
}
