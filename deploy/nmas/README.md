# nmas.shiplabs.net

Netman in public, behind one sign-in (`lab4_gui/access.py` in the Netman repo).

| File | Where it goes |
|---|---|
| `nmas.shiplabs.net.conf` | GCP proxy VM, `/etc/nginx/sites-available/` (symlinked into `sites-enabled`). Needs `/etc/nginx/nmas-netbox-secret.conf` (mode 600, one `proxy_set_header` line, see the file's header). |
| `netbox-public.compose.yml` | boulder: `docker compose -f deploy/nmas/netbox-public.compose.yml -p nmas up -d` |
| `netbox-nmas.py` | mounted into that container (`BASE_PATH = 'ipam/'`) |
| `netbox-env.py` | writes `~/.config/nmas/netbox-public.env` (mode 600) from `netbox-ipam`; prints names only |
| `guacamole-header.override.yml` | boulder: recreates `guacamole-web` with header login (command in the file) |

Paths: `/` is the Netman GUI (it signs people in), `/ipam/` is NetBox and `/desk/` the
service desk; nginx lets those two through only after Netman's `/auth/verify` answers
204. Accounts, the sign-in log and who is signed in are on Netman's Access page (last
in the sidebar). The LAN addresses (NetBox :8000, troubleshooting :8770, desk :8771)
are unchanged and do not go through this sign-in.

Only the public address asks for a sign-in. Netman decides "public" from the address the
request came from (the GCP proxy over NetBird or Tailscale), not from headers, so anyone
on the LAN or NetBird, including `http://10.9.8.182:8050`, gets straight in.

The same sign-in carries into the other tools:

- **NetBox**: nginx passes the account in the secret header and its group in
  `X-Nmas-Netbox-Group`. `netman-admin` is a superuser; `netman-user` may edit everything
  (ObjectPermission "netman-user: edit everything").
- **Grafana**: Netman serves it at `/grafana` and, for a signed-in account, sends
  `X-WEBAUTH-USER` / `X-WEBAUTH-ROLE` (admin -> Admin, user -> Editor). On the Grafana VM
  (10.0.0.58) `/etc/grafana/grafana.ini` has `[auth.proxy]` enabled with
  `whitelist = 10.0.0.10` (boulder) only; the file before the change is
  `grafana.ini.pre-nmas-authproxy` next to it. Without a sign-in (LAN) Grafana is seen
  through the read-only Viewer token.

More tools behind the same sign-in (added 2026-11-10):

| Path | Goes to | Login |
|---|---|---|
| `/prometheus/` | prometheus-advnetman 10.0.0.95:9090 (NetBird 10.9.8.99) | none of its own; read-only (no admin or lifecycle API) |
| `/topology-api/` | grafana-advnetman 10.0.0.58:8090 (NetBird 10.9.8.238) | none; nginx allows GET only |
| `/guacamole/` | boulder :8088 (the lab's Guacamole, web SSH to the devices) | signed in as the Netman account, see below |
| `/jenkins` | redirect to https://jenkins.dheerajgajula.com/ | Jenkins keeps its own login (the user's choice) |

- **Guacamole**: `guacamole-header.override.yml` turns on Guacamole's header login with a
  secret header name (`~/.config/nmas/guacamole-header.env` on boulder, and
  `/etc/nginx/nmas-guacamole-secret.conf` on the proxy, both 600). Without that header, on
  the LAN at :8088, Guacamole's own login works as before. Permissions come from
  Guacamole's database: groups `netman-admin` (administers everything; `dheeraj`) and
  `netman-user` (no connections until some are granted to the group; `sdn_is_better`).
  A new Netman account needs a Guacamole user of the same name added to one of the two
  groups. Running the lab1 compose file on its own recreates Guacamole without the header
  login.
