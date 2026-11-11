"""Comparing device configs: running against golden, or against the last one seen.

Lines holding secrets never get past clean(), so a diff shown in the UI cannot
leak a password or a hash, and password rotation never reads as a config change.
"""

from __future__ import annotations

import calendar
import json
import time
import os
import re
from pathlib import Path

from ts import lab

BASELINE_DIR = Path(os.environ.get("TS_BASELINE", Path(__file__).resolve().parent.parent / "data" / "config-baseline"))
GOLDEN_DIR = Path(os.environ.get("TS_GOLDEN", "/opt/netman/live/conf/golden/snapshots"))  # the deployed main branch

_NOISE = re.compile(r"^(!|end$|exit$|exit-address-family$|Building configuration|Current configuration|frr version|"
                    r"frr defaults|>|show running)")
_SECRET = re.compile(r"secret|password|community|\bkey\b|tacacs|radius", re.I)

_HEADER_GROUPS = {
    "router_ospf": ("router ospf",),
    "router_bgp": ("router bgp",),
    "router_rip": ("router rip",),
    "static_routes": ("ip route ", "ipv6 route "),
    "router_static": ("ip route ", "ipv6 route "),
    "router_connected": ("interface",),
}
# The parts of a config that converge back to known-good. Management access, accounts, SNMP and logging never do.
_MANAGED = ("vlan ", "ip prefix-list ", "ipv6 prefix-list ", "ip access-list ", "ipv6 access-list ", "route-map ",
            "interface ", "router ", "ip route ", "ipv6 route ")
_PROTECTED = re.compile(r"^interface\s+(management|ma\d|eth0$)", re.I)
_LINE_GROUPS = {
    "vrrp": re.compile(r"\bvrrp\b"),
    "dhcp_relay": re.compile(r"helper-address|dhcp relay"),
}


def _is_managed(header: str) -> bool:
    return header.startswith(_MANAGED) and not _PROTECTED.match(header)


