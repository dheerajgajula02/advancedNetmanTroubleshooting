"""Read-only checks for routing: OSPF, RIP, BGP, routes and reachability."""

from __future__ import annotations

import ipaddress
import json
import re

from ts import intake, lab
from ts.lab import Evidence, Result
from ts.registry import ANY_PLATFORM, check

HOST = (ANY_PLATFORM,)
ROUTING = ("eos", "frr")
ALL_DEVICES = ("eos", "frr", "linux")
RIP_STALE_AFTER = 120  # seconds; RIP speaks every 30 s
# R5's side of the two uplinks. It is the ISP's router, so nothing here pings it directly.
ISP_ADDRESSES = frozenset({"20.20.20.1", "20.20.20.3", "2001:db8:20::1", "2001:db8:20::3"})


# --- OSPF -----------------------------------------------------------------

def ospf_neighbors(device: str) -> tuple[Evidence, list[dict] | None]:
    """Every OSPF neighbor as {router_id, state, interface, address}."""
    ev, data = lab.show_json(device, "show ip ospf neighbor", "show ip ospf neighbor json")
    if data is None:
        return ev, None
    out = []
    if lab.platform(device) == "eos":
        for instance in data.get("vrfs", {}).get("default", {}).get("instList", {}).values():
            for n in instance.get("ospfNeighborEntries", []):
                out.append({"router_id": n["routerId"], "state": n["adjacencyState"].lower(),
                            "interface": n["interfaceName"], "address": n["interfaceAddress"]})
    else:
        for router_id, entries in data.get("neighbors", {}).items():
            for n in entries:
                out.append({"router_id": router_id, "state": n["nbrState"].split("/")[0].lower(),
                            "interface": n["ifaceName"].split(":")[0], "address": n["ifaceAddress"]})
    return ev, out


@check("ospf_neighbor_state", ["device", "neighbor_id"], ["full", "forming", "missing"], produces=["ospf_state"],
       platforms=ROUTING, description="Whether the OSPF neighbor is fully up on this device right now")
def ospf_neighbor_state(ctx, device, neighbor_id):
    ev, neighbors = ospf_neighbors(device)
    if neighbors is None:
        return Result("error", f"{device} did not return its OSPF neighbors.", evidence=[ev])
    match = [n for n in neighbors if n["router_id"] == neighbor_id]
    ev.output = json.dumps(match or neighbors, indent=1)
    ev.note = "Trimmed to this neighbor" if match else "This neighbor is not listed, so all of them are shown"
    if not match:
        return Result("missing", f"{device} no longer lists {neighbor_id} as an OSPF neighbor.",
                      {"ospf_state": "missing"}, [ev])
    state = match[0]["state"]
    if state == "full":
        return Result("full", f"OSPF to {neighbor_id} is full on {match[0]['interface']}.", {"ospf_state": state}, [ev])
    return Result("forming", f"OSPF to {neighbor_id} is stuck in {state}, not full.", {"ospf_state": state}, [ev])


def _ospf_interface(device: str, interface: str) -> tuple[Evidence, dict | None]:
    """{area, hello, dead, network, passive} for one port, or None when OSPF is not on it."""
    if lab.platform(device) == "eos":
        ev, data = lab.show_json(device, f"show ip ospf interface {interface}", None)
        info = None
        for instance in (data or {}).get("vrfs", {}).get("default", {}).get("instList", {}).values():
            info = instance.get("interfaces", {}).get(interface) or info
        if info is None:
            return ev, None
        return ev, {"area": info["area"], "hello": info["helloInterval"], "dead": info["deadInterval"],
                    "network": "p2p" if info.get("interfaceType") == "p2p" else info.get("interfaceType"),
                    "passive": bool(info.get("passive"))}
    port = lab.linux_name(interface)
    ev, data = lab.show_json(device, None, f"show ip ospf interface {port} json")
    info = (data or {}).get("interfaces", {}).get(port)
    if not info or not info.get("ospfEnabled", True) or "area" not in info:
        return ev, None
    network = str(info.get("networkType", "")).lower()
    area = str(info["area"]).split(" ")[0]
    return ev, {"area": area, "hello": int(info.get("timerHelloInMsecs", info.get("timerMsecs", 0))) // 1000,
                "dead": int(info.get("timerDeadSecs", 0)),
                "network": "p2p" if "point" in network else network, "passive": bool(info.get("timerPassiveIface"))}


