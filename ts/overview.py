"""What the front page says: is the network all right, what is happening, what needs a person."""

from __future__ import annotations

import time

import yaml

from ts import agent, fixer, history, lab, rag, review, status, store, sweep, tools, vocab

GROUPS = {"internet": "Internet from hosts", "bgp": "BGP sessions", "route": "Key routes", "link": "Links", "rip": "RIP", "nat": "NAT", "return-routes": "Return routes", "dhcp": "DHCP", "device": "Devices", "drift": "Config against golden"}
DAY = 86400
WEEK = 7 * DAY
LIVE_FOR, SWEEP_FOR = 40, 1500  # seconds before the watcher or the 20-minute heartbeat counts as silent


def _health(rows: list[dict], now: float) -> tuple[list[dict], list[dict]]:
    groups = {name: {"key": name, "name": label, "total": 0, "bad": 0} for name, label in GROUPS.items()}
    broken = []
    for key, known in sweep.load_state().items():
        name = key.split(":", 1)[0]
        if key.startswith("_") or name not in groups:
            continue
        groups[name]["total"] += 1
        if not known.get("ok"):
            groups[name]["bad"] += 1
            event = known.get("event") or {}
            latest = next((r for r in rows if r["device"] == event.get("device") and r["subject"] == event.get("subject") and now - r["time"] < 6 * 3600), None)
            broken.append({"key": key, "group": groups[name]["name"], "title": event.get("title") or key, "detail": event.get("detail") or "", "device": event.get("device"), "subject": event.get("subject"),
                           "run": latest and latest["id"], "state": latest and latest["state"], "by": latest and latest["by"], "headline": latest and latest["headline"],
                           "doing": latest and latest["agent_doing"], "steps": latest and latest["agent_steps"], "source": (latest or event).get("source"), "ticket": latest and latest.get("ticket")})
    return [g for g in groups.values() if g["total"]], broken


def _funnel(rows: list[dict], now: float) -> list[dict]:
    day = [r for r in rows if now - r["time"] < DAY]
    real = [r for r in day if not (r["state"] == "cleared" and r["by"] == "rules" and r["result"]["kind"] == "stop")]
    ai = [r for r in day if r["agent"] in ("working", "queued", "done", "failed")]
    return [{"step": "Detected", "count": len(day), "note": "alarms and sweep findings"},
            {"step": "Confirmed", "count": len(real), "note": f"{len(day) - len(real)} cleared on their own"},
            {"step": "Rules found the cause", "count": sum(r["result"]["kind"] == "cause" for r in day), "note": "predictable checks"},
            {"step": "Rules fixed it", "count": sum(r["state"] == "fixed" and r["by"] == "rules" for r in day), "note": "put back to known-good"},
            {"step": "AI took over", "count": len(ai), "note": f"{sum(r['state'] == 'fixed' and r['by'] == 'ai' for r in day)} fixed by the AI"},
            {"step": "Needs you", "count": sum(r["state"] in ("needs_you", "workaround") for r in day), "note": "a person has to look"}]


def _owner(key: str) -> str:
    """The device a sweep finding is about."""
    parts = key.split(":")
    return "dhcp-Server" if parts[:2] == ["dhcp", "server"] else parts[1]


