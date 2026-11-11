#!/bin/bash
# Run once with sudo. Swaps the hand-started watcher and page for services that come back after a reboot.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run this with sudo." >&2; exit 1; }
here="$(cd "$(dirname "$0")" && pwd)"
install -m 644 "$here"/netman-troubleshoot-watch.service "$here"/netman-troubleshoot-ui.service "$here"/netman-troubleshoot-desk.service /etc/systemd/system/
systemctl daemon-reload
pkill -u dheeraj -f -- '-m ts (watch|serve|desk)' || true
sleep 2
systemctl enable --now netman-troubleshoot-watch netman-troubleshoot-ui netman-troubleshoot-desk
systemctl --no-pager --lines=0 status netman-troubleshoot-watch netman-troubleshoot-ui netman-troubleshoot-desk
