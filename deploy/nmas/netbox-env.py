#!/usr/bin/env python3
"""Write ~/.config/nmas/netbox-public.env (mode 600) for the public NetBox front end.

Database, cache and Django secret are copied from the running netbox-ipam container,
so both front ends share one NetBox. The remote-user header name is a new random
secret; nginx on the GCP proxy must send the account in that header (see
nmas.shiplabs.net.conf). Nothing is printed except names. Re-running keeps the
existing header secret.
"""

import json
import os
import secrets
import subprocess
from pathlib import Path

COPIED = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "REDIS_HOST", "REDIS_CACHE_HOST", "SECRET_KEY")
TARGET = Path.home() / ".config" / "nmas" / "netbox-public.env"

source = dict(item.split("=", 1) for item in json.loads(subprocess.check_output(
    ["docker", "inspect", "netbox-ipam", "--format", "{{json .Config.Env}}"], text=True)))
missing = [name for name in COPIED if not source.get(name)]
if missing:
    raise SystemExit(f"netbox-ipam has no {', '.join(missing)}")

old = {}
if TARGET.exists():
    old = dict(line.split("=", 1) for line in TARGET.read_text().splitlines() if "=" in line)
secret = old.get("REMOTE_AUTH_HEADER") or f"HTTP_X_NMAS_USER_{secrets.token_hex(12).upper()}"

values = {name: source[name] for name in COPIED}
values.update({
    "ALLOWED_HOSTS": "nmas.shiplabs.net",
    "CSRF_TRUSTED_ORIGINS": "https://nmas.shiplabs.net",
    "SKIP_SUPERUSER": "true",
    "REMOTE_AUTH_ENABLED": "True",
    "REMOTE_AUTH_HEADER": secret,
    "REMOTE_AUTH_AUTO_CREATE_USER": "True",
    "REMOTE_AUTH_GROUP_SYNC_ENABLED": "True",
    "REMOTE_AUTH_GROUP_HEADER": "HTTP_X_NMAS_NETBOX_GROUP",
    "REMOTE_AUTH_AUTO_CREATE_GROUPS": "True",
    "REMOTE_AUTH_SUPERUSER_GROUPS": "netman-admin",
    "REMOTE_AUTH_STAFF_GROUPS": "netman-admin",
})
TARGET.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(TARGET, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as out:
    out.write("".join(f"{name}={value}\n" for name, value in values.items()))
os.chmod(TARGET, 0o600)
print(f"wrote {TARGET}: {', '.join(values)}")
