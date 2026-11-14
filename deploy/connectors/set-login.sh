#!/bin/sh
# Save the login a connector uses, without it passing through chat or the shell history.
#   sh deploy/connectors/set-login.sh servicenow   -> instance (dev123456), user, password
#   sh deploy/connectors/set-login.sh salesforce   -> My Domain (acme.my.salesforce.com), consumer key, consumer secret
set -eu
cd "$(dirname "$0")/../.."
umask 077
mkdir -p secrets
ask() { printf '%s: ' "$1" >&2; read -r v; printf '%s' "$v"; }
hide() { printf '%s: ' "$1" >&2; stty -echo 2>/dev/null || true; read -r v; stty echo 2>/dev/null || true; printf '\n' >&2; printf '%s' "$v"; }
case "${1:-}" in
  servicenow)
    i=$(ask "Instance (like dev123456)"); u=$(ask "User"); p=$(hide "Password")
    printf 'SN_INSTANCE=%s\nSN_USER=%s\nSN_PASSWORD=%s\n' "$i" "$u" "$p" > secrets/servicenow.env ;;
  salesforce)
    d=$(ask "My Domain (like acme-dev-ed.develop.my.salesforce.com)"); k=$(ask "Consumer key"); s=$(hide "Consumer secret")
    printf 'SF_DOMAIN=%s\nSF_CLIENT_ID=%s\nSF_CLIENT_SECRET=%s\n' "$d" "$k" "$s" > secrets/salesforce.env ;;
  *) echo "usage: sh $0 servicenow|salesforce" >&2; exit 2 ;;
esac
echo "saved secrets/$1.env; check it with: .venv/bin/python -m ts connector status" >&2
