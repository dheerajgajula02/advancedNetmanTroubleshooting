"""What an automatic step may run and change. One gate, used by rules and by the AI alike.

Reading: one command at a time, device show commands or a short list of Linux ones.
Output is stripped of anything holding a secret before it is stored or sent anywhere.
Changing: small config changes, or a few Linux commands that each name their own undo.
Anything that could cut off management, touch accounts or the ISP session, or take
a whole protocol down is refused here, whoever asks.
"""

from __future__ import annotations

import re
import shlex

from ts import configs, lab, scrub
from ts.vocab import DEVICES, FORBIDDEN_DEVICES

MAX_CONFIG_LINES = 20
_SHELL_CHARS = re.compile(r"[;&$`<>\n\r\\]")
_EOS_PIPES = re.compile(r"\|(?!\s*(?:json|include|exclude|begin|section|no-more)\b)")
_PROTECTED_WORD = re.compile(r"^(eth0|ma\d|management\d*)$", re.I)


def _isp_addresses() -> frozenset:
    from ts.blocks.routing import ISP_ADDRESSES
    return frozenset(ISP_ADDRESSES)


def _device_problem(device: str) -> str | None:
    if device in FORBIDDEN_DEVICES:
        return f"{device} is the ISP's router, which is out of scope."
    if device not in DEVICES:
        return f"There is no device called {device!r}."
    return None


# --- reading --------------------------------------------------------------

_IP_OBJECTS = {"a", "addr", "address", "l", "link", "r", "route", "n", "neigh", "neighbor", "neighbour", "rule", "maddr"}
_READ_VERBS = {"show", "list", "ls", "lst", "get"}
_IPTABLES_OK = re.compile(r"^(-S|-L|-n|-v|-nv|-vn|-nL|-vL|-nvL|-t|--line-numbers|nat|filter|mangle|raw|[A-Z][A-Z_]+|[a-z][a-z0-9_-]*)$")
_FILES = re.compile(r"^(/proc/net/[\w/.-]+|/sys/class/net/\w+/(operstate|carrier|mtu|speed)|/etc/resolv\.conf|/etc/dnsmasq[\w./-]*)$")


def _linux_read_problem(argv: list[str]) -> str | None:
    tool, rest = argv[0], argv[1:]
    if tool == "ip":
        words = [w for w in rest if not w.startswith("-")]
        if not words or words[0] not in _IP_OBJECTS:
            return "ip may only read addresses, links, routes, neighbors and rules."
        verb = words[1] if len(words) > 1 else "show"
        return None if verb in _READ_VERBS or verb not in ("add", "del", "delete", "change", "replace", "set", "flush", "append", "save", "restore") else f"ip {words[0]} {verb} changes something."
    if tool in ("iptables", "ip6tables"):
        if "-S" not in rest and not any(w.endswith("L") and w.startswith("-") for w in rest):
            return f"{tool} may only list (-S or -L)."
        bad = [w for w in rest if not _IPTABLES_OK.match(w)]
        return f"{tool} does not allow {bad} here." if bad else None
    if tool in ("iptables-save", "ip6tables-save", "ps", "uptime", "date", "free", "ifconfig"):
        return None if tool != "ifconfig" or len(rest) <= 1 else "ifconfig may only read."
    if tool == "ss":
        return "ss may not kill sockets." if any(w in ("-K", "--kill") for w in rest) else None
    if tool == "tc":
        words = [w for w in rest if not w.startswith("-")]
        return None if len(words) >= 2 and words[0] in ("qdisc", "class", "filter") and words[1] == "show" else "tc may only show."
    if tool == "sysctl":
        return "sysctl may only read." if any(w == "-w" or "=" in w or w == "-p" for w in rest) else None
    if tool == "cat":
        return None if rest and all(_FILES.match(w) for w in rest) else "cat may only read /proc/net, port state under /sys/class/net, and the resolver and DHCP config."
    if tool == "ethtool":
        return None if len([w for w in rest if not w.startswith("-")]) == 1 and all(w in ("-S", "-i") or not w.startswith("-") for w in rest) else "ethtool may only read."
    return f"{tool} is not on the list of read-only commands."


def read_problem(command: str) -> str | None:
    """Why this command may not run, or None. Decided from the command alone."""
    command = command.strip()
    if not command:
        return "No command given."
    if _SHELL_CHARS.search(command):
        return "One command only, with no shell characters."
    if any(address in command for address in _isp_addresses()):
        return "That address belongs to the ISP's router, which is out of scope."
    if command.startswith("show "):
        if re.search(r"\btech\b", command):
            return "show tech-support is too large to run here."
        return "Only json, include, exclude, begin and section may follow a pipe." if _EOS_PIPES.search(command) else None
    if "|" in command:
        return "Pipes are only for device show commands."
    try:
        return _linux_read_problem(shlex.split(command))
    except ValueError as exc:
        return f"Could not read that command: {exc}"


def _redact(text: str) -> str:
    return "\n".join("(line held back: it holds a secret)" if configs._SECRET.search(line) else line for line in text.splitlines())


