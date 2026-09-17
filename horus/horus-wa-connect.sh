# `horus wa-connect`: re-pair the WhatsApp bridge in one go.
#
# WA revokes unofficial (Baileys) linked devices from time to time — that is
# inherent, not a bug (see Horus.md "Known limits"). horus-wa-watch makes the
# revocation loud; this script is the fix it points at. It replaces the manual
# dance (rm -rf wa-auth, restart, dig the QR out of bridge.log, repeat when the
# QR rotates) with:
#
#   1. make sure the container is up
#   2. delete ~/horus/wa-auth and kill the bridge process (systemd restarts it
#      5s later with no creds -> it asks for a QR). Only the bridge is killed,
#      not the container: NOPASSWD covers `machinectl shell horus@horus *`, and
#      the model / opencode server / a running agent stay untouched.
#   3. follow bridge.log and render every QR block as it appears — Baileys
#      rotates the QR every ~20-60s and after a few unscanned ones closes with
#      a 408, which the bridge answers with a reconnect and fresh QRs, so the
#      screen is redrawn on each new one until "WhatsApp connected." shows up.
#
# Log-driven on purpose: the QR only exists in bridge.log (the bridge prints it
# with qrcode-terminal), and the same stream tells us about success/failure, so
# there's nothing to poll. Runs impure from the repo path (like `horus log`) so
# tweaks need no rebuild. Needs no password.
#
# Flags: --force             re-pair even though the bridge reports "connected"
#        --timeout SECONDS   give up after this long (default 600)

set -euo pipefail

HORUS_DIR=/home/a3chron/horus
AUTH_DIR="$HORUS_DIR/wa-auth"
LOG="$HORUS_DIR/bridge/bridge.log"
STATUS_URL=http://127.0.0.1:8765/status
# [n]ode: the `bash -c` wrapper's own cmdline contains this pattern and would
# otherwise be pkill's first victim; the bracket keeps the regex from matching
# its own literal text
BRIDGE_CMD='[n]ode /home/horus/work/bridge/server.js'

force=0
timeout_s=600
while [ $# -gt 0 ]; do
	case "$1" in
		--force|-f) force=1 ;;
		--timeout) timeout_s="${2:?--timeout needs seconds}"; shift ;;
		-h|--help)
			echo "usage: horus wa-connect [--force] [--timeout SECONDS]"
			exit 0 ;;
		*) echo "horus wa-connect: unknown flag '$1'" >&2; exit 1 ;;
	esac
	shift
done

say() { printf 'wa-connect: %s\n' "$*"; }
die() { printf 'wa-connect: %s\n' "$*" >&2; exit 1; }

bridge_status() {
	curl -sf --max-time 2 "$STATUS_URL" 2>/dev/null | jq -r '.status // "?"' 2>/dev/null || echo unreachable
}

# machinectl swallows the inner exit code (same as `horus cancel`), so every
# in-container command echoes a marker and we key off stdout.
in_container() {
	sudo machinectl shell horus@horus /run/current-system/sw/bin/bash -c "$1" 2>/dev/null | tr -d '\r'
}

# --- 1. container up -------------------------------------------------------
if ! systemctl is-active -q container@horus.service; then
	say "container is down — starting it"
	sudo systemctl start container@horus.service
	for _ in $(seq 1 60); do
		[ "$(bridge_status)" != unreachable ] && break
		sleep 1
	done
fi

st=$(bridge_status)
case "$st" in
	connected)
		if [ "$force" -ne 1 ]; then
			say "the bridge reports 'connected' — nothing to re-pair."
			say "if WhatsApp still shows Horus as unlinked, run: horus wa-connect --force"
			exit 0
		fi
		say "bridge is connected but --force given — re-pairing anyway"
		;;
	unreachable)
		# container up but no bridge yet (just started / restarting) — still
		# fine to proceed: systemd brings it back, and we watch the log anyway
		say "bridge not answering on $STATUS_URL yet — proceeding, systemd will (re)start it"
		;;
	*)
		say "bridge status: $st"
		;;