def _map(rows: list[dict], moment: float, failing: set | frozenset = frozenset()) -> dict:
    """The network as a picture: every cabled link, and how each device is doing."""
    checks: dict[str, dict] = {}
    bad_ports = set()
    for key, known in sweep.load_state().items():
        if key.startswith("_") or ":" not in key:
            continue
        name, device = key.split(":", 1)[0], _owner(key)
        entry = checks.setdefault(device, {"total": 0, "bad": []})
        entry["total"] += 1
        if not known.get("ok"):
            entry["bad"].append(GROUPS.get(name, name))
            if name == "link":
                bad_ports.add((device, lab.linux_name(key.split(":")[2])))
    try:
        cabled = lab.links()
    except (OSError, yaml.YAMLError):
        cabled = ()
    placed = {d for a, _ai, b, _bi in cabled for d in (a, b)} | set(checks)
    nodes = []
    for device in sorted(placed):
        mine, runs = checks.get(device, {"total": 0, "bad": []}), [r for r in rows if r["device"] == device]
        nodes.append({"id": device, "role": vocab.DEVICES.get(device, ("", ""))[0], "total": mine["total"], "bad": mine["bad"], "working": any(r["state"] == "working" for r in runs),
                      "waiting": sum(r["state"] in ("needs_you", "workaround") and moment - r["time"] < WEEK and _still(r, failing) != "clear" for r in runs)})
    return {"nodes": nodes, "links": [{"a": a, "ai": ai, "b": b, "bi": bi, "bad": (a, ai) in bad_ports or (b, bi) in bad_ports} for a, ai, b, bi in cabled]}


def _still(row: dict, failing: set) -> str | None:
    """Is what this run was about failing now? None when the sweep does not watch it, so nobody is told it is fine on a guess."""
    if (row["device"], row.get("subject")) in failing:
        return "failing"
    return "clear" if row.get("source") == "health" else None


_memory_seen: tuple[float, dict] = (0.0, {})


def _memory_state(moment: float) -> dict:
    """How the long memory is doing. Asked every 10 seconds by every open page, so looked up once a minute: reaching the GPU machine can take seconds."""
    global _memory_seen
    if moment - _memory_seen[0] > 60:
        _memory_seen = (moment, rag.status())
    return _memory_seen[1]


def _memory_note(known: dict) -> str:
    held = f"{known['runs']} runs, {known['pieces']} pieces"
    if not known["reachable"]:
        return f"{held}. Finding by meaning is away, so it is finding by exact words alone. It catches up by itself."
    return f"{held}, {known['waiting']} still being read" if known["waiting"] else f"{held}, all searchable by meaning and by words"


def now() -> dict:
    moment, state = time.time(), status.read()
    rows = store.summaries()
    groups, broken = _health(rows, moment)
    tags_by_run = history.all_run_tags()
    failing = {(b["device"], b["subject"]) for b in broken}
    needing = [{**r, "tags": tags_by_run.get(r["id"], []), "still": _still(r, failing)} for r in rows if r["state"] in ("needs_you", "workaround") and moment - r["time"] < WEEK]
    working = [{**r, "tags": tags_by_run.get(r["id"], [])} for r in rows if r["state"] == "working"][:5]
    latest = [{**r, "tags": tags_by_run.get(r["id"], [])} for r in rows[:8]]
    sources = next((rec.get("sources") for rec in (store.load(r["id"]) for r in rows[:1]) if rec), None) or {}
    alive, swept = state.get("alive") or 0, state.get("last_sweep") or 0
    sizes, known = history.sizes(), _memory_state(moment)
    return {"time": moment, "groups": groups, "map": _map(rows, moment, failing), "broken": broken, "needs_you": needing, "working": working, "latest": latest, "funnel": _funnel(rows, moment), "review": review.counts(),
            "waiting": state.get("waiting") or [], "ai_working": state.get("ai_working"), "ai_queue": state.get("ai_queue") or 0,
            "services": [{"name": "Watcher", "ok": moment - alive < LIVE_FOR, "note": "following alarms"}, {"name": "Heartbeat", "ok": moment - swept < SWEEP_FOR, "note": "checks everything every 20 minutes"},
                         {"name": "Memory", "ok": known["reachable"], "note": _memory_note(known)},
                         *({"name": name.capitalize(), "ok": info["state"] == "ok", "note": info["state"] if info["state"] != "ok" else f"{info['found']} at last run"} for name, info in sorted(sources.items()))],
            "switches": {"repairs": not fixer.OFF_SWITCH.exists(), "ai": not agent.OFF_SWITCH.exists(), "ai_changes": not tools.AI_CHANGES_OFF.exists()},
            "memory": {"runs": sizes["runs"], "docs": sizes["docs"], **known}, "watcher_seen": alive, "swept": swept}