@check("ospf_interface", ["device", "interface"], ["enabled", "passive", "not_enabled"], platforms=ROUTING,
       description="Whether OSPF is running on the port and allowed to form neighbors there")
def ospf_interface(ctx, device, interface):
    ev, info = _ospf_interface(device, interface)
    if info is None:
        return Result("not_enabled", f"OSPF is not running on {interface} on {device}.", evidence=[ev])
    if info["passive"]:
        return Result("passive", f"{interface} on {device} is passive, so OSPF will not form a neighbor there.", evidence=[ev])
    return Result("enabled", f"OSPF is running on {interface} on {device}, in area {info['area']}.", evidence=[ev])


@check("ospf_params_match", ["device", "interface", "peer_device", "peer_interface"], ["match", "mismatch"],
       produces=["param", "mismatch"], platforms=ROUTING,
       description="Whether both ends agree on the OSPF settings that must be equal: area, timers and network type")
def ospf_params_match(ctx, device, interface, peer_device, peer_interface):
    ev_a, a = _ospf_interface(device, interface)
    ev_b, b = _ospf_interface(peer_device, peer_interface)
    if a is None or b is None:
        return Result("error", "One of the two ends has no OSPF settings to compare.", evidence=[ev_a, ev_b])
    names = {"area": "area", "hello": "hello interval", "dead": "dead interval", "network": "network type"}
    ev_a.output, ev_b.output = json.dumps(a, indent=1), json.dumps(b, indent=1)
    for key, name in names.items():
        if a[key] != b[key]:
            detail = f"{name} is {a[key]} on {device} and {b[key]} on {peer_device}"
            return Result("mismatch", f"The two ends disagree: {detail}.", {"param": key, "mismatch": detail}, [ev_a, ev_b])
    return Result("match", f"Both ends agree on area {a['area']}, hello {a['hello']} s, dead {a['dead']} s "
                           f"and network type {a['network']}.", {"param": "", "mismatch": ""}, [ev_a, ev_b])


# --- which port, and did it drop? -----------------------------------------

def _addresses(device: str) -> tuple[Evidence, list[tuple[str, ipaddress.IPv4Interface | ipaddress.IPv6Interface]]]:
    """(the port's own name, its address and subnet) for every address on the device."""
    if lab.platform(device) == "eos":
        ev, data = lab.show_json(device, "show ip interface", None)
        out = []
        for name, info in (data or {}).get("interfaces", {}).items():
            ip = info.get("interfaceAddress", {}).get("primaryIp", {})
            if ip.get("maskLen"):
                out.append((name, ipaddress.ip_interface(f"{ip['address']}/{ip['maskLen']}")))
        return ev, out
    shown, text, _rc = lab.shell(device, ["ip", "-j", "addr"])
    ev = Evidence("device", shown, output=lab._trim(text, 1500))
    out = []
    for link in json.loads(text or "[]"):
        for addr in link.get("addr_info", []):
            out.append((link["ifname"], ipaddress.ip_interface(f"{addr['local']}/{addr['prefixlen']}")))
    return ev, out


@check("neighbor_port", ["device", "neighbor_ip"], ["found", "none"], produces=["interface"], platforms=ALL_DEVICES,
       description="Which port on the device is in the same subnet as the neighbor's address")
def neighbor_port(ctx, device, neighbor_ip):
    target = ipaddress.ip_address(neighbor_ip)
    ev, addresses = _addresses(device)
    for name, interface in addresses:
        if target in interface.network and not interface.is_loopback:
            ev.output, ev.note = f"{name}: {interface}", "Trimmed to the matching port"
            return Result("found", f"{name} on {device} faces {neighbor_ip} (it holds {interface}).",
                          {"interface": name}, [ev])
    return Result("none", f"No port on {device} is in the same subnet as {neighbor_ip}.", evidence=[ev])


@check("link_went_down", ["device", "interface"], ["went_down", "stayed_up"], produces=["link_run"], platforms=HOST,
       description="Whether the same port sent a linkDown trap around the same moment")
def link_went_down(ctx, device, interface):
    from ts import engine, store  # the engine imports the rule schema, which imports these checks
    event = intake.link_down_near(device, interface, ctx.event_time)
    ev = Evidence("file", f"linkDown traps for {interface} on {device} near this event, in {intake.TRAPS_LOG.name}",
                  output=event["raw"].replace("\t", "\n") if event else "(none from 90 s before to 60 s after)")
    if not event:
        return Result("stayed_up", f"{device} sent no linkDown for {interface} around this event.", {"link_run": ""}, [ev])
    run = engine.incident_id(event)
    return Result("went_down", f"{device} sent a linkDown for {interface} at {event['trap_time'][11:]}.",
                  {"link_run": run if store.exists(run) else ""}, [ev])