def clean(text: str) -> list[str]:
    """Config lines worth comparing: no comments, banners or anything holding a secret."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not _NOISE.match(stripped) and not _SECRET.search(stripped):
            out.append(line.rstrip())
    return out


def sections(lines: list[str]) -> dict[str, list[str]]:
    """Top-level line -> the indented lines under it. Both EOS and FRR indent this way."""
    out: dict[str, list[str]] = {}
    current = None
    for line in lines:
        if line.startswith(" "):
            if current is not None:
                out[current].append(line.strip())
        else:
            current = line.strip()
            out.setdefault(current, [])
    return out


def pick(parsed: dict[str, list[str]], group: str) -> dict[str, list[str]] | None:
    """The part of a config that belongs to one named group, or None if there is no such group."""
    if group == "all":
        return parsed
    if group == "managed":
        return {h: c for h, c in parsed.items() if _is_managed(h)}
    if group in _LINE_GROUPS:
        pattern = _LINE_GROUPS[group]
        kept = {h: [c for c in children if pattern.search(c)] for h, children in parsed.items()}
        return {h: c for h, c in kept.items() if c}
    if group in _HEADER_GROUPS:
        return {h: c for h, c in parsed.items() if h.startswith(_HEADER_GROUPS[group])}
    return None


def diff(old: dict[str, list[str]], new: dict[str, list[str]]) -> dict[str, dict[str, list[str]]]:
    """Section -> {"removed": [...], "added": [...]} for every section that differs."""
    changes = {}
    for header in sorted(set(old) | set(new)):
        if header not in new:
            changes[header] = {"removed": [header, *old[header]], "added": []}
        elif header not in old:
            changes[header] = {"removed": [], "added": [header, *new[header]]}
        else:
            removed = [c for c in old[header] if c not in new[header]]
            added = [c for c in new[header] if c not in old[header]]
            if removed or added:
                changes[header] = {"removed": removed, "added": added}
    return changes


def render(changes: dict[str, dict[str, list[str]]], limit: int = 60) -> str:
    lines = []
    for header, change in changes.items():
        lines.append(header)
        lines += [f"  - {c}" for c in change["removed"]] + [f"  + {c}" for c in change["added"]]
    shown = "\n".join(lines[:limit])
    return shown if len(lines) <= limit else f"{shown}\n… ({len(lines) - limit} more lines)"


def running(device: str) -> tuple[str, list[str] | None]:
    """(what was run, cleaned running config), or None as the config if the device did not answer."""
    if lab.platform(device) == "eos":
        shown, out, rc = lab.eos(device, "show running-config")
    else:
        shown, out, rc = lab.frr(device, "show running-config")
    return shown, (clean(out) if rc == 0 else None)


def golden(device: str, root: Path = GOLDEN_DIR) -> tuple[str, list[str]] | None:
    """(snapshot name, cleaned golden config) from the newest snapshot that has this device."""
    if not root.is_dir():
        return None
    for snapshot in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        path = snapshot / f"{device}.cfg"
        if path.is_file():
            return snapshot.name, clean(path.read_text(errors="replace"))
    return None


def _snapshot_time(name: str) -> float:
    try:
        return calendar.timegm(time.strptime(name, "%Y%m%dT%H%M%SZ"))
    except ValueError:
        return 0.0


def known_good(device: str, root: Path | None = None, golden_root: Path = GOLDEN_DIR) -> tuple[str, list[str]] | None:
    """(where it came from, config) for what this device should look like.

    The golden snapshot is the truth wherever one exists and is current. A few
    devices have none, or only an old one, so a copy taken while they were healthy
    stands in for them. Whichever is newer wins, so a fresh snapshot replaces it.
    """
    gold = golden(device, golden_root)
    path = (root or BASELINE_DIR) / f"{device}.approved"
    if path.is_file() and (gold is None or _snapshot_time(gold[0]) <= path.stat().st_mtime):
        saved = time.strftime("%b %d", time.localtime(path.stat().st_mtime))
        return f"the known-good copy saved {saved}", path.read_text().splitlines()
    return (f"golden snapshot {gold[0]}", gold[1]) if gold else None


def has_known_good(device: str, root: Path | None = None) -> bool:
    return ((root or BASELINE_DIR) / f"{device}.approved").is_file()


def save_known_good(device: str, lines: list[str], root: Path | None = None) -> None:
    root = root or BASELINE_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{device}.approved").write_text("\n".join(lines) + "\n")


def baseline(device: str, root: Path | None = None) -> list[str] | None:
    path = (root or BASELINE_DIR) / f"{device}.cfg"
    return path.read_text().splitlines() if path.is_file() else None


def save_baseline(device: str, lines: list[str], root: Path | None = None) -> None:
    root = root or BASELINE_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{device}.cfg").write_text("\n".join(lines) + "\n")


# --- stop-gaps that stay until someone has done the real repair ------------

def _holds_file(root: Path | None = None) -> Path:
    return (root or BASELINE_DIR) / "holds.json"


def holds(root: Path | None = None) -> list[dict]:
    try:
        return json.loads(_holds_file(root).read_text())
    except (OSError, ValueError):
        return []


def _write_holds(items: list[dict], root: Path | None = None) -> None:
    path = _holds_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1))
    tmp.replace(path)


def _held_sections(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if re.match(r"\s*(interface|router)\b", line)]


def hold(device: str, lines: list[str], run_id: str, ticket: int | None = None, why: str = "", root: Path | None = None) -> None:
    """Keep these lines through the automatic return to known-good, until the work they belong to is closed."""
    _write_holds([*holds(root), {"device": device, "sections": _held_sections(lines), "lines": list(lines), "run": run_id, "ticket": ticket, "why": why, "since": time.time()}], root)


def release(run_id: str | None = None, ticket: int | None = None, device: str | None = None, root: Path | None = None) -> list[dict]:
    """The real repair is done, or the stop-gap came back out. The next comparison puts that part of the config back to known-good."""
    def matches(item: dict) -> bool:
        return (run_id is not None and item.get("run") == run_id and (device is None or item.get("device") == device)) or (ticket is not None and item.get("ticket") == ticket)
    items = holds(root)
    gone = [item for item in items if matches(item)]
    if gone:
        _write_holds([item for item in items if not matches(item)], root)
    return gone


def put_back(items: list[dict], root: Path | None = None) -> None:
    """Taking these out did not stick, so they are on hold again, with the reasons they had."""
    _write_holds([*holds(root), *items], root)


def _bare(line: str) -> str:
    return re.sub(r"^no\s+", "", " ".join(line.split())).lower()


def _same_thing(line: str) -> str:
    """Two lines about one destination are about the same route, whichever next hop they name."""
    text = _bare(line)
    return " ".join(text.split()[:3]) if re.match(r"ip(v6)? route ", text) else text


def touching(device: str, lines: list[str], skip_run: str = "", root: Path | None = None) -> list[dict]:
    """What is on hold from other work that a change to these lines would alter."""
    sections = {section.lower() for section in _held_sections(lines)}
    wanted = {_same_thing(line) for line in lines}

    def hits(item: dict) -> bool:
        if item["sections"]:
            return bool(sections & {section.lower() for section in item["sections"]})
        return bool(wanted & {_same_thing(line) for line in item["lines"]})

    return [item for item in holds(root) if item.get("device") == device and item.get("run") != skip_run and hits(item)]


def takes_out(lines: list[str], items: list[dict]) -> list[str]:
    """The lines of a change that would remove something on hold, as opposed to adjusting it."""
    held = {" ".join(line.split()).lower() for item in items for line in item["lines"] if not line.strip().lower().startswith("no ")}
    return [line.strip() for line in lines if line.strip().lower().startswith("no ") and any(h == _bare(line) or h.startswith(_bare(line) + " ") for h in held)]


def without_held(device: str, lines: list[str], root: Path | None = None) -> list[str]:
    """A config with what is on hold taken out, so comparing and converging both leave it alone."""
    mine = [item for item in holds(root) if item.get("device") == device]
    if not mine:
        return lines
    held = {section.lower() for item in mine for section in item["sections"]}
    loose = {re.sub(r"^no\s+", "", line.strip()).lower() for item in mine if not item["sections"] for line in item["lines"]}  # a route, say: the old line and the new one are both on hold
    out, skipping = [], False
    for line in lines:
        if not line.startswith(" "):
            skipping = line.strip().lower() in held or line.strip().lower() in loose
        if not skipping:
            out.append(line)
    return out


# --- converging back to known-good ----------------------------------------

Item = tuple  # (context line or None, config line)


def tree(lines: list[str]) -> dict[str, list[Item]]:
    """Like sections(), but a line under a nested block such as address-family remembers which block it was in."""
    rows = [(len(line) - len(line.lstrip(" ")), line.strip()) for line in lines]
    out: dict[str, list[Item]] = {}
    header, context, context_indent = None, None, 0
    for i, (indent, text) in enumerate(rows):
        if indent == 0:
            header, context = text, None
            out.setdefault(header, [])
            continue
        if header is None:
            continue
        if context is not None and indent > context_indent:
            out[header].append((context, text))
            continue
        context = None
        out[header].append((None, text))
        if i + 1 < len(rows) and rows[i + 1][0] > indent:
            context, context_indent = text, indent
    return out


def _managed_tree(lines: list[str]) -> dict[str, list[Item]]:
    return {h: items for h, items in tree(lines).items() if _is_managed(h)}


def managed_changes(known: list[str], running_lines: list[str]) -> dict[str, dict[str, list[str]]]:
    """Section -> what differs, in the parts of a config that converge. Same shape as diff(), so render() works on it."""
    want, have = _managed_tree(known), _managed_tree(running_lines)
    show = lambda item: f"{item[0]} > {item[1]}" if item[0] else item[1]
    changes = {}
    for header in sorted(set(want) | set(have)):
        if header not in have:
            changes[header] = {"removed": [header, *map(show, want[header])], "added": []}
        elif header not in want:
            changes[header] = {"removed": [], "added": [header, *map(show, have[header])]}
        else:
            removed = [show(i) for i in want[header] if i not in have[header]]
            added = [show(i) for i in have[header] if i not in want[header]]
            if removed or added:
                changes[header] = {"removed": removed, "added": added}
    return changes


def _negate(platform: str, line: str) -> str:
    if line.startswith("no "):
        return f"default {line[3:]}" if platform == "eos" else line[3:]
    return f"no {line}"


def _leave(platform: str, context: str) -> str:
    return "exit-address-family" if platform == "frr" and context.startswith("address-family") else "exit"


def _rebuild(platform: str, items: list[Item]) -> list[str]:
    """The lines that recreate a section's contents, in order."""
    out, inside = [], None
    for context, line in items:
        if context is None and inside is not None:
            out.append(_leave(platform, inside))
            inside = None
        if context is not None and context != inside:
            inside = context
        out.append(line)
    if inside is not None:
        out.append(_leave(platform, inside))
    return out


