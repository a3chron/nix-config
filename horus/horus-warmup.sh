#!/usr/bin/env bash
# Proactively warm the LLM so Kurt's first request doesn't pay the ~40-50s cold
# penalty (model load + re-processing the ~10k-token system/AGENTS/tool-schema
# prefix from an empty KV cache). A trivial opencode run loads the model AND
# primes that prefix in llama-server's cache, so the next real request
# prefix-matches and skips the cold prefill.
#
# Gated: never warm while a GPU-heavy app is running, and never force-resume a
# stack Kurt deliberately paused. Runs the warmup in the FOREGROUND — the caller
# decides whether to background it (the boot oneshot runs it inline; the
# headphone watcher spawns it detached). Needs curl + jq on PATH.
set -uo pipefail

# 1. GPU-heavy app running? leave the GPU to it. (If War Thunder's process/path
#    isn't caught here, add its exact name — matched case-insensitively against
#    the full command line.)
if pgrep -fi 'warthunder|minecraft|kdenlive|bambu|blender' >/dev/null 2>&1; then
	echo "horus-warmup: GPU-heavy app running — skipping"
	exit 0
fi

# 2. llama-swap reachable? if not, the stack is paused/down — don't resume it.
running=$(curl -sf --max-time 3 http://127.0.0.1:8080/running 2>/dev/null) || {
	echo "horus-warmup: llama-swap unreachable (paused?) — skipping"
	exit 0
}

# 3. model already loaded (or loading)? then it's warm — and priming now would
#    evict an active session's cache. Nothing to do. (Same shape cli.nix reads:
#    .running is the list of live models.)
if echo "$running" | jq -e '.running | length > 0' >/dev/null 2>&1; then
	echo "horus-warmup: model already loaded — nothing to do"
	exit 0
fi

# 4. container up? opencode runs inside it. (absolute path: at boot this runs as
#    a system service with a minimal PATH.)
if ! /run/current-system/sw/bin/systemctl is-active -q container@horus.service; then
	echo "horus-warmup: container not running — skipping"
	exit 0
fi

# 5. prime it. Trivial prompt (fresh session) so the model does nothing but
#    ingest the shared prefix and reply.
echo "horus-warmup: priming model + prompt cache…"
mc=/run/current-system/sw/bin/machinectl
if [ "$(id -u)" -eq 0 ]; then
	pre=""
else
	pre="/run/wrappers/bin/sudo -n "
fi
# shellcheck disable=SC2086
$pre "$mc" shell horus@horus /run/current-system/sw/bin/bash -c \
	"cd /home/horus/work && timeout 180 opencode run 'warmup — reply with just: ok'" \
	>/dev/null 2>&1
echo "horus-warmup: done"
