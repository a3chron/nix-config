# Remote approval (A3C-167): a host-side broker that turns an unattended
# opencode permission prompt into a WhatsApp message Kurt can answer with two
# characters from his watch — and, when nobody answers, into a soft "no" that
# the agent run survives.
#
# Why it is a USER unit on the HOST and not a service in the container:
#   - it has to outlive `horus pause` and container restarts, which are exactly
#     the moments pending approvals need cleaning up;
#   - phase 5 gives it the ntfy signing key, which must live somewhere the
#     agent cannot read. ~/horus is bind-mounted into the container in full, so
#     a container-resident broker could not hold a secret from the agent. The
#     key goes to ~/.config/horus/approval.key, deliberately OUTSIDE the mount;
#   - ntfy and tailscale run on the host, and `tailscale serve` can only
#     publish a host-local listener;
#   - a user unit can raise desktop notifications, the one channel that still
#     works when WhatsApp itself is the broken thing (same argument as
#     wa-watch.nix).
#
# The broker's liveness is a HARD dependency of the whole design, not a nicety:
# opencode's Permission.ask has no timeout of any kind, so an unanswered
# permission blocks its session forever and the broker's expiry timer is the
# only thing in the system that can unblock it. Hence Restart=always,
# StartLimitIntervalSec=0 and OnFailure=horus-alert@.
#
# Secrets Kurt creates once, by hand (the agent must never generate or read
# these — the whole point of the ticket is that one of them lives where the
# agent cannot):
#
#   install -d -m 700 ~/horus/.secrets
#   openssl rand -hex 32 | sed 's/^/OPENCODE_SERVER_PASSWORD=/' \
#     > ~/horus/.secrets/opencode-server.env
#   chmod 600 ~/horus/.secrets/opencode-server.env
#
#   openssl rand -hex 32 > ~/horus/.secrets/broker-local
#   chmod 600 ~/horus/.secrets/broker-local
#
#   install -d -m 700 ~/.config/horus                      # phase 5, not yet used
#   openssl rand 32 > ~/.config/horus/approval.key
#   chmod 600 ~/.config/horus/approval.key
#
# .secrets/ is already gitignored in ~/horus. The first two legitimately live
# there: the server runs AS the agent's uid, so hiding its own password from
# the agent is impossible, and broker-local is anti-accident hygiene rather
# than a security boundary. approval.key is the one that must not be.
{ pkgs, ... }:

{
	systemd.user.services.horus-approval-broker = {
		description = "Horus remote-approval broker (:8790)";
		wantedBy = [ "default.target" ];
		# notify-send for the desktop channel; systemctl to fire horus-alert@
		path = [ pkgs.libnotify pkgs.systemd ];
		unitConfig = {
			OnFailure = [ "horus-alert@%n.service" ];
			# NixOS installs systemd.user units into /etc/systemd/user, so EVERY
			# user session starts them — including gdm-greeter's, which cannot read
			# /home/a3chron (0700) and would die on the impure ExecStart path,
			# firing OnFailure on a loop. See the long note in music.nix.
			ConditionUser = "a3chron";
			StartLimitIntervalSec = 0;
		};
		serviceConfig = {
			# Deliberately impure repo path, same as horus-music / horus-wakeup-drain:
			# tuning the broker needs `systemctl --user restart horus-approval-broker`,
			# never a rebuild.
			ExecStart = "${pkgs.python3}/bin/python /home/a3chron/nixos-config/horus/horus-approval-broker.py";
			Restart = "always";
			RestartSec = 5;
		};
	};
}
