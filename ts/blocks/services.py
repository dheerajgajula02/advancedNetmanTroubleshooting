"""Read-only checks for gateways, DHCP, NAT, device health and config changes."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone

from ts import configs, lab
from ts.blocks.checks import _clock, _log_epoch, _who, HOST
from ts.lab import Evidence, Result
from ts.registry import check
from ts.vocab import DEVICES

DHCP_SERVER = "dhcp-Server"
NAT_SOURCE = {"r3": "50.50.50.1", "r4": "50.50.50.2"}  # each border's outside address
BOOT_SCRIPT = "/etc/frr/nat.sh"  # what each border runs at start, on the router itself
_BOOT_ROUTE = re.compile(r"^\s*ip\s+-6\s+route\s+replace\s+(\S+)\s+via\s+(\S+)\s+dev\s+(\S+)", re.M)
_ROUTE_LINE = re.compile(r"(\S+)\s.*?\bvia\s+(\S+)\s+dev\s+(\S+)")
AUTOMATION_SOURCE = "172.20.20.1"
PERSON_SOURCE = "172.20.20.61"  # Guacamole
RESTART_WINDOW = 600  # a restart this close to the event is the one the event is about


# --- VRRP -----------------------------------------------------------------

def _vrrp_groups(device: str, interface: str) -> tuple[Evidence, dict | None]:
    """The IPv4 VRRP group on that interface as {state, priority, group}, or None."""
    ev, data = lab.show_json(device, "show vrrp", None, keep=1500)
    routers = [r for r in (data or {}).get("virtualRouters", []) if r.get("interface") == interface]
    chosen = next((r for r in routers if r.get("version") == 2), routers[0] if routers else None)
    if chosen is None:
        return ev, None
    info = {"state": chosen["state"], "priority": chosen["priority"], "group": chosen["groupId"]}
    ev.output, ev.note = json.dumps(info, indent=1), "Trimmed to this interface"
    return ev, info


def _known_good_priority(device: str, interface: str, group) -> int | None:
    """The VRRP priority this device should have on that interface, or None if there is no known-good copy."""
    good = configs.known_good(device)
    if good is None:
        return None
    lines = configs.sections(good[1]).get(f"interface {interface}")
    if lines is None:
        return None
    for line in lines:
        m = re.fullmatch(rf"vrrp {group} priority(?:-level)? (\d+)", line)
        if m:
            return int(m.group(1))
    return 100  # the VRRP default when the config does not set one


@check("vrrp_state", ["device", "interface"], ["normal", "failed_over", "both_master", "no_master", "not_found"],
       produces=["peer_device", "expected_master", "current_master", "vrid"], platforms=("eos",),
       description="Who holds the gateway on each of the two switches, and whether that is the one that should")
def vrrp_state(ctx, device, interface):
    peers = [d for d, (role, _p) in DEVICES.items() if role == "access" and d != device]
    ev_a, a = _vrrp_groups(device, interface)
    if a is None or not peers:
        return Result("not_found", f"{device} has no VRRP group on {interface}.", evidence=[ev_a])
    peer = peers[0]
    ev_b, b = _vrrp_groups(peer, interface)
    if b is None:
        return Result("not_found", f"{peer} has no VRRP group on {interface}.", evidence=[ev_a, ev_b])
    evidence = [ev_a, ev_b]
    masters = [d for d, info in ((device, a), (peer, b)) if info["state"] == "master"]
    want_a, want_b = _known_good_priority(device, interface, a["group"]), _known_good_priority(peer, interface, b["group"])
    if want_a is not None and want_b is not None:  # who should hold it comes from known-good, so a tampered priority can't redefine normal
        expected = device if want_a >= want_b else peer
    else:
        expected = device if a["priority"] >= b["priority"] else peer
    values = {"peer_device": peer, "expected_master": expected, "current_master": ", ".join(masters), "vrid": str(a["group"])}
    if len(masters) == 2:
        return Result("both_master", f"Both {device} and {peer} claim to be master for {interface}.", values, evidence)
    if not masters:
        return Result("no_master", f"Neither {device} nor {peer} is master for {interface}.", values, evidence)
    if masters[0] == expected:
        return Result("normal", f"{expected} is master for {interface}, which is its normal state.", values, evidence)
    return Result("failed_over", f"{masters[0]} is master for {interface}, but {expected} should normally hold it.", values, evidence)


# --- DHCP -----------------------------------------------------------------

def dhcp_reservations(host: str) -> tuple[Evidence, dict[str, str]]:
    """The addresses dnsmasq reserves for a host: {"v4": ..., "v6": ...}, whichever exist."""
    argv = ["grep", "-h", "^dhcp-host=", "/etc/dnsmasq.conf"]
    shown, out, _rc = lab.shell(DHCP_SERVER, argv)
    found: dict[str, str] = {}
    lines = []
    for line in out.splitlines():
        parts = line.split("=", 1)[-1].split(",")
        if host not in parts:
            continue
        lines.append(line)
        for part in parts:
            if part.startswith("[") and part.endswith("]"):
                found["v6"] = part.strip("[]")
            elif re.fullmatch(r"\d+\.\d+\.\d+\.\d+", part):
                found["v4"] = part
    return Evidence("device", shown, output="\n".join(lines) or f"(no reservation for {host})"), found


def host_addresses(host: str) -> tuple[Evidence, set[str] | None]:
    shown, out, rc = lab.shell(host, ["ip", "-j", "addr", "show", "eth1"])
    ev = Evidence("device", shown, output=lab._trim(out, 900))
    if rc != 0:
        return ev, None
    links = json.loads(out or "[]")
    return ev, {a["local"] for link in links for a in link.get("addr_info", [])}


def dnsmasq_running() -> tuple[Evidence, bool]:
    shown, out, rc = lab.shell(DHCP_SERVER, ["pgrep", "-x", "dnsmasq"])
    return Evidence("device", shown, output=out.strip() or "(no dnsmasq process)"), rc == 0 and bool(out.strip())


@check("dhcp_lease_state", ["device"], ["ok", "missing", "no_reservation"], produces=["missing_address"], platforms=HOST,
       description="Whether the host holds every address the DHCP server reserves for it, or the server is running if that is the device")
def dhcp_lease_state(ctx, device):
    if device == DHCP_SERVER:
        ev, running = dnsmasq_running()
        if running:
            return Result("ok", "dnsmasq is running on the DHCP server.", {"missing_address": ""}, [ev])
        return Result("missing", "dnsmasq is not running on the DHCP server.", {"missing_address": "the DHCP service itself"}, [ev])
    ev_r, reserved = dhcp_reservations(device)
    if not reserved:
        return Result("no_reservation", f"The DHCP server has no reservation for {device}.", {"missing_address": ""}, [ev_r])
    ev_a, have = host_addresses(device)
    if have is None:
        return Result("error", f"Could not read eth1 on {device}.", evidence=[ev_r, ev_a])
    lacking = [addr for addr in reserved.values() if addr not in have]
    if lacking:
        missing = ", ".join(lacking)
        return Result("missing", f"{device} should hold {missing} but does not.", {"missing_address": missing}, [ev_r, ev_a])
    return Result("ok", f"{device} holds its reserved {' and '.join(reserved.values())}.", {"missing_address": ""}, [ev_r, ev_a])


@check("dhcp_daemon_running", [], ["running", "stopped"], platforms=HOST,
       description="Whether the dnsmasq DHCP service is running on the DHCP server")
def dhcp_daemon_running(ctx):
    ev, running = dnsmasq_running()
    if running:
        return Result("running", "dnsmasq is running on the DHCP server.", evidence=[ev])
    return Result("stopped", "dnsmasq is not running on the DHCP server.", evidence=[ev])


@check("port_vlan", ["device", "interface"], ["found", "none"], produces=["vlan", "gateway_interface"], platforms=("eos",),
       description="Which VLAN the access port is in, and so which gateway interface serves it")
def port_vlan(ctx, device, interface):
    ev, data = lab.show_json(device, f"show interfaces {interface} switchport", None)
    info = (data or {}).get("switchports", {}).get(interface, {}).get("switchportInfo", {})
    vlan = info.get("accessVlanId")
    if not vlan:
        return Result("none", f"{interface} on {device} is not an access port.", evidence=[ev])
    ev.output, ev.note = json.dumps({"mode": info.get("mode"), "accessVlanId": vlan}, indent=1), "Trimmed to the VLAN"
    return Result("found", f"{interface} on {device} is in VLAN {vlan}.", {"vlan": str(vlan), "gateway_interface": f"Vlan{vlan}"}, [ev])


@check("dhcp_relay_configured", ["device", "interface"], ["present", "absent"], platforms=("eos",),
       description="Whether the gateway interface is set up to pass DHCP requests to the server")
def dhcp_relay_configured(ctx, device, interface):
    shown, out, rc = lab.eos(device, f"show running-config interfaces {interface}")
    relay = [l.strip() for l in out.splitlines() if "helper-address" in l or "dhcp relay" in l]
    ev = Evidence("device", shown, output="\n".join(relay) or lab._trim(out, 900),
                  note="Trimmed to the DHCP relay lines" if relay else "")
    if rc == 0 and relay:
        return Result("present", f"{interface} on {device} relays DHCP: {'; '.join(relay)}.", evidence=[ev])
    return Result("absent", f"{interface} on {device} has no DHCP relay configured.", evidence=[ev])


# --- border routers -------------------------------------------------------

@check("nat_rule_present", ["device"], ["present", "missing"], platforms=("frr",),
       description="Whether the border router still has its outbound NAT rule")
def nat_rule_present(ctx, device):
    shown, out, rc = lab.shell(device, ["iptables", "-t", "nat", "-S", "POSTROUTING"])
    rules = [l for l in out.splitlines() if "SNAT" in l or "MASQUERADE" in l]
    ev = Evidence("device", shown, output="\n".join(rules) or lab._trim(out, 800))
    if rc == 0 and rules:
        return Result("present", f"{device} has its outbound NAT rule.", evidence=[ev])
    return Result("missing", f"{device} has no outbound NAT rule.", evidence=[ev])


def boot_routes(device: str) -> tuple[Evidence, list[tuple[str, str, str]] | None]:
    """The IPv6 routes the border's startup script adds outside FRR, as (prefix, gateway, port).

    Default routes are left out, because FRR's own config carries those and restores them itself."""
    shown, out, rc = lab.shell(device, ["cat", BOOT_SCRIPT])
    routes = [r for r in _BOOT_ROUTE.findall(out) if r[0] != "default"] if rc == 0 else None
    listed = "\n".join(f"{prefix} via {gateway} dev {port}" for prefix, gateway, port in routes or [])
    return Evidence("device", shown, output=listed or lab._trim(out, 800)), routes


