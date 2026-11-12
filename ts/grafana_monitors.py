"""The Grafana dashboard of monitors: one row per monitor, a graph per variable with its threshold, and its state.

The graphs read what the watcher exports for Prometheus (ts_monitor_*), so they show the
monitor's own variables, exactly as the watcher read them. The dashboard is rebuilt whole
whenever a monitor is accepted, paused, resumed or retired. Panel numbers come from the
monitor's own number, so a panel embedded on the Monitors page keeps its address.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ts import monitors
from ts.sources import grafana

UID = "ts-monitors"
TITLE = "Troubleshooting monitors"
SLUG = "troubleshooting-monitors"
FOLDER_UID = "troubleshooting"
FOLDER_TITLE = "Troubleshooting"
DATASOURCE = {"type": "prometheus", "uid": "lab3-prometheus"}
STATE_TEXT = {"0": ("Quiet", "green"), "1": ("Conditions holding", "yellow"), "2": ("Started a run", "red"), "3": ("Held back: a test was running", "blue"),
              "-1": ("No data", "text"), "-2": ("Cannot read", "purple")}


def panel_ids(row: dict) -> dict:
    """Where each of a monitor's panels sits: the state strip, then one graph per variable."""
    base = int(row["num"]) * 10
    return {"state": base, **{v["name"]: base + 1 + i for i, v in enumerate(row["spec"]["variables"])}}


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(grafana.BASE + path, data=data, method=method,
                                     headers={"Authorization": grafana._authorization(), "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _thresholds(spec: dict, name: str) -> dict:
    conds = [c for c in spec["conditions"] if c["variable"] == name]
    if not conds:
        return {"mode": "absolute", "steps": [{"color": "green", "value": None}]}
    c = conds[0]
    if c["op"] in (">", ">="):
        steps = [{"color": "green", "value": None}, {"color": "red", "value": c["value"]}]
    elif c["op"] in ("<", "<="):
        steps = [{"color": "red", "value": None}, {"color": "green", "value": c["value"]}]
    else:
        steps = [{"color": "green", "value": None}, {"color": "orange", "value": c["value"]}]
    return {"mode": "absolute", "steps": steps}


def _target(expr: str, legend: str) -> dict:
    return {"refId": "A", "datasource": DATASOURCE, "expr": expr, "legendFormat": legend, "range": True}


def dashboard(rows: list[dict]) -> dict:
    panels, y = [], 0
    if not rows:
        panels.append({"id": 1, "type": "text", "title": "", "gridPos": {"h": 4, "w": 24, "x": 0, "y": 0},
                       "options": {"mode": "markdown", "content": "No monitors yet. The AI suggests them on the troubleshooting review page; each one appears here once accepted."}})
    for row in rows:
        spec, ids = row["spec"], panel_ids(row)
        paused = " (paused)" if row["status"] == "paused" else ""
        panels.append({"id": ids["state"] + 9, "type": "row", "title": f"{spec['title']}{paused}", "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []})
        y += 1
        panels.append({"id": ids["state"], "type": "state-timeline", "title": f"{spec['id']}: state", "description": monitors.describe(spec), "datasource": DATASOURCE,
                       "gridPos": {"h": 4, "w": 24, "x": 0, "y": y}, "targets": [_target(f'ts_monitor_state{{monitor="{spec["id"]}"}}', "state")],
                       "fieldConfig": {"defaults": {"color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                                                    "mappings": [{"type": "value", "options": {k: {"text": text, "color": color, "index": i} for i, (k, (text, color)) in enumerate(STATE_TEXT.items())}}]}, "overrides": []},
                       "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8, "alignValue": "left", "legend": {"showLegend": False}, "tooltip": {"mode": "single"}}})
        y += 4
        width = 24 // min(len(spec["variables"]), 3) if len(spec["variables"]) > 1 else 24
        x = 0
        for v in spec["variables"]:
            if x + width > 24:
                x, y = 0, y + 8
            panels.append({"id": ids[v["name"]], "type": "timeseries", "title": f"{v['name']}", "description": monitors.words(v), "datasource": DATASOURCE,
                           "gridPos": {"h": 8, "w": width, "x": x, "y": y},
                           "targets": [_target(f'ts_monitor_variable{{monitor="{spec["id"]}",variable="{v["name"]}"}}', v["name"])],
                           "fieldConfig": {"defaults": {"color": {"mode": "palette-classic"}, "thresholds": _thresholds(spec, v["name"]),
                                                        "custom": {"lineWidth": 2, "fillOpacity": 8, "showPoints": "never", "spanNulls": False, "thresholdsStyle": {"mode": "dashed"}}}, "overrides": []},
                           "options": {"legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "single"}}})
            x += width
        y += 8
    return {"uid": UID, "title": TITLE, "tags": ["troubleshooting", "monitors"], "timezone": "browser", "editable": False, "refresh": "30s",
            "time": {"from": "now-6h", "to": "now"}, "schemaVersion": 39, "panels": panels,
            "description": "Written by the troubleshooter (ts/grafana_monitors.py); changes made here are overwritten."}


def _folder() -> None:
    try:
        _request("GET", f"/api/folders/{FOLDER_UID}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        _request("POST", "/api/folders", {"uid": FOLDER_UID, "title": FOLDER_TITLE})


def sync() -> dict:
    """Rebuild the dashboard from the monitors that are active or paused."""
    _folder()
    rows = monitors.listing()
    saved = _request("POST", "/api/dashboards/db", {"dashboard": dashboard(rows), "folderUid": FOLDER_UID, "overwrite": True, "message": f"{len(rows)} monitors"})
    return {"url": saved.get("url"), "version": saved.get("version"), "monitors": len(rows)}


def sync_quietly() -> str:
    """The same, for a caller that must not fail because Grafana is away."""
    try:
        done = sync()
        return f"updated ({done['monitors']} monitors)"
    except Exception as exc:  # accepting or pausing a monitor still counts without its graphs
        return f"could not update: {type(exc).__name__}: {exc}"[:200]
