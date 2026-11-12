"""Turn lines from snmptrapd's log into events, and hold events until they are ready to investigate."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from ts.vocab import DEVICES

TRAPS_LOG = Path(os.environ.get("TS_TRAPS_LOG", "/var/log/nmas/traps.log"))
TRAP_OID = "iso.3.6.1.6.3.1.1.4.1.0"
LINK_DOWN = "iso.3.6.1.6.3.1.1.5.3"
COLD_START = "iso.3.6.1.6.3.1.1.5.1"
AGENT_START = "iso.3.6.1.4.1.8072.4.0.1"  # the SNMP agent itself started
OSPF_NEIGHBOR = "iso.3.6.1.2.1.14.16.2.2"
VRRP_NEW_MASTER = "iso.3.6.1.2.1.68.0.1"
CONFIG_SAVED = "iso.3.6.1.4.1.30065.3.9.3.0.1"  # Arista: a copy to startup-config

_LINE = re.compile(r"^\[(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] TRAPv2 from (?:clab-lab1-)?(?P<dev>[\w-]+): (?P<body>.*)$")
_VARBIND = re.compile(r"^(?P<oid>iso[\d.]+) = (?:(?P<type>[A-Za-z0-9-]+): )?(?P<value>.*)$")
_IF_FIELD = re.compile(r"^iso\.3\.6\.1\.2\.1\.2\.2\.1\.(?P<col>[1278])\.(?P<idx>\d+)$")
_ALIAS = re.compile(r"^iso\.3\.6\.1\.2\.1\.31\.1\.1\.1\.18\.\d+$")
_VRRP_MASTER = re.compile(r"^iso\.3\.6\.1\.2\.1\.68\.1\.3\.1\.7\.(?P<index>\d+)\.(?P<vrid>\d+)$")
_STATUS = {"1": "up", "2": "down", "3": "testing"}
_VLAN_IF_BASE = 2000000  # EOS numbers Vlan interfaces 2000000 + VLAN id
_OSPF_DOWN = "1"  # ospfNbrState: 1 is down, 8 is full; the steps between are a neighbor coming up


def _bind(binds: dict, prefix: str) -> str | None:
    return next((v for k, v in binds.items() if k.startswith(prefix)), None)


def _link_down(device: str, binds: dict) -> dict:
    fields = {}
    for oid, value in binds.items():
        f = _IF_FIELD.match(oid)
        if f:
            fields[{"1": "if_index", "2": "interface", "7": "admin_status", "8": "oper_status"}[f.group("col")]] = value
        elif _ALIAS.match(oid):
            fields["description"] = value
    interface = fields.get("interface", f"ifIndex {fields.get('if_index', '?')}")
    return {
        "event": "interface.down", "interface": interface, "subject": interface,
        "description": fields.get("description", ""),
        "admin_status": _STATUS.get(fields.get("admin_status", ""), "unknown"),
        "title": f"{device} {interface} went down", "trigger": f"linkDown arrived from {device}",
    }


def _ospf_neighbor(device: str, binds: dict) -> dict | None:
    if _bind(binds, "iso.3.6.1.2.1.14.10.1.6.") != _OSPF_DOWN:
        return None
    neighbor_id = _bind(binds, "iso.3.6.1.2.1.14.10.1.3.") or "unknown"
    neighbor_ip = _bind(binds, "iso.3.6.1.2.1.14.10.1.1.") or ""
    return {
        "event": "ospf.neighbor_down", "neighbor_id": neighbor_id, "neighbor_ip": neighbor_ip,
        "subject": f"OSPF {neighbor_id}", "title": f"{device} lost OSPF neighbor {neighbor_id}",
        "trigger": f"OSPF neighbor trap arrived from {device}",
        "detail": f"{device} said its OSPF neighbor {neighbor_id}, at {neighbor_ip}, went down.",
    }


def _vrrp_master(device: str, binds: dict) -> dict | None:
    for oid, value in binds.items():
        m = _VRRP_MASTER.match(oid)
        if m:
            index = int(m.group("index"))
            interface = f"Vlan{index - _VLAN_IF_BASE}" if index > _VLAN_IF_BASE else f"ifIndex {index}"
            return {
                "event": "vrrp.master_change", "interface": interface, "vrid": m.group("vrid"), "master_ip": value,
                "subject": f"VRRP {interface}", "title": f"VRRP master changed on {interface}",
                "trigger": f"VRRP new-master trap arrived from {device}",
                "detail": f"{device} took over as master for {interface}, using {value}.",
                "merge_key": f"vrrp:{interface}",
            }
    return None


def _restart(device: str, _binds: dict) -> dict:
    return {
        "event": "device.restarted", "subject": "restart", "title": f"{device} reported a restart",
        "trigger": f"Restart trap arrived from {device}",
        "detail": f"The SNMP agent on {device} said it just started. That can be the device or only the agent.",
        "merge_key": f"restart:{device}",
    }


def _config_saved(device: str, _binds: dict) -> dict:
    return {
        "event": "config.changed", "subject": "config", "title": f"Config saved on {device}",
        "trigger": f"Config-saved trap arrived from {device}",
        "detail": f"{device} said its running config was copied to startup-config.",
        "merge_key": f"config:{device}",
    }


_BUILDERS = {LINK_DOWN: _link_down, OSPF_NEIGHBOR: _ospf_neighbor, VRRP_NEW_MASTER: _vrrp_master,
             COLD_START: _restart, AGENT_START: _restart, CONFIG_SAVED: _config_saved}


def parse_line(line: str) -> dict | None:
    """A trap line as an event, or None for a trap nothing investigates."""
    m = _LINE.match(line.rstrip("\n"))
    if not m:
        return None
    binds = {}
    for part in m.group("body").split("\t"):
        vb = _VARBIND.match(part.strip())
        if vb:
            binds[vb.group("oid")] = vb.group("value").strip().strip('"')
    builder = _BUILDERS.get(binds.get(TRAP_OID))
    device = m.group("dev")
    fields = builder(device, binds) if builder else None
    if fields is None:
        return None
    role, platform = DEVICES.get(device, ("unknown", "unknown"))
    epoch = time.mktime(time.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"))  # snmptrapd logs local time
    return {"source": "trap", "severity": "warning", "device": device, "role": role, "platform": platform,
            **fields, "time": epoch, "trap_time": m.group("ts"), "raw": line.rstrip("\n")}


def event_for(kind: str, device: str, subject: str, title: str, trigger: str, detail: str, **fields) -> dict:
    """An event found by looking rather than by a trap. The sweep stamps its time when it fires."""
    role, platform = DEVICES.get(device, ("unknown", "unknown"))
    return {"event": kind, "source": "health", "severity": "warning", "device": device, "role": role, "platform": platform,
            "subject": subject, "title": title, "trigger": trigger, "detail": detail, "raw": detail, **fields}


# --- waiting until an event is ready --------------------------------------

# Seconds after an event before investigating it. Link-down needs Prometheus to have a clean
# minute (checks.TRAFFIC_WINDOW). OSPF waits a little longer, so the link-down run for the
# same port exists first and can be pointed at.
HOLD = {"interface.down": 120, "ospf.neighbor_down": 150, "config.changed": 180, "device.restarted": 180,
        "monitor.fired": 20}  # a monitor has already waited for its own conditions
DEFAULT_HOLD = 120
MAX_HOLD = 1200  # a burst that never goes quiet is still investigated after this long


class Waiting:
    """Events waiting for their turn. Events sharing a merge_key become one investigation:
    a nightly job can save a config 100 times in an hour, and only the end state matters."""

    def __init__(self):
        self.items: list[dict] = []

    def add(self, event: dict) -> bool:
        """Returns False when the event was merged into one already waiting."""
        hold = HOLD.get(event["event"], DEFAULT_HOLD)
        key = event.get("merge_key")
        for item in self.items:
            if key and item["key"] == key:
                item["event"], item["count"] = event, item["count"] + 1
                item["due"] = min(event["time"] + hold, item["first"] + MAX_HOLD)
                return False
        self.items.append({"key": key, "event": event, "first": event["time"], "count": 1, "due": event["time"] + hold})
        return True

    def ready(self, now: float) -> list[dict]:
        done = [item for item in self.items if item["due"] <= now]
        self.items = [item for item in self.items if item["due"] > now]
        for item in done:
            if item["count"] > 1:
                item["event"]["merged"] = item["count"]
        return [item["event"] for item in done]


def follow(path: Path = TRAPS_LOG, poll: float = 1.0, from_start: bool = False, stop=lambda: False):
    """Yield each new line appended to the trap log, and None whenever there is nothing new.

    logrotate renames traps.log at midnight and creates a fresh one, so when the
    path stops pointing at the open file, this finishes the old one and reopens
    the new one from its start. A line still being written is held back until
    it is complete.
    """
    fh, partial = None, ""
    try:
        while not stop():
            if fh is None:
                try:
                    fh = open(path, errors="replace")
                except FileNotFoundError:
                    yield None
                    time.sleep(poll)
                    continue
                if not from_start:
                    fh.seek(0, os.SEEK_END)
            chunk = fh.readline()
            if chunk:
                partial += chunk
                if partial.endswith("\n"):
                    line, partial = partial, ""
                    yield line
                continue
            try:
                rotated = os.stat(path).st_ino != os.fstat(fh.fileno()).st_ino
            except FileNotFoundError:
                rotated = False  # between the rename and the create; keep reading the old file
            if rotated:
                fh.close()
                fh, partial, from_start = None, "", True
                continue
            yield None
            time.sleep(poll)
    finally:
        if fh:
            fh.close()


def link_down_events(since: float = 0.0, path: Path = TRAPS_LOG) -> list[dict]:
    events = []
    with open(path, errors="replace") as fh:
        for line in fh:
            if LINK_DOWN not in line:
                continue
            ev = parse_line(line)
            if ev and ev["time"] >= since:
                events.append(ev)
    return events


def events_since(since: float, path: Path = TRAPS_LOG) -> list[dict]:
    """Every event in the trap log from that moment on."""
    events = []
    with open(path, errors="replace") as fh:
        for line in fh:
            ev = parse_line(line)
            if ev and ev["time"] >= since:
                events.append(ev)
    return events


def link_down_near(device: str, interface: str, when: float, before: int = 90, after: int = 60,
                   path: Path = TRAPS_LOG) -> dict | None:
    """The linkDown trap for that port closest to a moment, looking in today's log and the one rotated out."""
    best = None
    for candidate in (path, Path(f"{path}.1")):
        try:
            fh = open(candidate, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if LINK_DOWN not in line or f"-{device}:" not in line and f" {device}:" not in line:
                    continue
                ev = parse_line(line)
                if (ev and ev["device"] == device and ev["interface"] == interface and when - before <= ev["time"] <= when + after
                        and (best is None or abs(ev["time"] - when) < abs(best["time"] - when))):
                    best = ev
    return best
