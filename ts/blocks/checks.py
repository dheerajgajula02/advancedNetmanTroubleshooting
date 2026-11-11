"""Read-only checks used by the interface-down rule.

Each check is called as ``func(ctx, **args)`` and returns a ``Result``: one of
its declared outcomes, a plain sentence of what it saw, any values it sets for
later steps, and the evidence (exact command or query plus what came back).
``ctx.event_time`` is when the trap arrived (epoch seconds).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from ts import configs, lab
from ts.vocab import DEVICES
from ts.lab import Evidence, Result
from ts.registry import ANY_PLATFORM, check

HOST = (ANY_PLATFORM,)


def _clock(epoch: float) -> str:
    return time.strftime("%-I:%M:%S %p", time.localtime(epoch))


# --- confirm: does Prometheus agree with the trap? ------------------------

# gNMI counters arrive one sample late: the first sample after a drop still
# carries packets counted just before it. So traffic is judged on a window
# that starts 30 s after the trap. A live core link always carries OSPF
# hellos (every 10 s), so 60 s with no packets means the link is really dead.
# The engine has to run after the window ends (CONFIRM_DELAY in ts/cli.py).
TRAFFIC_WINDOW = (30, 90)
POLL_WINDOW = (-30, 120)

@check("snmp_saw_down", ["device", "interface"], ["down", "up", "no_data"], platforms=HOST,
       description="Whether SNMP polling (every 15 s) saw the port down around the trap")
def snmp_saw_down(ctx, device, interface):
    start, end = ctx.event_time + POLL_WINDOW[0], min(ctx.event_time + POLL_WINDOW[1], time.time())
    query = f'network_interface_oper_status{{device="{device}",interface="{interface}"}}'
    ev, series = lab.prom_range(query, start, end)
    ev.note = "1 = up, 2 = down (IF-MIB ifOperStatus)"
    if not series:
        return Result("no_data", f"Prometheus has no polling data for {interface} on {device}.", evidence=[ev])
    down = [t for t, v in series if v != 1]
    if down:
        return Result("down", f"Polling saw {interface} down in {len(down)} of {len(series)} samples, "
                              f"first at {_clock(down[0])}.", evidence=[ev])
    return Result("up", f"Polling saw {interface} up in all {len(series)} samples around the trap.", evidence=[ev])


@check("traffic_stopped", ["device", "interface"], ["stopped", "flowing", "no_data"], platforms=HOST,
       description="Whether packets stopped crossing the port, from 30 s to 90 s after the trap (gNMI counters)")
def traffic_stopped(ctx, device, interface):
    start, end = ctx.event_time + TRAFFIC_WINDOW[0], min(ctx.event_time + TRAFFIC_WINDOW[1], time.time())
    when = f"from {TRAFFIC_WINDOW[0]} s to {int(end - ctx.event_time)} s after the trap"
    evidence, moved = [], {}
    for direction in ("in", "out"):
        query = f'interfaces_interface_state_counters_{direction}_pkts{{source="{device}",interface_name="{interface}"}}'
        ev, series = lab.prom_range(query, start, end)
        evidence.append(ev)
        if len(series) >= 3:
            moved[direction] = int(series[-1][1] - series[0][1])
            ev.note = f"{moved[direction]} packets {direction} during the window"
    if len(moved) < 2:
        return Result("no_data", f"Prometheus has too few counter samples for {interface} on {device} "
                                 f"{when}.", evidence=evidence)
    if moved["in"] == 0 and moved["out"] == 0:
        return Result("stopped", f"No packets crossed {interface} {when}.", evidence=evidence)
    return Result("flowing", f"Traffic was flowing: {moved['in']} packets in and {moved['out']} out "
                             f"{when}.", evidence=evidence)


# --- investigate ----------------------------------------------------------

_LOG_TIME = re.compile(r"^([A-Z][a-z]{2})\s+(\d+)\s+(\d\d:\d\d:\d\d)\s")
_CONFIG_ENTER = re.compile(r"%SYS-5-CONFIG_E: Enter configuration mode from (.+)$")
_CONFIG_DONE = re.compile(r"%SYS-5-CONFIG_I: Configured from (.+)$")
_FROM_IP = re.compile(r"\((\d+\.\d+\.\d+\.\d+)\)")
# Who sits behind the management addresses seen in config log lines.
KNOWN_SOURCES = {"172.20.20.61": "Guacamole", "172.20.20.1": "the automation server: Jenkins, scripts or ssh from boulder"}
CONFIG_LOOKBACK = 900  # a person can sit in config mode a while before the change


def _log_epoch(line: str, year: int) -> float | None:
    m = _LOG_TIME.match(line)
    if not m:
        return None
    stamp = datetime.strptime(f"{year} {m.group(1)} {m.group(2)} {m.group(3)}", "%Y %b %d %H:%M:%S")
    return stamp.replace(tzinfo=timezone.utc).timestamp()  # cEOS logs in UTC


def _who(raw: str) -> str:
    """'console by admin on vty4 (172.20.20.61)' -> 'admin on vty4 (172.20.20.61, Guacamole)'."""
    who = raw.removeprefix("console by ").strip()
    ip = _FROM_IP.search(who)
    if ip and ip.group(1) in KNOWN_SOURCES:
        who = who.replace(ip.group(0), f"({ip.group(1)}, {KNOWN_SOURCES[ip.group(1)]})")
    return who


def config_sessions(lines: list[tuple[float, str]]) -> list[tuple[float | None, float | None, str]]:
    """(entered, saved, who) for each config session. EOS logs CONFIG_E on 'configure' and CONFIG_I
    on leaving config mode, so a change made at an interactive prompt sits between the two.
    A one-shot change (Cli -c) logs only CONFIG_I, so it has no entered time. saved is None
    while the session is still open."""
    sessions, open_ = [], {}
    for when, line in lines:
        if m := _CONFIG_ENTER.search(line):
            open_[m.group(1).strip()] = when
        elif m := _CONFIG_DONE.search(line):
            raw = m.group(1).strip()
            sessions.append((open_.pop(raw, None), when, raw))
    sessions += [(entered, None, raw) for raw, entered in open_.items()]
    return sessions


@check("device_log", ["device", "interface"], ["config_change", "link_lost", "nothing"],
       produces=["changed_by"], platforms=("eos",),
       description="What the device logged about the port around the trap, and whether a config change came with it")
def device_log(ctx, device, interface):
    shown, out, rc = lab.eos(device, "show logging")
    if rc != 0:
        return Result("error", f"Could not read the log on {device}.", evidence=[Evidence("device", shown, output=lab._trim(out))])
    year = datetime.fromtimestamp(ctx.event_time, timezone.utc).year
    lo, hi = ctx.event_time - 120, ctx.event_time + 120
    port = re.compile(rf"\b{re.escape(interface)}\b")
    relevant, downs, config_lines = [], [], []
    for line in out.splitlines():
        when = _log_epoch(line, year)
        if when is None:
            continue
        is_config = "%SYS-5-CONFIG_" in line
        if is_config and ctx.event_time - CONFIG_LOOKBACK <= when <= hi:
            config_lines.append((when, line))
            relevant.append(line)
        elif lo <= when <= hi and port.search(line):
            relevant.append(line)
            if "changed state to down" in line:
                downs.append(when)
    ev = Evidence("device", shown, output="\n".join(relevant) or "(no lines about this port in the 4 minutes around the trap)",
                  window=[lo, hi], note="Filtered to lines about the port (2 minutes either side) "
                                        "and config-mode lines (from 15 minutes before)")
    for down in downs:
        for entered, saved, raw in config_sessions(config_lines):
            who = _who(raw)
            if entered is not None and entered <= down and (saved is None or down <= saved):
                ended = f"left config mode at {_clock(saved)}" if saved else "is still in config mode"
                return Result("config_change", f"{device} logged {interface} going down while {who} was in config mode: "
                                               f"entered at {_clock(entered)}, the port went down at {_clock(down)}, "
                                               f"and they {ended}.", {"changed_by": who}, [ev])
            if entered is None and abs(saved - down) <= 5:
                return Result("config_change", f"{device} logged {interface} going down at the same second as a "
                                               f"one-shot config change by {who}.", {"changed_by": who}, [ev])
    if downs:
        return Result("link_lost", f"{device} logged {interface} going down with no config change around it.",
                      {"changed_by": ""}, [ev])
    return Result("nothing", f"{device}'s log has nothing about {interface} around the trap "
                             f"(the log buffer may have rolled over).", {"changed_by": ""}, [ev])


_EOS_STATUS = {"connected": "up", "disabled": "admin_down", "errdisabled": "errdisabled", "notconnect": "down"}


@check("interface_status", ["device", "interface"], ["up", "admin_down", "down", "errdisabled", "not_found"],
       platforms=("eos", "frr", "linux"), description="The port's state right now: up, shut down, or down")
def interface_status(ctx, device, interface):
    if lab.platform(device) == "eos":
        shown, out, rc = lab.eos(device, f"show interfaces {interface} | json")
        ev = Evidence("device", shown, output=lab._trim(out, 2500))
        if rc != 0 or "Invalid input" in out:
            return Result("not_found", f"{device} has no interface {interface}.", evidence=[ev])
        info = json.loads(out)["interfaces"][interface]
        status, proto = info.get("interfaceStatus", "?"), info.get("lineProtocolStatus", "?")
        ev.output = json.dumps({k: info.get(k) for k in ("interfaceStatus", "lineProtocolStatus", "description",
                                                         "lastStatusChangeTimestamp")}, indent=2)
        ev.note = "Trimmed to the state fields"
        outcome = _EOS_STATUS.get(status, "down")
        words = {"up": "up", "admin_down": "shut down (disabled)", "errdisabled": "error-disabled",
                 "down": "down (not connected)"}[outcome]
        return Result(outcome, f"Right now {interface} on {device} is {words}; line protocol {proto}.", evidence=[ev])
    port = lab.linux_name(interface)
    shown, out, rc = lab.shell(device, ["ip", "-j", "link", "show", port])
    ev = Evidence("device", shown, output=lab._trim(out, 2500))
    if rc != 0:
        return Result("not_found", f"{device} has no interface {port}.", evidence=[ev])
    info = json.loads(out)[0]
    if "UP" not in info.get("flags", []):
        if lab.platform(device) == "frr":
            shown_config, config = configs.running(device)
            ev_config = Evidence("device", shown_config, note=f"Looking for shutdown under interface {port}")
            if config is not None and "shutdown" not in configs.sections(config).get(f"interface {port}", []):
                return Result("down", f"Right now {port} on {device} is down, but its config does not shut it down.", evidence=[ev, ev_config])
        return Result("admin_down", f"Right now {port} on {device} is shut down.", evidence=[ev])
    if info.get("operstate") == "UP" or "LOWER_UP" in info.get("flags", []):
        return Result("up", f"Right now {port} on {device} is up.", evidence=[ev])
    return Result("down", f"Right now {port} on {device} is enabled but has no carrier.", evidence=[ev])


@check("topology_peer", ["device", "interface"], ["found", "none"], produces=["peer_device", "peer_interface"],
       platforms=HOST, description="Which device and port the topology file says sit on the other end")
def topology_peer(ctx, device, interface):
    found = lab.topology_peer(device, interface)
    ran = f"cabling inventory, {device} {interface}"
    if found:
        peer, peer_iface, line = found
        return Result("found", f"The other end of {interface} is {peer_iface} on {peer}.",
                      {"peer_device": peer, "peer_interface": peer_iface}, [Evidence("file", ran, output=line)])
    evidence = [Evidence("file", ran, output="(no matching link)")]
    if lab.platform(device) == "eos":  # the expansion routers are not in the topology file, but they do speak LLDP
        ev, data = lab.show_json(device, "show lldp neighbors", None)
        evidence.append(ev)
        for entry in (data or {}).get("lldpNeighbors", []):
            if entry.get("port") == interface and entry.get("neighborDevice") in DEVICES:
                peer, peer_iface = entry["neighborDevice"], entry.get("neighborPort", "")
                return Result("found", f"LLDP says the other end of {interface} is {peer_iface} on {peer}.",
                              {"peer_device": peer, "peer_interface": peer_iface}, evidence)
    return Result("none", f"Neither the cabling inventory nor LLDP knows what is on {interface} of {device}.", evidence=evidence)


@check("device_up", ["device"], ["up", "down"], platforms=HOST, description="Whether the device answers on its console")
def device_up(ctx, device):
    shown, out, rc = lab.console_probe(device)
    if rc == 0:
        return Result("up", f"{device} answers on its console.", evidence=[Evidence("device", shown, output=lab._trim(out, 500))])
    return Result("down", f"{device} does not answer on its console.", evidence=[Evidence("device", shown, output="(no response)")])
