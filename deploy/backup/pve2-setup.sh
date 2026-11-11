#!/bin/bash
# Run on pve2 as root, with boulder's backup public key as the only argument.
# Makes the boulderbak account, which can only rsync into /srv/boulder-backup/data, and a daily
# prune that keeps 30 days of copies and never fewer than the 7 newest.
set -euo pipefail
key="$1"
case "$key" in ssh-ed25519\ *) ;; *) echo "Give the ssh-ed25519 public key." >&2; exit 1;; esac
id boulderbak >/dev/null 2>&1 || useradd --system --home-dir /srv/boulder-backup --create-home --shell /bin/sh boulderbak
install -d -o boulderbak -g boulderbak -m 700 /srv/boulder-backup /srv/boulder-backup/data /srv/boulder-backup/.ssh
printf 'restrict,command="/usr/bin/rrsync /srv/boulder-backup/data" %s\n' "$key" > /srv/boulder-backup/.ssh/authorized_keys
chown boulderbak:boulderbak /srv/boulder-backup/.ssh/authorized_keys
chmod 600 /srv/boulder-backup/.ssh/authorized_keys
cat > /usr/local/sbin/boulder-backup-prune <<'PRUNE'
#!/bin/sh
# Keep 30 days of boulder's backups, and never fewer than the 7 newest.
cd /srv/boulder-backup/data || exit 0
cutoff=$(date -d '30 days ago' +%F)
ls -1d 20[0-9][0-9]-[0-9][0-9]-[0-9][0-9] 2>/dev/null | sort | head -n -7 | while read -r d; do
    [ "$d" \< "$cutoff" ] && rm -rf -- "$d"
done
exit 0
PRUNE
chmod 755 /usr/local/sbin/boulder-backup-prune
echo '30 4 * * * root /usr/local/sbin/boulder-backup-prune' > /etc/cron.d/boulder-backup-prune
echo "pve2 is ready for boulder's backups."