# --- RIP ------------------------------------------------------------------

_RIP_LINE = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+(\d+):(\d+):(\d+)\s")


def rip_neighbors(device: str) -> tuple[Evidence, dict[str, int] | None]:
    """Neighbor address -> seconds since it was last heard. EOS has no JSON for this command."""
    shown, out, rc = lab.eos(device, "show ip rip neighbors")
    ev = Evidence("device", shown, output=lab._trim(out, 1500))
    if rc != 0:
        return ev, None
    heard = {}
    for line in out.splitlines():
        if m := _RIP_LINE.match(line.strip()):
            heard[m.group(1)] = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
    return ev, heard


@check("rip_neighbor_heard", ["device", "neighbor_ip"], ["recent", "stale", "missing"], platforms=("eos",),
       description="Whether the device has heard this RIP neighbor in the last two minutes")
def rip_neighbor_heard(ctx, device, neighbor_ip):
    ev, heard = rip_neighbors(device)
    if heard is None:
        return Result("error", f"{device} did not return its RIP neighbors.", evidence=[ev])
    if neighbor_ip not in heard:
        return Result("missing", f"{device} no longer lists {neighbor_ip} as a RIP neighbor.", evidence=[ev])
    age = heard[neighbor_ip]
    if age <= RIP_STALE_AFTER:
        return Result("recent", f"{device} heard RIP from {neighbor_ip} {age} s ago.", evidence=[ev])
    return Result("stale", f"{device} last heard RIP from {neighbor_ip} {age} s ago.", evidence=[ev])


@check("rip_interface", ["device", "interface"], ["enabled", "passive", "not_enabled"], platforms=("eos",),
       description="Whether RIP covers the port's subnet and is allowed to speak there")
def rip_interface(ctx, device, interface):
    shown, out, rc = lab.eos(device, "show running-config section router rip")
    ev = Evidence("device", shown, output=lab._trim(out, 1500))
    if rc != 0:
        return Result("error", f"Could not read the RIP config on {device}.", evidence=[ev])
    if "router rip" not in out:
        return Result("not_enabled", f"{device} has no RIP config at all.", evidence=[ev])
    networks = [ipaddress.ip_network(m, strict=False) for m in re.findall(r"^\s*network (\S+)", out, re.M)]
    passive = re.findall(r"^\s*passive-interface (\S+)", out, re.M)
    if interface in passive:
        return Result("passive", f"{interface} is passive under RIP on {device}, so it will not send updates.", evidence=[ev])
    _ev, addresses = _addresses(device)
    own = [a for name, a in addresses if name == interface]
    if own and any(own[0].ip in net for net in networks):
        return Result("enabled", f"RIP covers {own[0]} on {interface} on {device}.", evidence=[ev])
    return Result("not_enabled", f"No RIP network statement on {device} covers {interface}.", evidence=[ev])


# --- BGP ------------------------------------------------------------------

def bgp_peers(device: str) -> tuple[Evidence, dict[str, str] | None]:
    """Peer address -> session state, IPv4 and IPv6."""
    ev, data = lab.show_json(device, None, "show bgp summary json")
    if data is None:
        return ev, None
    peers = {}
    for family in data.values():
        if isinstance(family, dict):
            for peer, info in family.get("peers", {}).items():
                peers[peer] = info.get("state", "unknown")
    return ev, peers


@check("bgp_peer_state", ["device", "peer_ip"], ["established", "admin_shut", "down", "missing"],
       produces=["bgp_state", "last_reset"], platforms=("frr",),
       description="The BGP session's state on this router, and why it last reset")
def bgp_peer_state(ctx, device, peer_ip):
    ev, data = lab.show_json(device, None, f"show bgp neighbors {peer_ip} json")
    info = (data or {}).get(peer_ip)
    if not info:
        return Result("missing", f"{device} has no BGP neighbor {peer_ip} configured.",
                      {"bgp_state": "missing", "last_reset": ""}, [ev])
    state, reset = info.get("bgpState", "unknown"), info.get("lastResetDueTo", "")
    ev.output = json.dumps({k: info.get(k) for k in ("bgpState", "lastResetDueTo", "nbrDesc", "hostLocal", "adminShutDown")}, indent=1)
    ev.note = "Trimmed to the session state"
    values = {"bgp_state": state, "last_reset": reset}
    if state == "Established":
        return Result("established", f"BGP to {peer_ip} is established.", values, [ev])
    if info.get("adminShutDown") or "Admin" in reset:
        return Result("admin_shut", f"BGP to {peer_ip} is shut down in {device}'s config.", values, [ev])
    return Result("down", f"BGP to {peer_ip} is {state}. It last reset because: {reset or 'not given'}.", values, [ev])