def read(device: str, command: str, keep: int = 6000) -> tuple[str, str, int]:
    """Run one read-only command. Returns (what ran, output safe to store and share, exit code)."""
    command = command.strip()
    problem = _device_problem(device) or read_problem(command)
    if problem:
        raise ValueError(problem)
    platform = lab.platform(device)
    if command.startswith("show "):
        if platform == "linux":
            raise ValueError(f"{device} is a host, so it has no show commands. Use Linux commands such as ip route show.")
        shown, out, rc = lab.eos(device, command) if platform == "eos" else lab.frr(device, command)
    else:
        if platform == "eos":
            raise ValueError("On this device use show commands.")
        shown, out, rc = lab.shell(device, shlex.split(command))
    return shown, lab._trim(scrub.text(_redact(out)), keep), rc


# --- changing -------------------------------------------------------------

_NEVER_LINE = re.compile(
    r"^\s*(?:no\s+|default\s+)?(?:reload|write\s+erase|erase\b|delete\b|format\b|copy\b|boot\b|bash\b|username\b|enable\s+(?:password|secret)|aaa\b|"
    r"management\b|zerotouch\b|snmp-server\b|logging\b|tacacs|radius|hostname\b|clear\b|ip\s+routing\b|ipv6\s+unicast-routing\b|router\s+bgp\b|neighbor\b|"
    r"interface\s+(?:management|ma\d|eth0\b))", re.I)
_TAKES_THINGS_DOWN = re.compile(r"^\s*(?:shutdown\b|no\s+router\b|no\s+interface\b|no\s+vlan\b|no\s+switchport\b|no\s+ip\s+address\b)", re.I)


def config_problem(device: str, lines: list[str]) -> str | None:
    """Why these config lines may not be applied, or None."""
    problem = _device_problem(device)
    if problem:
        return problem
    if lab.platform(device) not in ("eos", "frr"):
        return f"{device} has no router config to change."
    if not lines:
        return "No config lines given."
    if len(lines) > MAX_CONFIG_LINES:
        return f"That is {len(lines)} lines. An automatic change is at most {MAX_CONFIG_LINES}."
    return next(filter(None, map(line_problem, lines)), None)


def line_problem(line: str) -> str | None:
    if "\n" in line or "\r" in line:
        return "One config line per entry."
    if _NEVER_LINE.match(line):
        return f"{line.strip()!r} touches management, accounts, logging or the ISP session, so it is never done automatically."
    if _TAKES_THINGS_DOWN.match(line):
        return f"{line.strip()!r} can take part of the network down, so it is never done automatically."
    return None


_FORWARDING_SYSCTL = re.compile(r"^net\.ipv4\.ip_forward=[01]$|^net\.ipv6\.conf\.(all|default|eth[1-9]\d*)\.forwarding=[01]$")


def _command_shape_problem(argv: list[str]) -> str | None:
    tool, rest = argv[0], argv[1:]
    if tool == "ip":
        words = [w for w in rest if w not in ("-4", "-6")]
        if len(words) >= 2 and words[0] == "route" and words[1] in ("add", "replace", "del", "delete"):
            return None
        if len(words) >= 4 and words[0] == "link" and words[1] == "set" and (words[3] == "up" or (words[3] == "mtu" and len(words) == 5)):
            return None
        if len(words) >= 2 and words[0] in ("addr", "address") and words[1] in ("add", "del"):
            return None
        return "ip may only add, replace or delete a route, bring a port up, set an MTU, or add or remove an address."
    if tool == "tc":
        return None if len(rest) >= 3 and rest[0] == "qdisc" and rest[1] in ("add", "del", "replace") and rest[2] == "dev" else "tc may only add, replace or remove a qdisc on a named port."
    if tool in ("iptables", "ip6tables"):
        action = [w for w in rest if w in ("-A", "-I", "-D")]
        banned = [w for w in rest if w in ("-F", "-X", "-P", "-Z", "-E", "--flush", "--policy", "--delete-chain")]
        return None if len(action) == 1 and not banned else f"{tool} may only add or remove one rule at a time."
    if tool == "sysctl":
        return None if len(rest) == 2 and rest[0] == "-w" and _FORWARDING_SYSCTL.match(rest[1]) else "sysctl may only turn IP forwarding on or off."
    if tool == "dnsmasq":
        return None if not rest else "dnsmasq may only be started with no arguments."
    return f"{tool} is not on the list of commands an automatic change may run."


def command_problem(device: str, argv: list[str]) -> str | None:
    """Why this Linux-level change may not run, or None. Routers' EOS side only ever takes config lines."""
    problem = _device_problem(device)
    if problem:
        return problem
    if lab.platform(device) not in ("frr", "linux"):
        return f"{device} takes config lines, not Linux commands."
    return argv_problem(argv)


def argv_problem(argv: list[str]) -> str | None:
    if not argv:
        return "No command given."
    for word in argv:
        if _SHELL_CHARS.search(word) or "|" in word:
            return "No shell characters."
        if _PROTECTED_WORD.match(word):
            return "That is the management port, which is never changed."
        if word in _isp_addresses():
            return "That address belongs to the ISP's router, which is out of scope."
    if "flush" in argv:
        return "Flushing is never done automatically."
    return _command_shape_problem(argv)
