"""Shared vocabulary: event types, their fields, device roles and platforms.

Rules match on these names, so the linter rejects anything outside them.
The device map is static for M1; M2 reads it from lab1's inventory.
"""

from __future__ import annotations

# Fields every normalized event carries.
COMMON_FIELDS = frozenset({"event", "device", "role", "platform", "source", "severity", "time"})

# Event type -> extra fields the intake guarantees (it resolves interface names,
# neighbor addresses and so on before an event reaches the rule tables).
EVENT_TYPES: dict[str, frozenset[str]] = {
    "interface.down": frozenset({"interface"}),
    "interface.up": frozenset({"interface"}),
    "ospf.neighbor_down": frozenset({"neighbor_id", "neighbor_ip"}),
    "ospf.neighbor_up": frozenset({"neighbor_id", "neighbor_ip"}),
    "rip.neighbor_lost": frozenset({"neighbor_ip"}),
    "bgp.peer_down": frozenset({"peer_ip"}),
    "bgp.peer_up": frozenset({"peer_ip"}),
    "vrrp.master_change": frozenset({"interface", "vrid"}),
    "config.changed": frozenset(),
    "device.restarted": frozenset(),
    "device.unreachable": frozenset(),
    "route.missing": frozenset({"prefix", "expected_protocol"}),
    "internet.unreachable": frozenset({"internet_target", "default_prefix"}),
    "dhcp.failure": frozenset(),
    "link.degraded": frozenset({"interface", "neighbor_ip"}),
    "nat.missing": frozenset(),
    "routes.missing": frozenset(),
    "mac.learn": frozenset({"interface"}),
    "mac.age": frozenset({"interface"}),
    "lldp.change": frozenset(),
    "health.failure": frozenset({"category"}),
    "ticket.report": frozenset({"ticket_id"}),
}

ROLES = frozenset({"border", "core", "distribution", "access", "expansion", "host", "isp"})
PLATFORMS = frozenset({"eos", "frr", "linux"})
SOURCES = frozenset({"trap", "syslog", "gnmi", "health", "ticket", "manual"})
SEVERITIES = frozenset({"debug", "info", "notice", "warning", "error", "critical"})

# device -> (role, platform)
DEVICES: dict[str, tuple[str, str]] = {
    "r1": ("distribution", "eos"),
    "r2": ("distribution", "eos"),
    "r3": ("border", "frr"),
    "r4": ("border", "frr"),
    "r5": ("isp", "eos"),
    "s1": ("access", "eos"),
    "s2": ("access", "eos"),
    "s3": ("core", "eos"),
    "s4": ("core", "eos"),
    "r6": ("expansion", "eos"),
    "r7": ("expansion", "eos"),
    "r8": ("expansion", "eos"),
    "s5": ("expansion", "eos"),
    "dhcp-Server": ("host", "linux"),
    "webserver": ("host", "linux"),
    **{f"h{n}": ("host", "linux") for n in range(1, 8)},
}

ROLE_PLATFORMS: dict[str, frozenset[str]] = {}
for _role, _platform in DEVICES.values():
    ROLE_PLATFORMS[_role] = ROLE_PLATFORMS.get(_role, frozenset()) | {_platform}

# Never acted on and never queried: R5 is the external ISP router.
FORBIDDEN_DEVICES = frozenset({"r5"})
# Cutting these would cut the management path the system itself depends on.
PROTECTED_INTERFACES = frozenset({"management0", "management1", "ma0", "ma1", "eth0"})

# Fix rules match on cause labels plus these keys.
FIX_MATCH_KEYS = frozenset({"cause", "device", "role", "platform"})