@check("arp_neighbor", ["device", "ip"], ["reachable", "unreachable"], platforms=("frr", "linux"),
       description="Whether the device has a working layer 2 entry for that address. This only reads the device's own table")
def arp_neighbor(ctx, device, ip):
    shown, out, rc = lab.shell(device, ["ip", "neigh", "show", ip])
    ev = Evidence("device", shown, output=out.strip() or "(no entry)")
    if rc == 0 and re.search(r"\b(REACHABLE|STALE|DELAY|PROBE)\b", out):
        return Result("reachable", f"{device} has a working layer 2 entry for {ip}.", evidence=[ev])
    return Result("unreachable", f"{device} has no working layer 2 entry for {ip}.", evidence=[ev])


# --- routes and reachability ----------------------------------------------

def route_table(device: str) -> tuple[Evidence, dict[str, dict] | None]:
    """Prefix -> {protocol, via} for the whole IPv4 table."""
    ev, data = lab.show_json(device, "show ip route", "show ip route json", keep=800)
    if data is None:
        return ev, None
    table = {}
    if lab.platform(device) == "eos":
        for prefix, info in data.get("vrfs", {}).get("default", {}).get("routes", {}).items():
            vias = info.get("vias", [])
            table[prefix] = {"protocol": _eos_protocol(info.get("routeType", "")),
                             "via": vias[0].get("nexthopAddr", vias[0].get("interface", "")) if vias else ""}
    else:
        for prefix, entries in data.items():
            best = entries[0]
            hops = best.get("nexthops", [])
            table[prefix] = {"protocol": best.get("protocol", ""),
                             "via": hops[0].get("ip", hops[0].get("interfaceName", "")) if hops else ""}
    return ev, table


def _eos_protocol(route_type: str) -> str:
    kind = route_type.lower()
    for protocol in ("ospf", "rip", "bgp", "static", "connected"):
        if protocol in kind:
            return protocol
    return kind or "unknown"


@check("route_present", ["device", "prefix"], ["present", "missing"], produces=["route_protocol", "route_via"],
       platforms=ALL_DEVICES, description="Whether that prefix is in the device's routing table right now")
def route_present(ctx, device, prefix):
    values = {"route_protocol": "", "route_via": ""}
    if lab.platform(device) == "linux":
        family = ["-6"] if ":" in prefix else []
        target = "default" if prefix in ("0.0.0.0/0", "::/0") else prefix
        shown, out, rc = lab.shell(device, ["ip", *family, "-j", "route", "show", target])
        ev = Evidence("device", shown, output=lab._trim(out, 800))
        routes = json.loads(out or "[]") if rc == 0 else []
        if not routes:
            return Result("missing", f"{device} has no route for {prefix}.", values, [ev])
        via = routes[0].get("gateway", routes[0].get("dev", ""))
        return Result("present", f"{device} reaches {prefix} via {via}.", {"route_protocol": routes[0].get("protocol", ""), "route_via": via}, [ev])
    ev, table = route_table(device)
    if table is None:
        return Result("error", f"{device} did not return its routing table.", evidence=[ev])
    route = table.get(prefix)
    if not route:
        ev.output, ev.note = f"{prefix} is not among the {len(table)} routes on {device}", "The whole table was read"
        return Result("missing", f"{device} has no route for {prefix}.", values, [ev])
    ev.output, ev.note = json.dumps({prefix: route}, indent=1), "Trimmed to this prefix"
    return Result("present", f"{device} has {prefix} from {route['protocol']} via {route['via']}.",
                  {"route_protocol": route["protocol"], "route_via": route["via"]}, [ev])


@check("protocol_neighbors_ok", ["device", "protocol"], ["ok", "degraded", "not_applicable"], produces=["degraded_detail"], platforms=ROUTING,
       description="Whether every neighbor of that routing protocol is up on this device")