def absent_routes(device: str, wanted: list[tuple[str, str, str]]) -> tuple[Evidence, list[tuple[str, str, str]] | None]:
    """Which of the wanted routes the device lacks right now. FRR-installed copies of them count as present."""
    shown, out, rc = lab.shell(device, ["ip", "-6", "route", "show"])
    ev = Evidence("device", shown, output=lab._trim(out, 1500))
    if rc != 0:
        return ev, None
    have = {m.groups() for m in map(_ROUTE_LINE.match, out.splitlines()) if m}
    return ev, [route for route in wanted if route not in have]


def _port_up(device: str, port: str) -> bool:
    _shown, out, rc = lab.shell(device, ["ip", "-o", "link", "show", "dev", port])
    return rc == 0 and "LOWER_UP" in out


@check("border_routes_present", ["device"], ["present", "missing", "interface_down", "unreadable"], platforms=("frr",),
       description="Whether the border router still has the IPv6 return routes its startup script adds")
def border_routes_present(ctx, device):
    script, wanted = boot_routes(device)
    if not wanted:
        return Result("unreadable", f"{device}'s startup script lists no return routes to compare with.", evidence=[script])
    table, absent = absent_routes(device, wanted)
    if absent is None:
        return Result("unreadable", f"{device} did not return its routes.", evidence=[script, table])
    if not absent:
        return Result("present", f"{device} has all {len(wanted)} return routes from its startup script.", evidence=[script, table])
    names = ", ".join(prefix for prefix, _gateway, _port in absent)
    if any(_port_up(device, port) for port in {port for _prefix, _gateway, port in absent}):
        return Result("missing", f"{device} is missing return routes: {names}.", evidence=[script, table])
    return Result("interface_down", f"{device} is missing {names}, but the port they leave through is down, so they cannot be there yet.", evidence=[script, table])


