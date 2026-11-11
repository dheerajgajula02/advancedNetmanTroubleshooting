"""What the metrics show for a device around an event: port changes, interface errors, CPU, and scrapes that failed."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from ts.sources import Doc, source

BASE = os.environ.get("TS_PROMETHEUS", "http://10.0.0.95:9090")
CPU_BUSY = 80


def _query(expr: str, at: float) -> list[dict]:
    url = f"{BASE}/api/v1/query?" + urllib.parse.urlencode({"query": expr, "time": at})
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)["data"]["result"]


@source("prometheus")
def metrics(start: float, end: float, device: str | None = None) -> list[Doc]:
    span = max(60, int(end - start))
    docs = []
    who = f'device="{device}"' if device else 'device!=""'
    for row in _query(f"changes(network_interface_oper_status{{{who}}}[{span}s]) > 0", end):
        m = row["metric"]
        docs.append(Doc("prometheus", end, f"{m['device']} {m['interface']}: port status changed {float(row['value'][1]):.0f} times", device=m["device"]))
    src = f'source="{device}"' if device else 'source!=""'
    for name in ("in_errors", "in_fcs_errors", "in_discards", "out_errors", "out_discards"):
        for row in _query(f"increase(interfaces_interface_state_counters_{name}{{{src}}}[{span}s]) > 0", end):
            m = row["metric"]
            docs.append(Doc("prometheus", end, f"{m['source']} {m['interface_name']}: {name.replace('_', ' ')} rose by {float(row['value'][1]):.0f}", device=m["source"]))
    for row in _query(f"increase(interfaces_interface_state_counters_carrier_transitions{{{src}}}[{span}s]) > 0", end):
        m = row["metric"]
        docs.append(Doc("prometheus", end, f"{m['source']} {m['interface_name']}: link lost and regained carrier {float(row['value'][1]):.0f} times", device=m["source"]))
    for row in _query(f"max_over_time(node_cpu_utilization_percent{{{who}}}[{span}s]) > {CPU_BUSY}", end):
        m = row["metric"]
        docs.append(Doc("prometheus", end, f"{m['device']}: CPU reached {float(row['value'][1]):.0f}%", device=m["device"]))
    if device:
        docs += _elsewhere(device, span, end) + _this_week(device, end)
    return docs


def _elsewhere(device: str, span: int, end: float) -> list[Doc]:
    """Other devices' ports changing in the same window: one device's problem or many."""
    rows = _query(f'changes(network_interface_oper_status{{device!="{device}",device!="r5"}}[{span}s]) > 0', end)
    return [Doc("prometheus", end, f"Also around then: {r['metric']['device']} {r['metric']['interface']} changed state {float(r['value'][1]):.0f} times", device=r["metric"]["device"]) for r in rows[:12]]


def _this_week(device: str, end: float) -> list[Doc]:
    """The longer memory: what the metrics saw over the week, including times no investigation ran."""
    docs = []
    for row in _query(f'changes(network_interface_oper_status{{device="{device}"}}[7d]) > 2', end):
        m = row["metric"]
        docs.append(Doc("prometheus", end, f"Over the last 7 days {m['device']} {m['interface']} changed state {float(row['value'][1]):.0f} times", device=device))
    for name in ("in_errors", "in_fcs_errors", "out_errors"):
        for row in _query(f'increase(interfaces_interface_state_counters_{name}{{source="{device}"}}[7d]) > 0', end):
            m = row["metric"]
            docs.append(Doc("prometheus", end, f"Over the last 7 days {m['source']} {m['interface_name']} counted {float(row['value'][1]):.0f} {name.replace('_', ' ')}", device=device))
    return docs
