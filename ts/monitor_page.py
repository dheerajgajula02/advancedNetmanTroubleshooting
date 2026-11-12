"""What the Monitors page and the review page show about monitors. Read here, decided in ts/monitors.py."""

from __future__ import annotations

import time

from ts import grafana_monitors, monitor_ai, monitors, review

ALIVE_WITHIN = 3 * monitors.EVERY


def _view(row: dict) -> dict:
    spec = row["spec"]
    return {**row, "words": monitors.describe(spec), "conditions_words": monitors.condition_words(spec),
            "variables_words": {v["name"]: monitors.words(v) for v in spec["variables"]},
            "panels": grafana_monitors.panel_ids(row), "events": monitors.events(spec["id"], 12)}


def page() -> dict:
    rows = monitors.listing()
    last = max((r.get("last_eval") or 0 for r in rows if r["status"] == "active"), default=0)
    return {"monitors": [_view(r) for r in rows], "now": time.time(),
            "watching": bool(last and time.time() - last < ALIVE_WITHIN) if any(r["status"] == "active" for r in rows) else None,
            "grafana": {"uid": grafana_monitors.UID, "slug": grafana_monitors.SLUG, "title": grafana_monitors.TITLE},
            "waiting": review.counts()["waiting_by_kind"].get("monitor", 0),
            "sessions": [{k: s[k] for k in ("run_id", "time", "finished", "model", "status", "summary", "proposals", "error")} for s in monitor_ai.sessions(10)],
            "recent": monitors.events(None, 25)}


def _step(span: float) -> int:
    return max(60, int(span / 360) // 60 * 60)


def _episodes(monitor: str, start: float, end: float) -> list[dict]:
    """When a live monitor started a run or was held back, from what it noted, for shading its graphs."""
    out, open_ = [], None
    for e in sorted(monitors.events(monitor, 500), key=lambda e: e["time"]):
        if e["kind"] in ("fired", "held_back") and open_ is None:
            open_ = {"began": e["time"], "fired_at": e["time"], "until": None, "held_back": e["kind"] == "held_back", "jobs": []}
        elif e["kind"] in ("cleared", "paused", "retired") and open_ is not None:
            open_["until"] = e["time"]
            out.append(open_)
            open_ = None
    if open_ is not None:
        out.append({**open_, "until": end})
    return [x for x in out if x["until"] >= start and x["began"] <= end]


def preview(query: dict) -> dict:
    """Each variable's values over a stretch of time, for a small graph: around the incident for a suggestion, the past hours for a monitor."""
    now = time.time()
    if query.get("proposal"):
        found = review.get(int(query["proposal"]))
        if not found or found["kind"] != "monitor":
            raise KeyError("no such monitor suggestion")
        spec, at = found["body"]["monitor"], (found["proof"] or {}).get("incident_at")
        start, end = (at - 6 * 3600, min(now, at + 2 * 3600)) if at else (now - 24 * 3600, now)
        episodes = [e for e in (found["proof"] or {}).get("episodes") or [] if e["until"] >= start and e["began"] <= end]
    else:
        row = monitors.get(query.get("monitor") or "")
        if not row:
            raise KeyError("no such monitor")
        spec, at = row["spec"], None
        hours = min(168.0, max(1.0, float(query.get("hours") or 24)))
        start, end = now - hours * 3600, now
        episodes = _episodes(spec["id"], start, end)
    step = _step(end - start)
    series = {}
    for v in spec["variables"]:
        times, values = monitors.history_of(v, start, end, step)
        series[v["name"]] = {"words": monitors.words(v), "times": times, "values": values,
                             "thresholds": [{"op": c["op"], "value": c["value"]} for c in spec["conditions"] if c["variable"] == v["name"]]}
    return {"from": start, "to": end, "step": step, "incident_at": at, "episodes": episodes, "series": series}


def set_status(monitor: str, status: str) -> dict:
    row = monitors.set_status(monitor, status, "operator")
    return {"status": row["status"], "grafana": grafana_monitors.sync_quietly()}
