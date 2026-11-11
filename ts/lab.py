"""Access to the network: device commands, Prometheus and the cabling inventory.

Every helper returns what it ran next to what came back, so each step in an
incident can show its evidence. Only configure() and run_change() change
anything, and both refuse to run unless this process holds the change lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import urllib.parse
import uuid
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from ts.vocab import DEVICES, FORBIDDEN_DEVICES

LAB_DIR = Path(os.environ.get("TS_LAB_DIR", Path.home() / "advancedNetman" / "lab1"))
TOPOLOGY = LAB_DIR / "lab1.clab.yml"
PROMETHEUS = os.environ.get("TS_PROMETHEUS", "http://10.0.0.95:9090")
CONTAINER_PREFIX = "clab-lab1-"
CHANGE_LOCK = Path("/tmp/advancedNetman-network-change.lock")  # held by health recovery, deploys and rotation


@dataclass
class Evidence:
    """One thing a check ran and what came back."""
    kind: str                      # "device" | "prometheus" | "file" | "docker"
    ran: str                       # the exact command or query
    output: str = ""               # raw output, trimmed
    series: list | None = None     # [[epoch, value], ...] for Prometheus range queries
    window: list | None = None     # [start, end] the query covered
    note: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "", [])}


@dataclass
class Result:
    outcome: str
    saw: str                                  # one plain sentence: what the check observed
    values: dict = field(default_factory=dict)  # produced values for later steps
    evidence: list[Evidence] = field(default_factory=list)


def _trim(text: str, limit: int = 6000) -> str:
    text = text.strip("\n")
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters)"


def platform(device: str) -> str:
    return DEVICES.get(device, ("", "unknown"))[1]


def container(device: str) -> str:
    if device in FORBIDDEN_DEVICES:
        raise ValueError(f"{device} is the ISP router; nothing here reaches it")
    return CONTAINER_PREFIX + device


def docker(args: list[str], timeout: int = 20) -> tuple[int, str]:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr)


def eos(device: str, command: str, timeout: int = 20) -> tuple[str, str, int]:
    """Run one read-only EOS show or ping command on the device. Returns (shown command, output, rc)."""
    if not command.startswith(("show ", "ping ")):
        raise ValueError("only show and ping commands are allowed")
    rc, out = docker(["exec", container(device), "Cli", "-p", "15", "-c", command], timeout=timeout)
    return f"{device}# {command}", out, rc


def frr(device: str, command: str) -> tuple[str, str, int]:
    """Run one read-only vtysh show command on an FRR router."""
    if not command.startswith("show "):
        raise ValueError("only show commands are allowed")
    rc, out = docker(["exec", container(device), "vtysh", "-c", command])
    return f"{device}# {command}", out, rc


def show_json(device: str, eos_command: str | None, frr_command: str | None, keep: int = 2500) -> tuple[Evidence, dict | None]:
    """Run the right show command for the device's platform. Returns (evidence, parsed JSON or None)."""
    if platform(device) == "eos":
        shown, out, rc = eos(device, f"{eos_command} | json")
    else:
        shown, out, rc = frr(device, frr_command)
    ev = Evidence("device", shown, output=_trim(out, keep))
    try:
        data = json.loads(out) if rc == 0 else None
    except json.JSONDecodeError:
        data = None
    return ev, data


def change_running(path: Path = CHANGE_LOCK, locks: Path = Path("/proc/locks")) -> bool:
    """Whether a job holds the network change lock. Reads /proc/locks, so it never takes the lock itself."""
    try:
        inode = path.stat().st_ino
        table = locks.read_text()
    except OSError:
        return False
    for line in table.splitlines():
        parts = line.replace("->", "").split()
        if len(parts) >= 6 and parts[1] == "FLOCK" and parts[5].endswith(f":{inode}"):
            return True
    return False


def console_probe(device: str) -> tuple[str, str, int]:
    """The lightest command that proves the device is alive and answering."""
    if platform(device) == "eos":
        return eos(device, "show clock")
    if platform(device) == "frr":
        return frr(device, "show version")
    return shell(device, ["date"])


def shell(device: str, argv: list[str], timeout: int = 20) -> tuple[str, str, int]:
    """Run a read-only command (ip link show and the like) on the device."""
    rc, out = docker(["exec", container(device), *argv], timeout=timeout)
    return f"{device}$ {' '.join(argv)}", out, rc


# --- changing the network -------------------------------------------------

class LockBusy(RuntimeError):
    """Another job is changing the network."""


_lock_held = False
_REFUSED_LINE = re.compile(r"^\s*(no\s+|default\s+)?(interface\s+(management|ma\d|eth0\b)|username|aaa|snmp-server|tacacs|logging|management\s)", re.I)


@contextmanager
def change_lock(path: Path = CHANGE_LOCK):
    """Hold the lock every job takes before changing the network. Raises LockBusy rather than waiting."""
    global _lock_held
    with path.open("r") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy("another job holds the network change lock") from exc
        _lock_held = True
        try:
            yield
        finally:
            _lock_held = False