# --- device health --------------------------------------------------------

def _started(device: str) -> tuple[Evidence, float | None]:
    """When the device last came up: its own record on EOS, and the age of process 1 on Linux."""
    if lab.platform(device) == "eos":
        ev, data = lab.show_json(device, "show version", None, keep=700)
        stamp = (data or {}).get("bootupTimestamp")
        return ev, float(stamp) if stamp else None
    shown, out, rc = lab.shell(device, ["stat", "-c", "%Y", "/proc/1"])
    ev = Evidence("device", shown, output=out.strip())
    return ev, float(out.strip()) if rc == 0 and out.strip().isdigit() else None


@check("recently_restarted", ["device"], ["restarted", "not_recent"], produces=["started_at"], platforms=HOST,
       description="Whether the device really restarted around this event, rather than just its SNMP agent")
def recently_restarted(ctx, device):
    ev, started = _started(device)
    if started is None:
        return Result("error", f"{device} did not report when it started.", evidence=[ev])
    when = f"{_clock(started)} on {datetime.fromtimestamp(started).strftime('%b %-d')}"
    if abs(started - ctx.event_time) <= RESTART_WINDOW or started > ctx.event_time:
        return Result("restarted", f"{device} really did restart: it came up at {when}.", {"started_at": when}, [ev])
    return Result("not_recent", f"{device} has been up since {when}, so the device did not restart.", {"started_at": when}, [ev])