def protocol_neighbors_ok(ctx, device, protocol):
    if protocol == "ospf":
        ev, neighbors = ospf_neighbors(device)
        bad = None if neighbors is None else [n["router_id"] for n in neighbors if n["state"] != "full"]
        total = None if neighbors is None else len(neighbors)
    elif protocol == "rip" and lab.platform(device) == "eos":
        ev, heard = rip_neighbors(device)
        bad = None if heard is None else [ip for ip, age in heard.items() if age > RIP_STALE_AFTER]
        total = None if heard is None else len(heard)
    elif protocol == "bgp" and lab.platform(device) == "frr":
        ev, peers = bgp_peers(device)
        bad = None if peers is None else [ip for ip, state in peers.items() if state != "Established"]
        total = None if peers is None else len(peers)
    else:
        return Result("not_applicable", f"{protocol} has no neighbors to check on {device}.", {"degraded_detail": ""})
    if bad is None:
        return Result("error", f"{device} did not return its {protocol} neighbors.", evidence=[ev])
    if total == 0:
        return Result("degraded", f"{device} has no {protocol} neighbors at all.", {"degraded_detail": "none listed"}, [ev])
    if bad:
        detail = ", ".join(bad)
        return Result("degraded", f"On {device}, {protocol} is down to: {detail}.", {"degraded_detail": detail}, [ev])
    return Result("ok", f"All {total} {protocol} neighbors on {device} are up.", {"degraded_detail": ""}, [ev])


def can_ping(device: str, target: str) -> tuple[Evidence, bool]:
    if lab.platform(device) == "eos":
        shown, out, _rc = lab.eos(device, f"ping {target} repeat 2 timeout 2")
    else:
        argv = ["ping", *(["-6"] if ":" in target else []), "-c", "2", "-W", "2", target]
        shown, out, _rc = lab.shell(device, argv)
    m = re.search(r"(\d+)\s+(?:packets\s+)?received", out)
    return Evidence("device", shown, output=lab._trim(out, 800)), bool(m and int(m.group(1)) > 0)


@check("ping", ["device", "target", "source?"], ["reachable", "unreachable"], platforms=ALL_DEVICES,
       description="Whether the device gets a ping reply from that address")
def ping(ctx, device, target, source=None):
    if target in ISP_ADDRESSES:
        return Result("unsupported", f"{target} is on R5, the ISP's router, so it is not pinged directly.")
    ev, ok = can_ping(device, target)
    if ok:
        return Result("reachable", f"{device} gets ping replies from {target}.", evidence=[ev])
    return Result("unreachable", f"{device} gets no ping reply from {target}.", evidence=[ev])


@check("border_internet", [], ["both", "one", "none"], produces=["working_border", "cut_border"], platforms=HOST,
       description="Whether each border router, r3 and r4, can reach the internet test address 8.8.8.8")
def border_internet(ctx):
    results = {device: can_ping(device, "8.8.8.8") for device in ("r3", "r4")}
    evidence = [ev for ev, _ok in results.values()]
    working = [d for d, (_ev, ok) in results.items() if ok]
    cut = [d for d in results if d not in working]
    values = {"working_border": ", ".join(working), "cut_border": ", ".join(cut)}
    if len(working) == 2:
        return Result("both", "Both border routers reach 8.8.8.8.", values, evidence)
    if working:
        return Result("one", f"{working[0]} reaches 8.8.8.8 but {cut[0]} does not.", values, evidence)
    return Result("none", "Neither border router reaches 8.8.8.8.", values, evidence)


# --- link quality ---------------------------------------------------------

LOSSY_FROM = 20   # percent of pings lost that counts as a bad link
_LOSS = re.compile(r"(\d+(?:\.\d+)?)% packet loss")


def ping_loss(device: str, target: str, count: int = 10) -> tuple[Evidence, float | None]:
    """Percent of pings lost, or None if the device did not say."""
    if lab.platform(device) == "eos":
        shown, out, _rc = lab.eos(device, f"ping {target} repeat {count} interval 0.2 timeout 1", timeout=count + 20)
    else:
        shown, out, _rc = lab.shell(device, ["ping", "-c", str(count), "-i", "0.2", "-W", "1", "-q", target], timeout=count + 20)
    m = _LOSS.search(out)
    return Evidence("device", shown, output=lab._trim(out, 800)), float(m.group(1)) if m else None


@check("link_loss", ["device", "interface", "target"], ["clean", "lossy", "dead"], produces=["loss_pct"], platforms=ROUTING,
       description="Sends 50 pings across the link and measures how many are lost")
