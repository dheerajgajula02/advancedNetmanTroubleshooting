"""Monitors: numbers the network already reports, with conditions that start a troubleshooting run.

A variable is one number read from what is already collected: a Prometheus metric for one
device (and port), passed through a fixed function such as "increase over 10 minutes", or a
count of traps or syslog lines from one device in the data lake. A monitor names one to five
variables and a condition on each, combined with all or any. When the combination has held
for hold_seconds, the watcher starts a run on the monitor's device, the same way a trap does.

Nothing here is free text that a model could get wrong. A variable is built from named
parts, checked against what Prometheus and the data lake really hold, and turned into a query
by this module. A monitor is replayed over the past week (backtest) before anyone sees it,
and runs only once a person has accepted it on the review page.

While a job that changes or tests the network is running (the nightly test, a change
validation, anything holding the change lock), a monitor whose conditions hold is held back
rather than fired, and stays held back until its conditions clear: a fault a test causes on
purpose never starts a run.

Accepted monitors are exported for Prometheus on /metrics as ts_monitor_* (serve_metrics), and
ts/grafana_monitors.py keeps a dashboard with one row per monitor.
"""

from __future__ import annotations

import bisect
import calendar
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ts import history
from ts.vocab import DEVICES, FORBIDDEN_DEVICES

PROMETHEUS = os.environ.get("TS_PROMETHEUS", "http://10.0.0.95:9090")
DATALAKE = os.environ.get("TS_DATALAKE", "10.0.0.148")
METRICS_AT = os.environ.get("TS_MONITOR_METRICS", "10.0.0.10:9810")
EVERY = 30                 # seconds between looks at every active monitor
HELD_BACK_GRACE = 300      # a test's effects outlast it by a few minutes (a 10-minute window still counts its flaps)
BACKTEST_DAYS = 7          # what Prometheus keeps
STEP = 60
MAX_VARIABLES = 5
MAX_CONDITIONS = 5
STATUSES = ("active", "paused", "retired")
STATE_CODES = {"ok": 0, "holding": 1, "fired": 2, "held_back": 3, "no_data": -1, "error": -2}

ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,47}$")
NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
PORT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._:-]{0,39}$")
DURATION = re.compile(r"^(\d{1,4})([smh])$")
OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
       "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
OP_WORDS = {">": "is above", ">=": "is at least", "<": "is below", "<=": "is at most", "==": "equals", "!=": "is not"}


class Invalid(ValueError):
    """A variable or monitor that cannot be built. The message says exactly what to change."""


# --- what is collected ------------------------------------------------------


@dataclass(frozen=True)
class Family:
    pattern: re.Pattern
    means: str           # {0}, {1} ... are the pattern's groups
    device_label: str
    port_label: str | None
    kind: str            # counter (only goes up) or gauge


FAMILIES = (
    Family(re.compile(r"^interfaces_interface_state_counters_(carrier_transitions)$"), "how many times the port lost and regained its link (carrier transitions), from streaming telemetry", "source", "interface_name", "counter"),
    Family(re.compile(r"^interfaces_interface_state_counters_(in|out)_(errors|fcs_errors|discards)$"), "{0}bound {1} counted on the port, from streaming telemetry", "source", "interface_name", "counter"),
    Family(re.compile(r"^interfaces_interface_state_counters_(in|out)_(octets|pkts|unicast_pkts|multicast_pkts|broadcast_pkts)$"), "{0}bound {1} counted on the port, from streaming telemetry", "source", "interface_name", "counter"),
    Family(re.compile(r"^eth(\d)_(in|out)_(octets|pkts|errors)$"), "{1}bound {2} on the Linux router's port eth{0}, from streaming telemetry", "source", None, "counter"),
    Family(re.compile(r"^network_interface_oper_status$"), "the port's state from SNMP: 1 up, 2 down", "device", "interface", "gauge"),
    Family(re.compile(r"^network_interface_(receive|transmit)_bytes_total$"), "bytes {0}d on the port, from SNMP", "device", "interface", "counter"),
    Family(re.compile(r"^node_cpu_utilization_percent$"), "CPU use in percent, from SNMP", "device", None, "gauge"),
    Family(re.compile(r"^node_system_uptime_seconds$"), "seconds since the device last started, from SNMP", "device", None, "gauge"),
    Family(re.compile(r"^device_snmp_up$"), "whether the device answers SNMP: 1 yes, 0 no", "device", None, "gauge"),
)

FUNCTIONS = {
    "now": "its latest value",
    "increase": "how much it rose over the window",
    "rate": "how fast it rose over the window, per second",
    "changes": "how many times its value changed over the window",
    "delta": "how much it changed over the window, up or down",
    "max_over": "its highest value over the window",
    "min_over": "its lowest value over the window",
    "avg_over": "its average over the window",
}
COMBINE = {"max": "the highest of", "min": "the lowest of", "sum": "the total of", "avg": "the average of"}