def _need_lock() -> None:
    if not _lock_held:
        raise RuntimeError("the network can only be changed while holding the change lock")


def configure(device: str, lines: list[str]) -> tuple[bool, str]:
    """Apply config lines and save them. EOS validates the whole change first. Returns (worked, what came back)."""
    _need_lock()
    bad = [line for line in lines if _REFUSED_LINE.match(line)]
    if bad:
        return False, f"refused, these lines touch management, accounts or logging: {bad}"
    if platform(device) == "eos":
        return _configure_eos(device, lines)
    if platform(device) == "frr":
        return _configure_frr(device, lines)
    return False, f"{device} is not a device whose config can be changed this way"


def _eos_session(device: str, lines: list[str], last: str) -> tuple[bool, str]:
    script = "\n".join([f"configure session ts-{uuid.uuid4().hex[:8]}", *lines, last])
    rc, out = docker(["exec", container(device), "Cli", "-p", "15", "-c", script], timeout=60)
    return rc == 0 and "% " not in out, out


def _configure_eos(device: str, lines: list[str]) -> tuple[bool, str]:
    worked, out = _eos_session(device, lines, "abort")  # a dry run: the device rejects bad lines and nothing is applied
    if not worked:
        return False, f"the device rejected the change, so nothing was applied:\n{out}"
    worked, out = _eos_session(device, lines, "commit")
    if not worked:
        return False, out
    rc, saved = docker(["exec", container(device), "Cli", "-p", "15", "-c", "copy running-config startup-config"], timeout=60)
    return True, out + ("" if rc == 0 else f"\nApplied, but saving it failed: {saved}")


def _configure_frr(device: str, lines: list[str]) -> tuple[bool, str]:
    args = ["exec", container(device), "vtysh", "-c", "configure terminal"]
    for line in lines:
        args += ["-c", line]
    rc, out = docker(args, timeout=60)
    if rc != 0 or "% " in out:
        return False, out
    rc, saved = docker(["exec", container(device), "vtysh", "-c", "write memory"], timeout=60)
    return True, out + ("" if rc == 0 else f"\nApplied, but saving it failed: {saved}")


def run_change(device: str, argv: list[str]) -> tuple[str, str, int]:
    """Run a command that changes something on a device. The fixer's way of restarting a service or restoring a rule."""
    _need_lock()
    return shell(device, argv)


def linux_name(interface: str) -> str:
    """EthernetN on cEOS is ethN inside the container; FRR and hosts already use ethN."""
    m = re.fullmatch(r"(?:Ethernet|Et)(\d+)", interface)
    return f"eth{m.group(1)}" if m else interface


def device_name(device: str, linux_iface: str) -> str:
    """The name the device itself uses for a container port."""
    m = re.fullmatch(r"eth(\d+)", linux_iface)
    if m and platform(device) == "eos":
        return f"Ethernet{m.group(1)}"
    return linux_iface


# --- Prometheus -----------------------------------------------------------

def prom_range(query: str, start: float, end: float, step: int = 15) -> tuple[Evidence, list]:
    params = urllib.parse.urlencode({"query": query, "start": f"{start:.0f}", "end": f"{end:.0f}", "step": step})
    url = f"{PROMETHEUS}/api/v1/query_range?{params}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.load(resp)
    result = data.get("data", {}).get("result", [])
    series = [[float(t), float(v)] for t, v in result[0]["values"]] if result else []
    ev = Evidence("prometheus", query, series=series, window=[start, end],
                  output=f"{len(series)} samples every {step}s" if series else "no samples")
    return ev, series


# --- topology -------------------------------------------------------------

@lru_cache(maxsize=1)
def _nodes() -> dict:
    return yaml.safe_load(TOPOLOGY.read_text()).get("topology", {}).get("nodes", {})


def mgmt_address(device: str) -> str | None:
    """The device's management address, or None for devices managed only over their console."""
    return (_nodes().get(device) or {}).get("mgmt-ipv4")


@lru_cache(maxsize=1)
def _links() -> tuple[tuple[str, str, str, str], ...]:
    data = yaml.safe_load(TOPOLOGY.read_text())
    links = []
    for link in data.get("topology", {}).get("links", []):
        ends = link.get("endpoints", [])
        if len(ends) == 2:
            (a, ai), (b, bi) = (e.split(":", 1) for e in ends)
            links.append((a, ai, b, bi))
    return tuple(links)


def links() -> tuple[tuple[str, str, str, str], ...]:
    """Every cabled link in the topology file, as (device, port, device, port)."""
    return _links()


def topology_peer(device: str, interface: str) -> tuple[str, str, str] | None:
    """(peer device, peer interface as the peer names it, the matching topology line) or None."""
    port = linux_name(interface)
    for a, ai, b, bi in _links():
        if (a, ai) == (device, port):
            return b, device_name(b, bi), f"{a} {device_name(a, ai)} <-> {b} {device_name(b, bi)}"
        if (b, bi) == (device, port):
            return a, device_name(a, ai), f"{a} {device_name(a, ai)} <-> {b} {device_name(b, bi)}"
    return None
