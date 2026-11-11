"""Incident records on disk: data/incidents/<id>.json, one file per incident."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("TS_DATA", Path(__file__).resolve().parent.parent / "data" / "incidents"))


def save(record: dict, root: Path = DATA_DIR) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{record['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1, default=str))
    tmp.replace(path)
    return path


def exists(incident_id: str, root: Path = DATA_DIR) -> bool:
    return (root / f"{incident_id}.json").exists()


def load(incident_id: str, root: Path = DATA_DIR) -> dict | None:
    path = root / f"{incident_id}.json"
    if not path.is_file() or path.parent.resolve() != root.resolve():
        return None
    return json.loads(path.read_text())


def delete(incident_id: str, root: Path = DATA_DIR) -> bool:
    """Remove one stored record. False if there is no such record."""
    path = root / f"{incident_id}.json"
    if not path.is_file() or path.parent.resolve() != root.resolve():
        return False
    path.unlink()
    return True


QUIET_LABELS = {"config.changed_by_automation", "ospf.link_down"}  # automation's own work, and runs that only point at another run
_cache: dict[Path, tuple[int, dict]] = {}


def verdict(rec: dict) -> tuple[str, str]:
    """(where this run stands, who dealt with it). The one thing a person needs from a row."""
    r, fix, ai = rec["result"], rec.get("repair") or {}, rec.get("agent") or {}
    status = fix.get("status")
    if ai.get("status") in ("queued", "working"):
        return "working", "ai"
    if ai.get("status") == "done" and ai.get("outcome") == "fixed":
        return "fixed", "ai"
    if ai.get("status") == "done" and ai.get("outcome") == "workaround":
        return ("handled" if rec.get("handled") else "workaround"), "ai"
    if ai.get("status") == "not_needed" or (ai.get("status") == "done" and ai.get("outcome") == "nothing_wrong"):
        return "cleared", "ai" if ai.get("status") == "done" else "rules"
    if status == "fixed":
        return "fixed", "rules"
    if status == "already":
        return "cleared", "rules"
    if status == "held":
        return "held", "rules"
    if status == "would_fix":
        return "watching", "rules"
    if r["kind"] == "stop" and not ai:
        step = r.get("step")
        cleared = isinstance(step, int) and rec["steps"][step - 1].get("phase") == "confirm"
        return ("cleared" if cleared else "info"), "rules"
    if r.get("label") in QUIET_LABELS and not ai:
        return "info", "rules"
    return "handled" if rec.get("handled") else "needs_you", "ai" if ai.get("status") in ("done", "failed") else "rules"


def set_handled(incident_id: str, handled: bool, by: str = "operator", root: Path = DATA_DIR) -> str:
    """A person has dealt with a run that needed them, or takes that back. Says where the run stands now."""
    record = load(incident_id, root)
    if record is None:
        raise ValueError("There is no such run.")
    state = verdict(record)[0]
    if state not in ("needs_you", "handled", "workaround"):
        raise ValueError(f"Only a run that needs you can be marked handled. This one is {state.replace('_', ' ')}.")
    if handled:
        record["handled"] = {"at": time.time(), "by": by}
    else:
        record.pop("handled", None)
    save(record, root)
    return verdict(record)[0]


def _doing(ai: dict) -> dict | None:
    """The AI's latest step, short enough to show in a list."""
    if ai.get("status") != "working" or not ai.get("calls"):
        return None
    last, shown = ai["calls"][-1], ("device", "command", "text", "title", "name", "area", "tag")
    return {"tool": last.get("tool", ""), "args": {k: str(v)[:120] for k, v in (last.get("args") or {}).items() if k in shown}}