# Traps by name: the OID the trap log carries after "OID: iso." (the leading 1 is written iso).
TRAPS = {"linkDown": "3.6.1.6.3.1.1.5.3", "linkUp": "3.6.1.6.3.1.1.5.4", "coldStart": "3.6.1.6.3.1.1.5.1",
         "ospfNbrStateChange": "3.6.1.2.1.14.16.2.2", "vrrpNewMaster": "3.6.1.2.1.68.0.1",
         "entityStateChange": "3.6.1.2.1.131.0.1", "configSaved": "3.6.1.4.1.30065.3.9.3.0.1", "agentStart": "3.6.1.4.1.8072.4.0.1"}
TRAP_WORDS = {"linkDown": "a port went down", "linkUp": "a port came up", "coldStart": "the device restarted",
              "ospfNbrStateChange": "an OSPF neighbor changed state", "vrrpNewMaster": "it became the VRRP gateway",
              "entityStateChange": "a hardware part changed state", "configSaved": "its config was saved", "agentStart": "its SNMP agent restarted"}


def family(metric: str) -> tuple[Family, tuple] | None:
    for fam in FAMILIES:
        m = fam.pattern.match(metric or "")
        if m:
            return fam, m.groups()
    return None


def seconds(duration: str) -> int:
    m = DURATION.match(str(duration or ""))
    if not m:
        raise Invalid(f"{duration!r} is not a duration. Write it like 90s, 10m or 2h.")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]


# --- reading Prometheus and the data lake (tests replace these) -------------


def _prom(path: str, params: dict, timeout: int = 20) -> dict:
    url = f"{PROMETHEUS}{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = json.load(response)
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus said: {body.get('error') or body}")
    return body["data"]


def prom_query(expr: str, at: float | None = None) -> list[dict]:
    return _prom("/api/v1/query", {"query": expr, **({"time": at} if at else {})})["result"]


def prom_range(expr: str, start: float, end: float, step: int) -> list[dict]:
    return _prom("/api/v1/query_range", {"query": expr, "start": start, "end": end, "step": step}, timeout=60)["result"]


def _lake():
    import pymongo
    return pymongo.MongoClient(DATALAKE, 27017, serverSelectionTimeoutMS=4000, socketTimeoutMS=20000)["datalake"]


def _days(start: float, end: float) -> list[str]:
    """The data lake keeps one collection per UTC day."""
    days, t = [], start
    while True:
        day = time.strftime("%Y_%m_%d", time.gmtime(t))
        if day not in days:
            days.append(day)
        if t >= end:
            return days
        t = min(end, t + 86400)


def _event_filter(v: dict, start: float, end: float) -> dict:
    parts = [{"raw_payload": {"$regex": rf"4\.1\.0 = OID: iso\.{re.escape(TRAPS[v['trap']])}(\t|$)"}}] if v.get("trap") else []
    if v.get("text"):
        parts.append({"raw_payload": {"$regex": re.escape(v["text"]), "$options": "i"}})
    query = {"timestamp": {"$gt": start, "$lte": end}, "source_device": v["device"], "protocol": "snmp_trap" if v["protocol"] == "trap" else "syslog"}
    if parts:
        query["$and"] = parts
    return query


def event_times(v: dict, start: float, end: float, limit: int = 50000) -> tuple[list[float], float | None]:
    """When matching events happened, oldest first, and the earliest moment the data lake has records for (None if none)."""
    db = _lake()
    have = set(db.list_collection_names())
    days = [d for d in _days(start, end) if d in have]
    stamps: list[float] = []
    for day in days:
        stamps += [float(d["timestamp"]) for d in db[day].find(_event_filter(v, start, end), {"timestamp": 1, "_id": 0}).limit(limit)]
    covered = None
    if days:
        covered = max(start, calendar.timegm(time.strptime(days[0], "%Y_%m_%d")))
    return sorted(stamps), covered


def event_count(v: dict, at: float, since: float | None = None) -> int:
    start = max(at - seconds(v["window"]), since or 0.0)
    db = _lake()
    have = set(db.list_collection_names())
    return sum(db[day].count_documents(_event_filter(v, start, at)) for day in _days(start, at) if day in have)


# --- variables ----------------------------------------------------------------


def _choice(value, allowed, what: str) -> str:
    if value not in allowed:
        raise Invalid(f"{what} must be one of {', '.join(allowed)}, not {value!r}.")
    return value