def link_loss(ctx, device, interface, target):
    if target in ISP_ADDRESSES:
        return Result("unsupported", f"{target} is on the ISP's router, so it is not probed.")
    ev, lost = ping_loss(device, target, 50)
    if lost is None:
        return Result("error", f"{device} did not report a ping result.", evidence=[ev])
    pct = f"{lost:g}"
    if lost >= 100:
        return Result("dead", f"All 50 pings from {device} to {target} over {interface} were lost.", {"loss_pct": pct}, [ev])
    if lost >= LOSSY_FROM:
        return Result("lossy", f"{pct}% of 50 pings from {device} to {target} over {interface} were lost.", {"loss_pct": pct}, [ev])
    return Result("clean", f"Only {pct}% of 50 pings from {device} to {target} over {interface} were lost.", {"loss_pct": pct}, [ev])


@check("other_links_clean", ["device", "interface"], ["clean", "lossy", "none"], produces=["lossy_links"], platforms=ROUTING,
       description="Whether the device's other links are clean, to tell one bad link from a sick device")
def other_links_clean(ctx, device, interface):
    ev, neighbors = ospf_neighbors(device)
    if neighbors is None:
        return Result("error", f"{device} did not return its OSPF neighbors.", evidence=[ev])
    port = lab.linux_name(interface)
    others = [n for n in neighbors if lab.linux_name(n["interface"]) != port and n["address"] not in ISP_ADDRESSES][:3]
    if not others:
        return Result("none", f"{device} has no other OSPF links to compare with.", {"lossy_links": ""}, [ev])
    lossy, evidence = [], [ev]
    for n in others:
        probe, lost = ping_loss(device, n["address"], 20)
        evidence.append(probe)
        if lost is None or lost >= LOSSY_FROM:
            lossy.append(f"{n['interface']} ({'no answer' if lost is None else f'{lost:g}%'})")
    if lossy:
        return Result("lossy", f"{device}'s other links are losing packets too: {', '.join(lossy)}.", {"lossy_links": ", ".join(lossy)}, evidence)
    return Result("clean", f"{device}'s other {len(others)} links are clean, so the problem is this one link.", {"lossy_links": ""}, evidence)


def _error_counters(device: str, interface: str) -> tuple[Evidence, dict[str, int] | None]:
    if lab.platform(device) == "eos":
        ev, data = lab.show_json(device, f"show interfaces {interface} counters errors", None)
        counters = ((data or {}).get("interfaceErrorCounters") or {}).get(interface)
        return ev, {k: int(v) for k, v in counters.items()} if counters else None
    port = lab.linux_name(interface)
    shown, out, rc = lab.shell(device, ["ip", "-j", "-s", "link", "show", port])
    ev = Evidence("device", shown, output=lab._trim(out, 1200))
    try:
        stats = json.loads(out)[0]["stats64"] if rc == 0 else None
    except (ValueError, IndexError, KeyError):
        return ev, None
    if stats is None:
        return ev, None
    return ev, {"rx errors": stats["rx"]["errors"], "rx dropped": stats["rx"]["dropped"],
                "tx errors": stats["tx"]["errors"], "tx dropped": stats["tx"]["dropped"]}


@check("interface_errors", ["device", "interface", "target", "source?"], ["errors", "clean"], produces=["error_detail"], platforms=ROUTING,
       description="Whether the port's error counters climb while traffic crosses it")
def interface_errors(ctx, device, interface, target, source=None):
    ev1, before = _error_counters(device, interface)
    if before is None:
        return Result("error", f"{device} did not return error counters for {interface}.", evidence=[ev1])
    probe, _lost = ping_loss(source or device, target, 25)  # traffic that crosses this port, from whichever end can send it
    ev2, after = _error_counters(device, interface)
    if after is None:
        return Result("error", f"{device} did not return error counters for {interface}.", evidence=[ev1, probe, ev2])
    grew = {k: after[k] - before.get(k, 0) for k in after if after[k] > before.get(k, 0)}
    ev2.note = "Second reading, taken after 25 pings"
    if grew:
        detail = ", ".join(f"{k} +{v}" for k, v in grew.items())
        return Result("errors", f"{interface} on {device} counted errors while traffic crossed it: {detail}.", {"error_detail": detail}, [ev1, probe, ev2])
    return Result("clean", f"{interface} on {device} counted no new errors while traffic crossed it.", {"error_detail": ""}, [ev1, probe, ev2])
