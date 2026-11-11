#!/bin/bash
# Run once with sudo on boulder. Installs the nightly backup to pve2 and its weekly restore check.
# Then put the printed public key on pve2 with pve2-setup.sh (see README.md in this folder).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run this with sudo." >&2; exit 1; }
here="$(cd "$(dirname "$0")" && pwd)"
pve2=10.9.8.123
# A root-owned copy: the timer runs as root, so it must not run a file other users can edit.
install -d -m 755 /usr/local/lib/boulder-backup
install -m 755 "$here/boulder_backup.py" /usr/local/lib/boulder-backup/boulder_backup.py
install -d -m 700 /root/.ssh /var/backups/boulder
[ -f /root/.ssh/boulder-backup ] || ssh-keygen -q -t ed25519 -N '' -C "boulder-backup@$(hostname)" -f /root/.ssh/boulder-backup
ssh-keygen -F "$pve2" -f /root/.ssh/known_hosts >/dev/null 2>&1 || ssh-keyscan -t ed25519 "$pve2" 2>/dev/null >> /root/.ssh/known_hosts
install -m 644 "$here"/boulder-backup.service "$here"/boulder-backup.timer \
    "$here"/boulder-backup-check.service "$here"/boulder-backup-check.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now boulder-backup.timer boulder-backup-check.timer
echo "Backup key for pve2 (give it to pve2-setup.sh):"
cat /root/.ssh/boulder-backup.pub