def clean_variable(v: dict) -> dict:
    """The variable in its stored form, or Invalid saying what is wrong. Does not ask Prometheus whether it exists (check_exists does)."""
    if not isinstance(v, dict):
        raise Invalid("A variable is an object with name, source and the fields for that source.")
    name = str(v.get("name") or "")
    if not NAME.match(name):
        raise Invalid(f"Variable name {name!r} must be lowercase letters, digits and underscores, starting with a letter, 2 to 31 characters.")
    device = str(v.get("device") or "")
    if device not in DEVICES or device in FORBIDDEN_DEVICES:
        raise Invalid(f"{name}: device {device!r} is not one of this network's devices." + (" r5 is the ISP's router and out of scope." if device in FORBIDDEN_DEVICES else ""))
    source = _choice(v.get("source"), ("metric", "events"), f"{name}: source")
    if source == "metric":
        metric = str(v.get("metric") or "")
        found = family(metric)
        if not found:
            raise Invalid(f"{name}: {metric!r} is not a metric monitors can use. list_variables shows the ones that are.")
        fam, _groups = found
        out = {"name": name, "source": "metric", "device": device, "metric": metric}
        port = v.get("port")
        if port not in (None, ""):
            if not fam.port_label:
                raise Invalid(f"{name}: {metric} has no ports, so leave port out.")
            if not PORT.match(str(port)):
                raise Invalid(f"{name}: {port!r} is not a port name.")
            out["port"] = str(port)
        out["function"] = _choice(v.get("function") or "now", tuple(FUNCTIONS), f"{name}: function")
        if out["function"] != "now":
            window = seconds(v.get("window"))
            if not 60 <= window <= 86400:
                raise Invalid(f"{name}: the window must be between 1m and 24h.")
            out["window"] = str(v["window"])
        elif v.get("window"):
            raise Invalid(f"{name}: function now takes no window. Use increase, changes, max_over and the like for a window.")
        if fam.kind == "counter" and out["function"] in ("now", "max_over", "min_over", "avg_over"):
            raise Invalid(f"{name}: {metric} only ever goes up, so its value means nothing by itself. Use increase, rate or delta over a window.")
        out["combine"] = _choice(v.get("combine") or "max", tuple(COMBINE), f"{name}: combine")
        return out
    protocol = _choice(v.get("protocol"), ("trap", "syslog"), f"{name}: protocol")
    out = {"name": name, "source": "events", "device": device, "protocol": protocol}
    if protocol == "trap":
        out["trap"] = _choice(v.get("trap"), tuple(TRAPS), f"{name}: trap")
    text = str(v.get("text") or "").strip()
    if text:
        if not 2 <= len(text) <= 80:
            raise Invalid(f"{name}: text must be 2 to 80 characters.")
        out["text"] = text
    elif protocol == "syslog":
        raise Invalid(f"{name}: a syslog variable needs text: the words a matching line contains, as list_variables shows them.")
    window = seconds(v.get("window"))
    if not 60 <= window <= 86400:
        raise Invalid(f"{name}: the window must be between 1m and 24h.")
    out["window"] = str(v["window"])
    return out


def _selector(v: dict) -> str:
    fam, _ = family(v["metric"])
    parts = [f'{fam.device_label}="{v["device"]}"']
    if v.get("port"):
        parts.append(f'{fam.port_label}="{v["port"]}"')
    return f'{v["metric"]}{{{",".join(parts)}}}'


def promql(v: dict, window: str | None = None) -> str:
    """The query for a metric variable. Built here, never written by a model. window: a shorter one to use instead."""
    sel, f, window = _selector(v), v["function"], window or v.get("window")
    if f == "now":
        inner = sel
    elif f in ("increase", "rate", "changes", "delta"):
        inner = f"{f}({sel}[{window}])"
    else:
        inner = f"{f.removesuffix('_over')}_over_time({sel}[{window}])"
    return f"{v.get('combine', 'max')}({inner})"


def _cut(v: dict, at: float, since: float | None) -> str | None:
    """The variable's window, cut short so it begins no earlier than `since`. None when it already does."""
    if not since or not v.get("window"):
        return None
    left = int(at - since)
    return None if left >= seconds(v["window"]) else f"{max(left, 1)}s"


def check_exists(v: dict) -> None:
    """Raise Invalid unless the network really reports this variable."""
    if v["source"] == "events":
        return  # a count of zero is a real value; list_variables shows what has been seen
    fam, _ = family(v["metric"])
    if prom_query(f"count({_selector(v)})"):
        return
    if v.get("port"):
        ports = sorted({r["metric"].get(fam.port_label, "") for r in prom_query(f'count by ({fam.port_label}) ({v["metric"]}{{{fam.device_label}="{v["device"]}"}})')} - {""})
        if ports:
            raise Invalid(f"{v['name']}: {v['device']} reports {v['metric']} for {', '.join(ports[:40])}, not for {v['port']}.")
    raise Invalid(f"{v['name']}: {v['device']} does not report {v['metric']}. list_variables shows what it reports.")


def words(v: dict) -> str:
    """The variable in plain words."""
    where = f"{v['device']} {v['port']}" if v.get("port") else v["device"]
    if v["source"] == "events":
        what = f"traps saying {TRAP_WORDS[v['trap']]}" if v.get("trap") else "syslog lines"
        if v.get("text"):
            what += f" containing \"{v['text']}\""
        return f"{what} from {v['device']} in the last {v['window']}"
    fam, groups = family(v["metric"])
    means = fam.means.format(*[g.replace("_", " ") for g in groups])
    how = FUNCTIONS[v["function"]] + (f" ({v['window']})" if v.get("window") else "")
    many = "" if v.get("port") or not fam.port_label else f"{COMBINE[v.get('combine', 'max')]} all ports: "
    return f"{where}: {many}{means}, {how}"