def _remove_section(platform: str, header: str, items: list[Item]) -> list[str]:
    if header.startswith("interface "):
        name = header.split(None, 1)[1]
        if platform == "frr":  # FRR cannot delete an interface; it can only forget what was set on it
            return [header, *(_negate(platform, line) for context, line in items if context is None)]
        return [f"default interface {name}" if re.match(r"(?i)ethernet\d", name) else f"no interface {name}"]
    return [f"no {header}"]


def _repair_section(platform: str, header: str, want: list[Item], have: list[Item]) -> list[str]:
    added = [i for i in have if i not in want]
    removed = [i for i in want if i not in have]
    new_blocks = {line for context, line in added if context is None and any(c == line for c, _l in have)}
    old_blocks = {line for context, line in removed if context is None and any(c == line for c, _l in want)}
    out = [header]
    for context, line in added:  # take away what should not be there first, so a changed value is not refused
        if context is None:
            out.append(_negate(platform, line))
    for context, line in removed:
        if context is None and line not in old_blocks:
            out.append(line)
    for block in old_blocks:
        out += [block, *(line for context, line in want if context == block), _leave(platform, block)]
    for block in dict.fromkeys(context for context, _line in added + removed if context and context not in new_blocks | old_blocks):
        out.append(block)
        out += [_negate(platform, line) for context, line in added if context == block]
        out += [line for context, line in removed if context == block]
        out.append(_leave(platform, block))
    return out


def repair_lines(platform: str, known: list[str], running_lines: list[str]) -> list[str]:
    """The config lines that take a device from what it is running back to its known-good config."""
    want, have = _managed_tree(known), _managed_tree(running_lines)
    def order(header: str) -> tuple[int, str]:  # things that get referred to go in before what refers to them
        return next(i for i, prefix in enumerate(_MANAGED) if header.startswith(prefix)), header
    out: list[str] = []
    for header in sorted(set(want) | set(have), key=order):
        if header not in have:
            out += [header, *_rebuild(platform, want[header])]
        elif header not in want:
            out += _remove_section(platform, header, have[header])
        elif want[header] != have[header] and set(want[header]) != set(have[header]):
            out += _repair_section(platform, header, want[header], have[header])
    return out