esac

# --- 2. wipe creds, restart the bridge ---------------------------------------
# Start following the log BEFORE the kill so nothing printed by the fresh
# process can slip past us. `tail -F` follows by name, which survives the
# ExecStartPre logrotate (mv + recreate) the bridge does on every start.
touch "$LOG" 2>/dev/null || true
exec 3< <(tail -n 0 -F "$LOG" 2>/dev/null)
tail_pid=$!
cleanup() {
	kill "$tail_pid" 2>/dev/null || true
	printf '\033[?25h' # cursor back on
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

# rm first, kill second: a logged-out bridge holds no socket, so nothing can
# rewrite creds in between; the restarted process then finds an empty dir and
# asks for a QR. (The opposite order races the 5s RestartSec.)
if [ -d "$AUTH_DIR" ]; then
	rm -rf "$AUTH_DIR"
	say "removed $AUTH_DIR"
fi

out=$(in_container "if pkill -f '$BRIDGE_CMD'; then echo KILLED; else echo NONE; fi" || true)
case "$out" in
	*KILLED*) say "bridge process killed — systemd restarts it in ~5s" ;;
	*NONE*)   say "no bridge process running — waiting for systemd to start it" ;;
	*)        say "could not reach the container shell (sudo/machinectl problem?) — watching the log anyway" ;;
esac

# --- 3. render QRs from the log until connected --------------------------------
started=$(date +%s)
qr_n=0
collecting=0
qr=""
render() {
	printf '\033[H\033[2J\033[?25l' # clear, hide cursor
	printf "Horus WhatsApp — scan with the agent's phone: WhatsApp › Linked devices › Link a device\n"
	printf 'QR #%d, %s  (rotates every ~20-60s; screen refreshes on its own — Ctrl-C aborts)\n\n' "$qr_n" "$(date +%H:%M:%S)"
	printf '%s\n' "$qr"
}

say "waiting for the bridge to print a QR…"
while :; do
	now=$(date +%s)
	if [ $((now - started)) -ge "$timeout_s" ]; then
		printf '\n'
		die "no connection after ${timeout_s}s — bridge status now: $(bridge_status). Try again, or check: tail ~/horus/bridge/bridge.log"
	fi
	# read with a timeout so the deadline fires even when the log is silent
	IFS= read -r -t 5 -u 3 line && rc=0 || rc=$?
	if [ "$rc" -ne 0 ]; then
		# >128 = timeout (log silent, loop to re-check the deadline); anything
		# else means the stream closed under us
		[ "$rc" -gt 128 ] && continue
		die "log stream ended unexpectedly (tail died?)"
	fi

	if [ "$collecting" -eq 1 ]; then
		case "$line" in
			"") # a blank line closes the block — but the marker is followed by a
				# blank line too, so only close once we actually have QR rows
				if [ -n "$qr" ]; then
					collecting=0
					qr_n=$((qr_n + 1))
					render
				fi ;;
			*) qr="${qr}${qr:+$'\n'}${line}" ;;
		esac
		continue
	fi

	case "$line" in
		*"Scan this QR with WhatsApp"*)
			collecting=1
			qr=""
			;;
		*"WhatsApp connected."*)
			printf '\033[?25h\n'
			say "connected ✓  (bridge status: $(bridge_status))"
			say "WhatsApp lists Horus as an 'Ubuntu / Chrome' linked device."
			exit 0
			;;
		*"Logged out"*|*"401: Connection Failure"*)
			# the fresh process got a 401 -> creds were re-written before the kill
			# landed, or the phone rejected the link. A re-run is cheap.
			printf '\n'
			say "bridge got logged out again after the restart — see: tail ~/horus/bridge/bridge.log"
			die "re-run 'horus wa-connect' (if it keeps happening: rm -rf ~/horus/wa-auth && sudo systemctl restart container@horus.service)"
			;;
	esac
done