def _number(rows: list[dict]) -> float | None:
    if not rows:
        return None
    value = float(rows[0]["value"][1])
    return None if math.isnan(value) or math.isinf(value) else value


def read(v: dict, at: float | None = None, since: float | None = None) -> float | None:
    """The variable's value now (or at a moment). None when nothing was reported.

    since: nothing from before this moment counts. The watcher passes when the monitor was accepted,
    so a new monitor does not fire on the trouble it was written from, which has been looked at already.
    """
    if v["source"] == "events":
        return float(event_count(v, at or time.time(), since))
    return _number(prom_query(promql(v, _cut(v, at or time.time(), since)), at))


def history_of(v: dict, start: float, end: float, step: int = STEP) -> tuple[list[float], list[float | None]]:
    """The variable at every step from start to end: (times, values). A value is None where nothing was reported."""
    first = math.ceil(start / step) * step
    times = [float(t) for t in range(int(first), int(end) + 1, step)]
    if v["source"] == "events":
        window = seconds(v["window"])
        stamps, covered = event_times(v, first - window, end)
        if covered is None:
            return times, [None] * len(times)
        return times, [None if t - window < covered - 1 else float(bisect.bisect_right(stamps, t) - bisect.bisect_right(stamps, t - window)) for t in times]
    rows = prom_range(promql(v), first, end, step)
    got = {}
    for t, value in (rows[0]["values"] if rows else []):
        value = float(value)
        got[round(float(t))] = None if math.isnan(value) or math.isinf(value) else value
    return times, [got.get(round(t)) for t in times]


def summary(values: list[float | None]) -> dict:
    have = sorted(x for x in values if x is not None)
    if not have:
        return {"points": 0}
    pick = lambda q: have[min(len(have) - 1, int(q * (len(have) - 1) + 0.5))]
    return {"points": len(have), "min": have[0], "median": pick(0.5), "p95": pick(0.95), "max": have[-1]}


# --- monitors -----------------------------------------------------------------


