"""The fixed list of trouble tags.

Six areas, four tags each, so no part of the network gets more detail than another.
A rule says which tags it runs for, every event and cause maps to tags, and a run
carries the tags of what it saw and what it found. Tags are written area/name, and
naming just the area means all four.

The list changes only when a person accepts a new tag on the review page.
"""

from __future__ import annotations

import re
from pathlib import Path

from ts import history
from ts.vocab import EVENT_TYPES

AREAS: dict[str, dict[str, str]] = {
    "routing": {
        "ospf": "OSPF neighbors and adjacencies",
        "bgp": "BGP sessions",
        "rip": "RIP neighbors",
        "route": "A route that is missing or wrong",
    },
    "switching": {
        "vlan": "VLANs and which ports carry them",
        "vrrp": "Gateway redundancy",
        "stp": "Spanning tree",
        "lag": "Port channels",
    },
    "link": {
        "port": "A port that went down",
        "loss": "A link that is up but dropping packets",
        "flap": "A link going up and down repeatedly",
        "cabling": "The cable, optics or whatever is plugged in",
    },
    "device": {
        "restart": "A device restarted",
        "unreachable": "A device stopped answering",
        "resources": "CPU, memory or disk",
        "software": "A process or daemon on the device",
    },
    "service": {
        "dhcp": "Addresses handed to hosts",
        "nat": "Address translation at the border",
        "dns": "Name resolution",
        "internet": "Reaching the outside world",
    },
    "config": {
        "drift": "Config no longer matches known-good",
        "by-person": "Changed by someone by hand",
        "by-automation": "Changed by a job",
        "firewall": "A filter or firewall rule in the path",
    },
}

EVENT_TAGS: dict[str, tuple[str, ...]] = {
    "interface.down": ("link/port",),
    "interface.up": ("link/port",),
    "ospf.neighbor_down": ("routing/ospf",),
    "ospf.neighbor_up": ("routing/ospf",),
    "rip.neighbor_lost": ("routing/rip",),
    "bgp.peer_down": ("routing/bgp",),
    "bgp.peer_up": ("routing/bgp",),
    "vrrp.master_change": ("switching/vrrp",),
    "config.changed": ("config/drift",),
    "device.restarted": ("device/restart",),
    "device.unreachable": ("device/unreachable",),
    "route.missing": ("routing/route",),
    "internet.unreachable": ("service/internet",),
    "ticket.report": (),  # someone says something is wrong and nothing yet says what: no rule applies until the AI has looked
    "dhcp.failure": ("service/dhcp",),
    "link.degraded": ("link/loss",),
    "nat.missing": ("service/nat",),
    "routes.missing": ("routing/route",),
    "mac.learn": ("switching/vlan",),
    "mac.age": ("switching/vlan",),
    "lldp.change": ("link/cabling",),
    "health.failure": ("device/resources",),
    "monitor.fired": (),  # what a monitor is about is on the event itself (its tags), so no ordinary rule runs for it unasked
}

_CAUSE_TAGS = [
    (re.compile(r"^ospf\."), "routing/ospf"),
    (re.compile(r"^bgp\."), "routing/bgp"),
    (re.compile(r"^rip\."), "routing/rip"),
    (re.compile(r"^(route|routing)\.|^border\.routes"), "routing/route"),
    (re.compile(r"^vrrp\."), "switching/vrrp"),
    (re.compile(r"^dhcp\."), "service/dhcp"),
    (re.compile(r"^border\.nat"), "service/nat"),
    (re.compile(r"^internet\."), "service/internet"),
    (re.compile(r"^interface\.(no_link|errdisabled|far_end_errdisabled|missing)|^link\.physical"), "link/cabling"),
    (re.compile(r"^link\."), "link/loss"),
    (re.compile(r"^interface\.(admin_shut|far_end_shut)|^config\.changed_by_(person|unknown)"), "config/by-person"),
    (re.compile(r"^config\.changed_by_automation"), "config/by-automation"),
    (re.compile(r"^config\.drift|drifted$"), "config/drift"),
    (re.compile(r"^firewall\."), "config/firewall"),
    (re.compile(r"^device\.restarted"), "device/restart"),
    (re.compile(r"^device\.(down|mgmt_unreachable)"), "device/unreachable"),
    (re.compile(r"^device\.wide_loss"), "link/loss"),
]
_NAME = re.compile(r"^[a-z][a-z0-9-]{1,23}$")

BUILT_IN = {f"{area}/{name}": text for area, names in AREAS.items() for name, text in names.items()}


def known(path: Path | None = None) -> dict[str, str]:
    """Every tag there is: the built-in list plus any a person has accepted. Tag name -> what it means."""
    with history._open(path) as db:
        added = {row["name"]: row["description"] for row in db.execute("SELECT name, description FROM tags")}
    return {**BUILT_IN, **added}


def is_known(tag: str, path: Path | None = None) -> bool:
    return tag in AREAS or tag in known(path)


def _added_for(kind: str, path: Path | None = None) -> tuple[str, ...]:
    with history._open(path) as db:
        return tuple(row["name"] for row in db.execute("SELECT name, events FROM tags") if kind in (row["events"] or "").split(","))


def for_event(kind: str, path: Path | None = None) -> tuple[str, ...]:
    return (*EVENT_TAGS.get(kind, ()), *_added_for(kind, path))


def for_cause(label: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(tag for pattern, tag in _CAUSE_TAGS if pattern.search(label or "")))


def for_record(rec: dict) -> list[str]:
    """What a run is about: where the alarm came from, then what was found."""
    rule = rec.get("rule") or {}
    found = rule.get("tags", []) if rule.get("position") in ("beginning", "end") and rec["result"].get("kind") == "cause" else []  # what a specific rule says it is about
    own = [t for t in rec["event"].get("tags") or [] if isinstance(t, str)]  # a monitor says what it watches for
    return list(dict.fromkeys([*for_event(rec["event"].get("event", "")), *own, *for_cause(rec["result"].get("label") or ""), *found]))


def covers(patterns, tags) -> bool:
    """Does any pattern (a whole area, or one exact tag) cover any of these tags?"""
    return any(tag == pattern or tag.split("/", 1)[0] == pattern for pattern in patterns for tag in tags)


def events_for(patterns) -> set[str]:
    """The event types a list of tags would fire on."""
    return {kind for kind in EVENT_TYPES if covers(patterns, for_event(kind))}


def name_problem(name: str, area: str, path: Path | None = None) -> str | None:
    """Why a suggested new tag does not fit the list, or None if it does."""
    if area not in AREAS:
        return f"{area!r} is not an area. The areas are fixed: {', '.join(AREAS)}."
    if not _NAME.match(name):
        return "A tag name is one lowercase word or hyphenated phrase of 2 to 24 characters."
    full, have = f"{area}/{name}", known(path)
    if full in have:
        return f"{full} already exists."
    for other in have:
        other_name = other.split("/", 1)[1]
        if name == other_name or name.startswith(f"{other_name}-") or name.endswith(f"-{other_name}"):
            return f"{name!r} is only a variation of {other}, which already exists. Use that one."
    return None