@check("mgmt_reachable", ["device"], ["reachable", "unreachable"], platforms=HOST,
       description="Whether the device answers on its management address")
def mgmt_reachable(ctx, device):
    address = lab.mgmt_address(device)
    if not address:  # the expansion routers have no management address and are reached over their console
        shown, out, rc = lab.console_probe(device)
        ev = Evidence("device", shown, output=lab._trim(out, 500) if rc == 0 else "(no response)")
        if rc == 0:
            return Result("reachable", f"{device} has no management address, and it answered on its console.", evidence=[ev])
        return Result("unreachable", f"{device} has no management address, and its console did not answer.", evidence=[ev])
    argv = ["ping", "-c", "2", "-W", "2", address]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    ev = Evidence("device", " ".join(argv), output=lab._trim(proc.stdout + proc.stderr, 800))
    if proc.returncode == 0:
        return Result("reachable", f"{device} answers on its management address {address}.", evidence=[ev])
    return Result("unreachable", f"{device} does not answer on its management address {address}.", evidence=[ev])


# --- config ---------------------------------------------------------------

@check("known_good_match", ["device", "section"], ["match", "drift", "no_copy"], produces=["drift_lines"], platforms=HOST,
       description="Whether that part of the running config still matches the device's known-good copy")
def known_good_match(ctx, device, section):
    good = configs.known_good(device)
    if good is None:
        return Result("no_copy", f"There is no known-good copy of {device}'s config.", {"drift_lines": ""})
    source, good_lines = good
    shown, run_lines = configs.running(device)
    if run_lines is None:
        return Result("error", f"{device} did not return its running config.", evidence=[Evidence("device", shown)])
    if section == "managed":
        changes = configs.managed_changes(configs.without_held(device, good_lines), configs.without_held(device, run_lines))
    else:
        want, have = configs.pick(configs.sections(good_lines), section), configs.pick(configs.sections(run_lines), section)
        if want is None or have is None:
            return Result("no_copy", f"There is no part of a config called {section} to compare.", {"drift_lines": ""})
        changes = configs.diff(want, have)
    ev = Evidence("device", shown, output=configs.render(changes) or "(no differences)",
                  note=f"Compared with {source}. Lines holding secrets are left out. - known-good, + running")
    label = "config" if section == "all" else f"{section.replace('_', ' ')} config"
    if not changes:
        return Result("match", f"The {label} on {device} matches its known-good copy.", {"drift_lines": ""}, [ev])
    where = ", ".join(list(changes)[:3])
    return Result("drift", f"The {label} on {device} differs from its known-good copy in: {where}.", {"drift_lines": where}, [ev])