def clean(spec: dict, exists: bool = True) -> dict:
    """The monitor in its stored form. Raises Invalid with every problem found."""
    if not isinstance(spec, dict):
        raise Invalid("A monitor is an object: id, title, device, variables, conditions, match.")
    problems = []
    mid, title = str(spec.get("id") or ""), " ".join(str(spec.get("title") or "").split())
    if not ID.match(mid):
        problems.append(f"id {mid!r} must be lowercase words joined by hyphens, 3 to 48 characters.")
    if not 5 <= len(title) <= 90:
        problems.append("title must be 5 to 90 characters: what it catches, in plain words.")
    device = str(spec.get("device") or "")
    if device not in DEVICES or device in FORBIDDEN_DEVICES:
        problems.append(f"device {device!r} is not a device a run can start on.")
    raw_vars = spec.get("variables") or []
    if not isinstance(raw_vars, list) or not 1 <= len(raw_vars) <= MAX_VARIABLES:
        problems.append(f"variables must list 1 to {MAX_VARIABLES} variables.")
        raw_vars = []
    variables = []
    for v in raw_vars:
        try:
            cleaned = clean_variable(v)
            if any(x["name"] == cleaned["name"] for x in variables):
                raise Invalid(f"Two variables are called {cleaned['name']}.")
            variables.append(cleaned)
        except Invalid as exc:
            problems.append(str(exc))
    names = {v["name"] for v in variables}
    raw_conds = spec.get("conditions") or []
    if not isinstance(raw_conds, list) or not 1 <= len(raw_conds) <= MAX_CONDITIONS:
        problems.append(f"conditions must list 1 to {MAX_CONDITIONS} conditions.")
        raw_conds = []
    conditions = []
    for c in raw_conds:
        if not isinstance(c, dict):
            problems.append("Each condition is {variable, op, value}.")
            continue
        if c.get("variable") not in names:
            problems.append(f"condition on {c.get('variable')!r}: no variable has that name.")
            continue
        if c.get("op") not in OPS:
            problems.append(f"condition on {c['variable']}: op must be one of {' '.join(OPS)}.")
            continue
        try:
            value = float(c.get("value"))
            if math.isnan(value) or math.isinf(value):
                raise ValueError
        except (TypeError, ValueError):
            problems.append(f"condition on {c['variable']}: value must be a number.")
            continue
        conditions.append({"variable": c["variable"], "op": c["op"], "value": value})
    unused = names - {c["variable"] for c in conditions}
    if unused and not problems:
        problems.append(f"{', '.join(sorted(unused))} is never used in a condition. Use it or leave it out.")
    match = spec.get("match") or "all"
    if match not in ("all", "any"):
        problems.append("match must be all or any.")
    try:
        hold = int(spec.get("hold_seconds") or 0)
        cooldown = int(spec.get("cooldown_minutes") or 30)
    except (TypeError, ValueError):
        problems.append("hold_seconds and cooldown_minutes are whole numbers.")
        hold, cooldown = 0, 30
    if not 0 <= hold <= 1800:
        problems.append("hold_seconds must be 0 to 1800.")
    if not 5 <= cooldown <= 1440:
        problems.append("cooldown_minutes must be 5 to 1440.")
    port = str(spec.get("interface") or "").strip()
    if port and not PORT.match(port):
        problems.append(f"interface {port!r} is not a port name.")
    tags = [t for t in spec.get("tags") or [] if isinstance(t, str)]
    if tags:
        from ts import tags as tag_book
        unknown = [t for t in tags if not tag_book.is_known(t)]
        if unknown:
            problems.append(f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not tags. list_tags shows them.")
    if not problems and exists:
        for v in variables:
            try:
                check_exists(v)
            except Invalid as exc:
                problems.append(str(exc))
    if problems:
        raise Invalid(" ".join(problems))
    out = {"id": mid, "title": title, "device": device, "variables": variables, "conditions": conditions, "match": match,
           "hold_seconds": hold, "cooldown_minutes": cooldown}
    if port:
        out["interface"] = port
    if tags:
        out["tags"] = tags
    return out


def condition_words(spec: dict) -> list[str]:
    by_name = {v["name"]: v for v in spec["variables"]}
    return [f"{words(by_name[c['variable']])} {OP_WORDS[c['op']]} {c['value']:g}" for c in spec["conditions"]]


def describe(spec: dict) -> str:
    joined = (" AND " if spec["match"] == "all" else " OR ").join(condition_words(spec))
    held = f", for at least {spec['hold_seconds']} s" if spec.get("hold_seconds") else ""
    return f"Starts a run on {spec['device']} when {joined}{held}. Then quiet for at least {spec['cooldown_minutes']} min."


def fingerprint(spec: dict) -> str:
    """The same for two monitors that watch the same thing the same way, whatever they are called."""
    from ts import review
    keyed = {v["name"]: json.dumps({k: x for k, x in v.items() if k != "name"}, sort_keys=True) for v in spec["variables"]}
    conds = sorted([keyed[c["variable"]], c["op"], c["value"]] for c in spec["conditions"])
    return review.fingerprint("monitor", spec["device"], sorted(keyed.values()), conds, spec["match"])


def _holds(spec: dict, values: dict) -> tuple[list[bool | None], bool]:
    results = [None if values.get(c["variable"]) is None else OPS[c["op"]](values[c["variable"]], c["value"]) for c in spec["conditions"]]
    return results, (all(r is True for r in results) if spec["match"] == "all" else any(r is True for r in results))


def evaluate(spec: dict, at: float | None = None, since: float | None = None) -> dict:
    values = {v["name"]: read(v, at, since) for v in spec["variables"]}
    results, holds = _holds(spec, values)
    return {"values": values, "conditions": results, "holds": holds, "missing": [n for n, x in values.items() if x is None]}


def _in_test(t: float, windows: list[dict]) -> list[str]:
    return [w["job"] for w in windows if w["start"] - STEP <= t <= w["end"] + HELD_BACK_GRACE]


def replay(spec: dict, times: list[float], series: dict, windows: list[dict]) -> list[dict]:
    """Every time the monitor would have fired, with the same hold, cooldown and test rules as the watcher."""
    hold, cooldown = spec.get("hold_seconds") or 0, (spec.get("cooldown_minutes") or 30) * 60
    episodes, current, last_fired = [], None, None
    for i, t in enumerate(times):
        _, holds = _holds(spec, {name: values[i] for name, values in series.items()})
        if holds:
            if current is None:
                current = {"began": t, "fired_at": None, "until": t, "held_back": False, "jobs": []}
            current["until"] = t
            jobs = _in_test(t, windows)
            if jobs and current["fired_at"] is None:
                current["held_back"], current["jobs"] = True, sorted(set(current["jobs"]) | set(jobs))
            if current["fired_at"] is None and t - current["began"] >= hold and not current["held_back"] and (last_fired is None or t - last_fired >= cooldown):
                current["fired_at"] = last_fired = t
        elif current is not None:
            if current["fired_at"] is not None or current["held_back"]:
                episodes.append(current)
            current = None
    if current is not None and (current["fired_at"] is not None or current["held_back"]):
        episodes.append(current)
    return episodes


def backtest(spec: dict, days: float = BACKTEST_DAYS, around: float | None = None, now: float | None = None, windows: list[dict] | None = None) -> dict:
    """Replay the monitor over the past days. With `around` (when an incident began), say whether it caught that incident."""
    from ts.sources import jenkins
    end = now or time.time()
    start = end - days * 86400
    series, times, notes = {}, [], []
    for v in spec["variables"]:
        times, values = history_of(v, start, end)
        series[v["name"]] = values
        if all(x is None for x in values):
            notes.append(f"{v['name']} has no recorded values in this period.")
    windows = jenkins.change_windows(start - 3600) if windows is None else windows
    episodes = replay(spec, times, series, windows)
    true_points = sum(1 for i in range(len(times)) if _holds(spec, {n: s[i] for n, s in series.items()})[1])
    fired = [e for e in episodes if e["fired_at"] is not None]
    out = {"days": days, "from": start, "to": end, "step": STEP,
           "fired": len(fired), "held_back": sum(1 for e in episodes if e["held_back"]),
           "true_share": round(true_points / max(1, len(times)), 4),
           "episodes": [{**e, "jobs": e["jobs"][:4]} for e in episodes[-40:]],
           "variables": {name: summary(values) for name, values in series.items()}, "notes": notes}
    if around is not None:
        hit = next((e for e in fired if around - 3600 <= e["fired_at"] <= around + 900 or e["began"] <= around <= e["until"]), None)
        out["incident"] = {"at": around, "caught": hit is not None, "fired_at": hit["fired_at"] if hit else None,
                           "minutes_from_incident": round((hit["fired_at"] - around) / 60, 1) if hit else None,
                           "held_back_then": any(e["held_back"] and e["began"] - 900 <= around <= e["until"] + 900 for e in episodes)}
        out["false_alarms"] = len(fired) - (1 if hit else 0)
    else:
        out["false_alarms"] = len(fired)
    return out


# --- stored monitors ------------------------------------------------------------

_TABLES = """
CREATE TABLE IF NOT EXISTS monitors(num INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, created REAL, updated REAL, status TEXT,
  spec TEXT, why TEXT, proposal INTEGER, run_id TEXT, by TEXT, state TEXT, state_since REAL, last_eval REAL, last TEXT, last_fired REAL);
CREATE TABLE IF NOT EXISTS monitor_events(id INTEGER PRIMARY KEY AUTOINCREMENT, monitor TEXT, time REAL, kind TEXT, note TEXT, value TEXT, run_id TEXT);
CREATE INDEX IF NOT EXISTS monitor_events_by ON monitor_events(monitor, time);
CREATE TABLE IF NOT EXISTS monitor_sessions(run_id TEXT PRIMARY KEY, time REAL, finished REAL, model TEXT, status TEXT, summary TEXT, calls TEXT, proposals TEXT, error TEXT);
"""


def _db(path: Path | None = None):
    conn = history._open(path)
    conn.executescript(_TABLES)
    return conn


def _row(row) -> dict:
    out = dict(row)
    out["spec"] = json.loads(out["spec"] or "{}")
    out["last"] = json.loads(out["last"] or "{}")
    return out


def add(spec: dict, why: str = "", by: str = "operator", proposal: int | None = None, run_id: str = "", path: Path | None = None) -> dict:
    """Store an accepted monitor. It is active from the watcher's next look."""
    spec = clean(spec, exists=False)
    with _db(path) as db:
        if db.execute("SELECT 1 FROM monitors WHERE id = ?", (spec["id"],)).fetchone():
            raise Invalid(f"A monitor called {spec['id']} already exists.")
        now = time.time()
        db.execute("INSERT INTO monitors(id, created, updated, status, spec, why, proposal, run_id, by, state, state_since) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (spec["id"], now, now, "active", json.dumps(spec), why, proposal, run_id, by, "ok", now))
        db.execute("INSERT INTO monitor_events(monitor, time, kind, note) VALUES (?,?,?,?)", (spec["id"], now, "accepted", f"Accepted by {by}" + (f" from suggestion #{proposal}" if proposal else "")))
    return get(spec["id"], path)


def carried_tags(path: Path | None = None) -> set[str]:
    """The tags the active monitors put on the events they raise, so the rules written for them run."""
    try:
        return {t for m in listing("active", path) for t in m["spec"].get("tags") or []}
    except Exception:  # the rules still lint without the monitor store, only more warily
        return set()


def get(monitor: str, path: Path | None = None) -> dict | None:
    with _db(path) as db:
        row = db.execute("SELECT * FROM monitors WHERE id = ?", (monitor,)).fetchone()
    return _row(row) if row else None


def listing(status: str | None = None, path: Path | None = None) -> list[dict]:
    with _db(path) as db:
        rows = db.execute("SELECT * FROM monitors" + (" WHERE status = ?" if status else " WHERE status != 'retired'") + " ORDER BY num", (status,) if status else ()).fetchall()
    return [_row(r) for r in rows]


def set_status(monitor: str, status: str, by: str = "operator", path: Path | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    with _db(path) as db:
        if not db.execute("UPDATE monitors SET status = ?, updated = ? WHERE id = ?", (status, time.time(), monitor)).rowcount:
            raise KeyError(monitor)
        db.execute("INSERT INTO monitor_events(monitor, time, kind, note) VALUES (?,?,?,?)", (monitor, time.time(), status, f"Set to {status} by {by}"))
    return get(monitor, path)


def note(monitor: str, kind: str, text: str, value: dict | None = None, run_id: str = "", path: Path | None = None) -> None:
    with _db(path) as db:
        db.execute("INSERT INTO monitor_events(monitor, time, kind, note, value, run_id) VALUES (?,?,?,?,?,?)", (monitor, time.time(), kind, text, json.dumps(value or {}), run_id))


def events(monitor: str | None = None, limit: int = 30, path: Path | None = None) -> list[dict]:
    with _db(path) as db:
        rows = db.execute("SELECT * FROM monitor_events" + (" WHERE monitor = ?" if monitor else "") + " ORDER BY time DESC LIMIT ?", ((monitor, limit) if monitor else (limit,))).fetchall()
    return [{**dict(r), "value": json.loads(r["value"] or "{}")} for r in rows]


def _save_state(monitor: str, state: str, since: float, result: dict, now: float, fired: float | None, path: Path | None) -> None:
    with _db(path) as db:
        db.execute("UPDATE monitors SET state = ?, state_since = ?, last_eval = ?, last = ?, last_fired = COALESCE(?, last_fired) WHERE id = ?",
                   (state, since, now, json.dumps(result), fired, monitor))


# --- the watcher's side -----------------------------------------------------------

_LAST: dict[str, dict] = {}
_LAST_LOCK = threading.Lock()
_ALIVE = {"at": 0.0}


def origin(row: dict) -> dict:
    """Who put a monitor in place: the AI, through a suggestion a person accepted, or a person."""
    return {"by": "ai" if row.get("proposal") else "person", "proposal": row.get("proposal"), "accepted_by": row.get("by") or "", "accepted": row.get("created"),
            "from_run": row.get("run_id") or ""}


def fired_event(spec: dict, result: dict, now: float, why: str = "", made: dict | None = None) -> dict:
    """The event a monitor raises: investigated like a trap, on the monitor's device. made is origin(row)."""
    from ts.intake import event_for
    values = "; ".join(f"{name} = {value:.10g}" for name, value in result["values"].items() if value is not None)
    event = event_for("monitor.fired", spec["device"], f"monitor {spec['id']}", f"Monitor fired: {spec['title']}",
                      f"Monitor {spec['id']} fired", f"{describe(spec)} Values now: {values}." + (f" Why this monitor exists: {why}" if why else ""),
                      monitor=spec["id"], merge_key=f"monitor:{spec['id']}", monitor_values=result["values"])
    event.update(source="monitor", time=now, trap_time=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)))
    if made:
        event["monitor_origin"] = dict(made)
    if spec.get("interface"):
        event["interface"] = spec["interface"]
    if spec.get("tags"):
        event["tags"] = list(spec["tags"])
    return event


class Watch:
    """Looks at every active monitor in turn. `put` receives the event when one fires."""

    def __init__(self, put, log=print, path: Path | None = None):
        self.put, self.log, self.path = put, log, path
        self.mem: dict[str, dict] = {}
        self.test_seen, self.test_jobs = 0.0, []

    def tests_now(self, now: float) -> list[str]:
        from ts import lab
        from ts.sources import jenkins
        jobs = list(jenkins.running_change_jobs())
        if lab.lock_holders() - {os.getpid()}:  # this process holding it is its own repair or AI stop-gap, not a job (see own_change)
            jobs.append("a job holding the network change lock")
        if jobs:
            self.test_seen, self.test_jobs = now, jobs
            return jobs
        if now - self.test_seen < HELD_BACK_GRACE:
            return [f"{job}, finished less than {HELD_BACK_GRACE // 60} minutes ago" for job in self.test_jobs]
        return []

    def own_change(self) -> bool:
        """The troubleshooter is changing the network itself. A monitor waits for that to finish, then looks again;
        the trouble it watches began without it, so the episode is not put down to a test."""
        from ts import lab
        return lab.own_change_running()

    def _memory(self, row: dict) -> dict:
        if row["id"] not in self.mem:  # carry on where the last watcher left off, so a restart does not fire again
            busy = row.get("state") in ("holding", "fired", "held_back")
            self.mem[row["id"]] = {"since": row.get("state_since") if busy else None, "fired": row.get("state") == "fired", "held_back": row.get("state") == "held_back",
                                   "state": row.get("state") or "ok", "state_since": row.get("state_since"), "last_fired": row.get("last_fired"), "jobs": []}
        return self.mem[row["id"]]

    def step(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        fired_events, live = [], {}
        rows = listing("active", self.path)
        for row in rows:
            spec, mem = row["spec"], self._memory(row)
            before, fired_at = mem["state"], None
            try:
                result = evaluate(spec, now, since=row.get("created"))  # only what happened since it was accepted
            except Exception as exc:  # Prometheus or the data lake away: say so, never fire
                mem["state"] = "error"
                result = {"values": {}, "conditions": [], "holds": False, "missing": [], "error": f"{type(exc).__name__}: {exc}"[:300]}
                if before != "error":
                    self.log(f"monitor {spec['id']}: cannot read its variables: {result['error']}")
                    note(spec["id"], "error", result["error"], path=self.path)
            else:
                if result["holds"]:
                    if mem["since"] is None:
                        mem.update(since=now, fired=False, held_back=False, jobs=[])
                    jobs = self.tests_now(now) if not (mem["fired"] or mem["held_back"]) else []
                    if jobs and not mem["held_back"]:
                        mem.update(held_back=True, jobs=jobs)
                        note(spec["id"], "held_back", f"Conditions held while {', '.join(jobs[:3])} was running, so no run was started.", result, path=self.path)
                        self.log(f"monitor {spec['id']}: held back, {jobs[0]} is running")
                    cooled = mem["last_fired"] is None or now - mem["last_fired"] >= spec["cooldown_minutes"] * 60
                    if not mem["fired"] and not mem["held_back"] and now - mem["since"] >= spec["hold_seconds"] and cooled and not self.own_change():
                        event = fired_event(spec, result, now, row.get("why") or "", origin(row))
                        from ts import engine
                        run_id = engine.incident_id(event)
                        note(spec["id"], "fired", "Conditions held, so a run was started.", result, run_id, path=self.path)
                        self.log(f"monitor {spec['id']}: fired, starting run {run_id}")
                        mem.update(fired=True, last_fired=now)
                        fired_at = now
                        fired_events.append(event)
                    mem["state"] = "fired" if mem["fired"] else "held_back" if mem["held_back"] else "holding"
                else:
                    if mem["fired"] or mem["held_back"]:
                        note(spec["id"], "cleared", "Conditions no longer hold.", result, path=self.path)
                    mem.update(since=None, fired=False, held_back=False, jobs=[])
                    mem["state"] = "no_data" if result["values"] and len(result["missing"]) == len(result["values"]) else "ok"
            if mem["state"] != before:
                mem["state_since"] = now
            _save_state(spec["id"], mem["state"], mem.get("state_since") or now, {**result, "jobs": mem.get("jobs") or []}, now, fired_at, self.path)
            live[spec["id"]] = {"spec": spec, "result": result, "state": mem["state"]}
        with _LAST_LOCK:
            _LAST.clear()
            _LAST.update(live)
        _ALIVE["at"] = now
        for event in fired_events:
            self.put(event)
        return fired_events


def watch_forever(put, log=print, every: float = EVERY) -> None:
    watch = Watch(put, log)
    while True:
        began = time.time()
        try:
            watch.step(began)
        except Exception as exc:  # the monitors must never stop the watcher
            log(f"monitors failed: {type(exc).__name__}: {exc}")
        time.sleep(max(1.0, every - (time.time() - began)))


# --- what Prometheus scrapes -------------------------------------------------------


def _label(value) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def metrics_text(now: float | None = None) -> str:
    now = now or time.time()
    with _LAST_LOCK:
        live = dict(_LAST)
    lines = ["# HELP ts_monitor_up 1 while the troubleshooter is looking at monitors.", "# TYPE ts_monitor_up gauge",
             f"ts_monitor_up {1 if now - _ALIVE['at'] < 3 * EVERY else 0}",
             "# HELP ts_monitor_variable A monitor's variable, as the troubleshooter last read it.", "# TYPE ts_monitor_variable gauge"]
    for mid, item in sorted(live.items()):
        for v in item["spec"]["variables"]:
            value = item["result"]["values"].get(v["name"])
            if value is not None:
                lines.append(f'ts_monitor_variable{{monitor="{_label(mid)}",variable="{_label(v["name"])}",device="{_label(v["device"])}"}} {value:.10g}')
    lines += ["# HELP ts_monitor_threshold The value a condition compares its variable with.", "# TYPE ts_monitor_threshold gauge"]
    for mid, item in sorted(live.items()):
        for i, c in enumerate(item["spec"]["conditions"], 1):
            lines.append(f'ts_monitor_threshold{{monitor="{_label(mid)}",variable="{_label(c["variable"])}",condition="{i}",op="{_label(c["op"])}"}} {c["value"]:g}')
    lines += ["# HELP ts_monitor_condition 1 when the condition holds, 0 when not.", "# TYPE ts_monitor_condition gauge"]
    for mid, item in sorted(live.items()):
        for i, held in enumerate(item["result"].get("conditions") or [], 1):
            if held is not None:
                lines.append(f'ts_monitor_condition{{monitor="{_label(mid)}",condition="{i}"}} {1 if held else 0}')
    lines += ["# HELP ts_monitor_state 0 quiet, 1 conditions holding, 2 fired a run, 3 held back while a test ran, -1 no data, -2 cannot read.", "# TYPE ts_monitor_state gauge"]
    for mid, item in sorted(live.items()):
        lines.append(f'ts_monitor_state{{monitor="{_label(mid)}"}} {STATE_CODES.get(item["state"], -2)}')
    return "\n".join(lines) + "\n"


def serve_metrics(address: str = METRICS_AT, log=print) -> ThreadingHTTPServer | None:
    """/metrics for Prometheus, on the address it already scrapes this machine on. Read-only."""
    host, _, port = address.rpartition(":")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ts-monitors"

        def do_GET(self):  # noqa: N802
            if self.path.split("?", 1)[0] != "/metrics":
                self.send_error(404)
                return
            body = metrics_text().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    try:
        server = ThreadingHTTPServer((host, int(port)), Handler)
    except OSError as exc:
        log(f"monitors: cannot serve /metrics on {address}: {exc}")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"monitors: /metrics on http://{address}/metrics")
    return server
