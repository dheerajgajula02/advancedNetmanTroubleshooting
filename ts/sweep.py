"""A regular read-only look at the things that send no trap.

r3 and r4 send no routing traps, RIP has none at all, and nothing in the lab
reports a missing route, a lost DHCP address or a broken internet path. So the
watcher looks for itself. An event fires only when something that was fine is
no longer fine, so a problem that lasts opens one investigation, not one a minute.
While a job holds the network change lock the sweep sits out, because its
changes are expected to break things for a moment.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ts import configs, lab
from ts.blocks import routing, services
from ts.intake import event_for
from ts.sources import jenkins
from ts.vocab import FORBIDDEN_DEVICES

STATE_FILE = Path(os.environ.get("TS_SWEEP_STATE", Path(__file__).resolve().parent.parent / "data" / "sweep-state.json"))
BORDERS = ("r3", "r4")
RIP_SPEAKERS = ("r1", "r2", "s1", "s2", "r6", "r7")
ROUTERS = ("r1", "r2", "r3", "r4", "s1", "s2", "s3", "s4")
HOSTS = ("h1", "h2", "h3", "h4")
CONFIG_DEVICES = ("r1", "r2", "r3", "r4", "r6", "r7", "r8", "s1", "s2", "s3", "s4", "s5")
WATCHED_DEVICES = (*ROUTERS, "r6", "r7", "r8", "s5", services.DHCP_SERVER, *HOSTS)
KEY_PREFIXES = ("0.0.0.0/0", "10.10.10.0/24", "10.20.20.0/24", "10.0.99.0/31")
INTERNET_V4, INTERNET_V6 = "8.8.8.8", "2001:db8:8::8"
WORKERS = 6
DRIFT_EVERY = 1  # heartbeats between config comparisons; a heartbeat is rare enough to read every config each time


@dataclass
class Finding:
    key: str
    ok: bool
    event: dict | None = None  # what to investigate if this goes bad; None reuses the one from when it was fine


def _bgp(device: str) -> tuple[str, list[Finding]]:
    _ev, peers = routing.bgp_peers(device)
    if peers is None:
        raise RuntimeError(f"{device} did not return its BGP summary")
    return f"bgp:{device}:", [Finding(
        f"bgp:{device}:{peer}", state == "Established",
        event_for("bgp.peer_down", device, f"BGP {peer}", f"{device} lost BGP to {peer}",
                  f"The sweep saw BGP to {peer} leave Established on {device}",
                  f"BGP from {device} to {peer} was {state} when the sweep looked.", peer_ip=peer),
    ) for peer, state in peers.items()]


def _rip(device: str) -> tuple[str, list[Finding]]:
    _ev, heard = routing.rip_neighbors(device)
    if heard is None:
        raise RuntimeError(f"{device} did not return its RIP neighbors")
    return f"rip:{device}:", [Finding(
        f"rip:{device}:{ip}", age <= routing.RIP_STALE_AFTER,
        event_for("rip.neighbor_lost", device, f"RIP {ip}", f"{device} stopped hearing RIP from {ip}",
                  f"The sweep saw RIP from {ip} go quiet on {device}",
                  f"{device} last heard RIP from {ip} too long ago, or no longer lists it.", neighbor_ip=ip),
    ) for ip, age in heard.items()]


def _routes(device: str) -> tuple[str, list[Finding]]:
    _ev, table = routing.route_table(device)
    if table is None:
        raise RuntimeError(f"{device} did not return its routing table")
    findings = []
    for prefix in KEY_PREFIXES:
        route = table.get(prefix)
        event = None
        if route:
            event = event_for("route.missing", device, f"route {prefix}", f"{device} lost the route to {prefix}",
                              f"The sweep saw {prefix} leave the routing table on {device}",
                              f"{prefix} was last seen on {device} from {route['protocol']} via {route['via']}.",
                              prefix=prefix, expected_protocol=route["protocol"])
        findings.append(Finding(f"route:{device}:{prefix}", route is not None, event))
    return f"route:{device}:", findings


def _internet(host: str) -> tuple[str, list[Finding]]:
    _ev, reserved = services.dhcp_reservations(host)
    target, default = (INTERNET_V4, "0.0.0.0/0") if "v4" in reserved else (INTERNET_V6, "::/0")
    _ev, ok = routing.can_ping(host, target)
    return f"internet:{host}", [Finding(
        f"internet:{host}", ok,
        event_for("internet.unreachable", host, "internet", f"{host} cannot reach the internet",
                  f"The sweep's ping from {host} to {target} failed",
                  f"{host} got no reply from {target}.", internet_target=target, default_prefix=default),
    )]


def _dhcp(host: str) -> tuple[str, list[Finding]]:
    _ev, reserved = services.dhcp_reservations(host)
    _ev, have = services.host_addresses(host)
    if have is None:
        raise RuntimeError(f"could not read eth1 on {host}")
    ok = bool(reserved) and all(addr in have for addr in reserved.values())
    return f"dhcp:{host}", [Finding(
        f"dhcp:{host}", ok,
        event_for("dhcp.failure", host, "DHCP", f"{host} lost its DHCP address",
                  f"The sweep saw {host} without an address the DHCP server reserves for it",
                  f"{host} should hold {' and '.join(reserved.values()) or 'a reserved address'}.", interface="eth1"),
    )]


def _dhcp_server() -> tuple[str, list[Finding]]:
    _ev, running = services.dnsmasq_running()
    return "dhcp:server", [Finding(
        "dhcp:server", running,
        event_for("dhcp.failure", services.DHCP_SERVER, "DHCP service", "The DHCP service stopped",
                  "The sweep saw dnsmasq stop on the DHCP server", "dnsmasq was not running when the sweep looked.",
                  interface="eth1"),
    )]


def _devices() -> tuple[str, list[Finding]]:
    rc, out = lab.docker(["ps", "--format", "{{.Names}}"])
    if rc != 0:
        raise RuntimeError("docker ps failed")
    running = {name.removeprefix(lab.CONTAINER_PREFIX) for name in out.split()}
    return "device:", [Finding(
        f"device:{device}", device in running,
        event_for("device.unreachable", device, "unreachable", f"{device} stopped answering",
                  f"The sweep saw {device} stop answering", f"{device} did not answer when the sweep looked."),
    ) for device in WATCHED_DEVICES]


def _links(device: str) -> tuple[str, list[Finding]]:
    """Ping across each routed link. Only one end probes a link. A neighbor that vanishes is left to the traps,
    except on the border routers, which send none."""
    _ev, neighbors = routing.ospf_neighbors(device)
    if neighbors is None:
        raise RuntimeError(f"{device} did not return its OSPF neighbors")
    findings = []
    for n in neighbors:
        port, address = n["interface"], n["address"]
        peer = lab.topology_peer(device, port)
        if n["state"] != "full" or address in routing.ISP_ADDRESSES or (peer and peer[0] in FORBIDDEN_DEVICES):
            continue
        if peer and (peer[0], peer[1]) < (device, port):
            continue
        _ev, lost = routing.ping_loss(device, address)
        if lost is None:
            continue
        findings.append(Finding(f"link:{device}:{port}", lost < routing.LOSSY_FROM, event_for(
            "link.degraded", device, f"link {port}", f"The link on {port} of {device} is dropping packets",
            f"The sweep's pings from {device} to {address} lost {lost:g}%",
            f"{lost:g}% of 10 pings from {device} to its neighbor at {address} were lost.",
            interface=port, neighbor_ip=address, merge_key=f"link:{device}:{port}")))
    return f"link:{device}:" if lab.platform(device) == "frr" else f"probed:{device}", findings


def _nat(device: str) -> tuple[str, list[Finding]]:
    res = services.nat_rule_present(None, device)
    if res.outcome not in ("present", "missing"):
        raise RuntimeError(f"could not read {device}'s NAT table")
    return f"nat:{device}", [Finding(
        f"nat:{device}", res.outcome == "present",
        event_for("nat.missing", device, "NAT", f"{device} lost its NAT rule", f"The sweep saw the NAT rule leave {device}",
                  f"{device}'s outbound NAT rule was not in its NAT table when the sweep looked."),
    )]


def _return_routes(device: str) -> tuple[str, list[Finding]]:
    res = services.border_routes_present(None, device)
    if res.outcome == "unreadable":
        raise RuntimeError(f"could not compare {device}'s return routes")
    return f"return-routes:{device}", [Finding(
        f"return-routes:{device}", res.outcome != "missing",  # a down port is its own fault, and these can only return after it
        event_for("routes.missing", device, "return routes", f"{device} lost its IPv6 return routes", f"The sweep saw the return routes leave {device}",
                  f"Routes from {device}'s startup script were not in its routing table when the sweep looked."),
    )]


def _drift(device: str) -> tuple[str, list[Finding]]:
    good = configs.known_good(device)
    _shown, now = configs.running(device)
    if now is None:
        raise RuntimeError(f"{device} did not return its config")
    if good is None:
        return f"drift:{device}", []
    changes = configs.managed_changes(configs.without_held(device, good[1]), configs.without_held(device, now))
    where = ", ".join(list(changes)[:3])
    return f"drift:{device}", [Finding(
        f"drift:{device}", not changes,
        event_for("config.changed", device, "config", f"{device}'s config no longer matches its known-good copy",
                  f"The sweep compared {device} with its known-good copy and found a difference",
                  f"It differs in: {where}.", merge_key=f"config:{device}"),
    )]


def _probes(with_drift: bool = False) -> list:
    return [*((_bgp, d) for d in BORDERS), *((_nat, d) for d in BORDERS), *((_return_routes, d) for d in BORDERS), *((_rip, d) for d in RIP_SPEAKERS), *((_routes, d) for d in ROUTERS),
            *((_links, d) for d in ROUTERS), *((_internet, h) for h in HOSTS), *((_dhcp, h) for h in HOSTS), (_dhcp_server,), (_devices,),
            *((_drift, d) for d in CONFIG_DEVICES if with_drift)]


def _gather(log, with_drift: bool = False) -> list[tuple[str, list[Finding]]]:
    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [(probe, pool.submit(*probe)) for probe in _probes(with_drift)]
        for probe, future in futures:
            try:
                results.append(future.result())
            except Exception as exc:  # no answer is not the same as bad news; judge it next time
                log(f"sweep: {probe[0].__name__} {' '.join(probe[1:])} gave no answer: {type(exc).__name__}: {exc}")
    return results


def look(log=print) -> list[Finding]:
    return [finding for _scope, findings in _gather(log, with_drift=True) for finding in findings]


def step(state: dict, gathered: list[tuple[str, list[Finding]]], now: float) -> list[dict]:
    """Compare what was found with what was known, update what is known, and return the new events."""
    events = []
    for scope, findings in gathered:
        seen = {f.key for f in findings}
        gone = [Finding(key, False) for key, known in state.items() if key.startswith(scope) and key not in seen and known["ok"]]
        for finding in [*findings, *gone]:
            known = state.get(finding.key)
            template = finding.event or (known or {}).get("event")
            if known and known["ok"] and not finding.ok and template:
                events.append({**template, "time": now, "trap_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))})
            state[finding.key] = {"ok": finding.ok, "event": template, "first_seen": (known or {}).get("first_seen") or now, "since": now if not known or known["ok"] != finding.ok else known.get("since"),
                                  "last_ok": now if finding.ok else (known or {}).get("last_ok")}  # so "was it working before?" has an answer
    return events


def run(state: dict, log=print) -> list[dict]:
    if lab.change_running():
        return []
    tick = state.get("_tick", {"n": 0})["n"] + 1
    state["_tick"] = {"n": tick, "at": time.time()}
    gathered = _gather(log, with_drift=tick % DRIFT_EVERY == 0)
    if lab.change_running():  # it started while we were looking, so what we saw may be its doing
        return []
    events = step(state, gathered, time.time())
    for event in events:
        event["change_running"] = False
    healthy = all(f.ok for _scope, findings in gathered for f in findings)
    _refresh_copies_after_deploy(state, healthy, log)
    return events


def _refresh_copies_after_deploy(state: dict, healthy: bool, log) -> None:
    """A deployment changes what known-good means. Devices with a golden snapshot get a new one from the deployment;
    the few that rely on a saved copy take their config now, but only once the network is healthy."""
    deploys = jenkins.recent_deploys()
    done = set(state.get("_deploys", {}).get("seen", []))
    if not healthy or not deploys or set(deploys) <= done:
        return
    for device in CONFIG_DEVICES:
        if configs.has_known_good(device):
            _shown, lines = configs.running(device)
            if lines is not None:
                configs.save_known_good(device, lines)
                configs.save_baseline(device, lines)
    log(f"sweep: refreshed the saved known-good copies after {', '.join(deploys[:3])}")
    state["_deploys"] = {"seen": deploys}


def load_state(path: Path = STATE_FILE) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def remember_configs(log=print) -> None:
    """Save what each device's next change gets compared with, and a known-good copy only where the golden can't serve."""
    for device in CONFIG_DEVICES:
        gold, have_copy = configs.golden(device), configs.has_known_good(device)
        if configs.baseline(device) is not None and (have_copy or gold is not None):
            continue
        try:
            _shown, lines = configs.running(device)
        except (subprocess.SubprocessError, OSError) as exc:
            log(f"sweep: could not read {device}'s config: {exc}")
            continue
        if lines is None:
            continue
        if configs.baseline(device) is None:
            configs.save_baseline(device, lines)
        if not have_copy and gold is None:
            configs.save_known_good(device, lines)