@check("changed_sections", ["device"], ["changed", "unchanged", "no_baseline"], produces=["section"], platforms=HOST,
       description="Which parts of the running config differ from the last time this device was looked at")
def changed_sections(ctx, device):
    shown, run_lines = configs.running(device)
    if run_lines is None:
        return Result("error", f"{device} did not return its running config.", evidence=[Evidence("device", shown)])
    old, compared = configs.baseline(device), "the last config seen on this device"
    if old is None:
        gold = configs.golden(device)
        if gold is None:
            return Result("no_baseline", f"There is nothing to compare {device}'s config with yet.", {"section": ""})
        old, compared = gold[1], f"golden snapshot {gold[0]}"
    changes = configs.diff(configs.sections(old), configs.sections(run_lines))
    ev = Evidence("device", shown, output=configs.render(changes) or "(no differences)",
                  note=f"Compared with {compared}. Lines holding secrets are left out. - before, + now")
    if not changes:
        return Result("unchanged", f"{device}'s running config is the same as {compared}.", {"section": ""}, [ev])
    where = ", ".join(list(changes)[:4])
    return Result("changed", f"{device}'s running config changed in: {where}.", {"section": where}, [ev])


@check("change_window_open", [], ["open", "closed"], platforms=HOST,
       description="Whether an automation job held the network change lock when the event arrived")
def change_window_open(ctx):
    held = ctx.event.get("change_running")
    ev = Evidence("file", f"flock entry for {lab.CHANGE_LOCK} in /proc/locks when the event arrived",
                  output={True: "held", False: "not held", None: "not recorded for this event"}[held])
    if held:
        return Result("open", "An automation job held the network change lock when this arrived.", evidence=[ev])
    return Result("closed", "No automation job held the network change lock when this arrived.", evidence=[ev])


_CONFIG_LOG = re.compile(r"%SYS-5-CONFIG_(?:STARTUP|I): .*? (?:from \S+ )?by (.+?)\.?$|%SYS-5-CONFIG_I: Configured from (.+)$")


@check("config_author", ["device"], ["automation", "person", "unknown"], produces=["changed_by"], platforms=("eos",),
       description="Who the device logged as making the change, and whether that was automation or a person")
def config_author(ctx, device):
    shown, out, rc = lab.eos(device, "show logging")
    year = datetime.fromtimestamp(ctx.event_time, timezone.utc).year
    hits = []
    for line in out.splitlines() if rc == 0 else []:
        when = _log_epoch(line, year)
        if when is not None and ctx.event_time - 900 <= when <= ctx.event_time + 60 and "%SYS-5-CONFIG_" in line and "CONFIG_E" not in line:
            hits.append(line)
    ev = Evidence("device", shown, output="\n".join(hits[-12:]) or "(no config lines in the 15 minutes before this event)",
                  note="Filtered to config lines from 15 minutes before the event")
    if not hits:
        return Result("unknown", f"{device}'s log does not say who changed it.", {"changed_by": "someone unknown"}, [ev])
    m = _CONFIG_LOG.search(hits[-1])
    raw = (m.group(1) or m.group(2)) if m else hits[-1]
    who = _who(raw.strip())
    if AUTOMATION_SOURCE in raw:
        return Result("automation", f"The change came from the automation server: {who}.", {"changed_by": who}, [ev])
    if PERSON_SOURCE in raw:
        return Result("person", f"A person made the change through Guacamole: {who}.", {"changed_by": who}, [ev])
    return Result("unknown", f"The change came from {who}, which is not a known source.", {"changed_by": who}, [ev])