def _summary(rec: dict) -> dict:
    ev, ai = rec["event"], rec.get("agent") or {}
    state, by = verdict(rec)
    return {"id": rec["id"], "device": ev["device"], "interface": ev.get("interface"), "source": ev.get("source"), "ticket": ev.get("ticket"),
            "subject": ev.get("subject") or ev.get("interface"), "kind": ev.get("event", "interface.down"),
            "title": ev.get("title") or f"{ev['device']} {ev.get('interface')} went down",
            "description": ev.get("description", ""), "time": ev["time"], "finished": rec.get("finished"),
            "result": {k: rec["result"].get(k) for k in ("kind", "label", "text", "step")},
            "steps": len(rec["steps"]), "rule": rec["rule"]["id"], "repair": (rec.get("repair") or {}).get("status"), "ai": bool(rec.get("analysis")),
            "agent": ai.get("status"), "agent_outcome": ai.get("outcome"), "agent_steps": len(ai.get("calls") or []), "agent_changes": len(ai.get("changes") or []), "agent_proposals": len(ai.get("proposals") or []), "agent_doing": _doing(ai), "source": ev.get("source"), "handled": (rec.get("handled") or {}).get("at"),
            "state": state, "by": by, "headline": ai.get("root_cause") or rec["result"].get("text") or rec["result"].get("label") or rec["result"]["kind"],
            "took": max(0.0, (ai.get("finished") or rec.get("finished") or ev["time"]) - ev["time"]), "emailed": bool((rec.get("email") or {}).get("sent")), "pattern": (rec.get("pattern") or {}).get("verdict")}


def summaries(root: Path = DATA_DIR) -> list[dict]:
    """One row per run, newest first. A file that has not changed is not read again."""
    out = []
    for path in root.glob("*.json"):
        try:
            stamp = path.stat().st_mtime_ns
            cached = _cache.get(path)
            if cached and cached[0] == stamp:
                out.append(cached[1])
                continue
            row = _summary(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            continue
        _cache[path] = (stamp, row)
        out.append(row)
    return sorted(out, key=lambda s: s["time"], reverse=True)


SORTS = {"time": lambda s: s["time"], "device": lambda s: s["device"], "title": lambda s: s["title"].lower(), "state": lambda s: s["state"], "took": lambda s: s["took"], "by": lambda s: s["by"]}


def _keep(row: dict, tags: list[str], want: dict, skip: str = "") -> bool:
    if want.get("state") and skip != "state" and row["state"] != want["state"]:
        return False
    if want.get("device") and skip != "device" and row["device"] != want["device"]:
        return False
    if want.get("by") and skip != "by" and row["by"] != want["by"]:
        return False
    if want.get("tag") and skip != "tag" and not any(t == want["tag"] or t.split("/", 1)[0] == want["tag"] for t in tags):
        return False
    if want.get("since") and row["time"] < float(want["since"]):
        return False
    text = (want.get("q") or "").lower()
    ticket = f"ticket t-{row['ticket']}" if row.get("ticket") else ""
    return not text or text in f"{row['title']} {row['headline']} {row['device']} {row['id']} {ticket} {' '.join(tags)}".lower()


def page(want: dict, tags_by_run: dict[str, list[str]], root: Path = DATA_DIR) -> dict:
    """One page of runs, filtered and sorted, with how many runs each filter choice would show."""
    rows = summaries(root)
    size = min(max(int(want.get("size") or 25), 5), 100)
    number = max(int(want.get("page") or 1), 1)
    tag_of = lambda r: tags_by_run.get(r["id"], [])  # noqa: E731
    kept = [r for r in rows if _keep(r, tag_of(r), want)]
    key = SORTS.get(want.get("sort") or "time", SORTS["time"])
    kept.sort(key=key, reverse=(want.get("dir") or "desc") != "asc")
    pages = max(1, (len(kept) + size - 1) // size)
    number = min(number, pages)
    facets = {"state": {}, "device": {}, "by": {}, "tag": {}}
    for name in ("state", "device", "by"):
        for r in rows:
            if _keep(r, tag_of(r), want, name):
                facets[name][r[name]] = facets[name].get(r[name], 0) + 1
    for r in rows:
        if _keep(r, tag_of(r), want, "tag"):
            for tag in tag_of(r):
                facets["tag"][tag] = facets["tag"].get(tag, 0) + 1
    return {"total": len(kept), "all": len(rows), "page": number, "pages": pages, "size": size, "facets": facets,
            "items": [{**r, "tags": tag_of(r)} for r in kept[(number - 1) * size:number * size]]}
