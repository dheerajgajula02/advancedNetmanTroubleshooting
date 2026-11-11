#!/bin/bash
# Run once with sudo on boulder. Runs the health check every five minutes.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run this with sudo." >&2; exit 1; }
here="$(cd "$(dirname "$0")" && pwd)"
install -m 644 "$here"/boulder-health.service "$here"/boulder-health.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now boulder-health.timer
systemctl list-timers --no-pager boulder-health.timer
