"""After a run finds a cause, the AI suggests monitors that would have caught it sooner.

It works only through tools, so it never writes a query or guesses a name:
list_variables shows what a device really reports, read_variable shows a variable's values
around the incident against its normal range, test_monitor replays a monitor over the past
week, and propose_monitor only accepts a monitor exactly as it was tested, when that test
caught this incident and stayed quiet otherwise. A person accepts or rejects each suggestion
on the review page; a rejection's reason is shown to it next time.

It runs off to the side of the watcher, one run at a time, with the model chosen for
"Monitor suggestions" on the AI models page.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from pathlib import Path

from ts import aiconfig, history, llm, monitors, review, scrub, store, tags

OFF_SWITCH = Path(os.environ.get("TS_AI_OFF", Path(__file__).resolve().parent.parent / "data" / "ai.off"))
MAX_SECONDS = 480
MAX_CALLS = 30
MAX_PROPOSALS = 2
MAX_FALSE_ALARMS = 2          # in the week replayed, outside test jobs and this incident
MAX_SESSIONS_PER_HOUR = 4
SAME_AGAIN_WITHIN = 86400     # one look per device and cause a day
INCIDENT_SPAN = 1800          # read_variable shows this much either side of the incident
NOT_WORTH = {"stop"}          # a run that saw nothing wrong leaves nothing to monitor for

SYSTEM = """You are the monitoring engineer for a production network. An incident has just been worked, and your job is to suggest monitors that would catch this kind of trouble sooner next time, or to conclude that none is worth adding.

A monitor watches one to five variables that the network already reports, each with a condition, and starts a troubleshooting run on one device when the conditions hold. A person reviews every suggestion before it does anything, so each one has to earn their attention: it fires on this kind of trouble and stays quiet the rest of the time.

Work only through the tools. Never invent a metric, device, port, trap or log text: list_variables shows exactly what exists, and anything else is refused. Every tool call except finish takes a reason: one sentence saying why you are making it.

How to work:
1. Read the incident in your brief: what broke, where, when it began, and what the investigation concluded.
2. Call list_variables for the device where the trouble showed. If the brief names a far end or another device on the path, look at that one too. Pick the variables this trouble would move: carrier transitions or port state on a port that flapped, error or discard counters on a lossy link, traps such as linkDown or ospfNbrStateChange from the device, a syslog message that appeared.
3. Call read_variable on each candidate, built exactly as the variable will be used. Compare its value around the incident with its normal range outside test jobs. A variable that did not move around the incident cannot catch it: drop it.
4. Build a monitor from one to three of the variables that moved. Set each threshold from the numbers you read: above the normal range (its max outside tests and the incident) and at or below what the incident reached. Use match all when the conditions must be true together, which keeps false alarms down; any when each alone is enough. Use hold_seconds when a brief blip should not count. Counters only ever go up, so use increase, rate or changes over a window on them, never their raw value.
5. Call test_monitor. It replays the monitor over the past week, minute by minute, and says whether it catches this incident, how many other times it would have fired (false alarms), and how many firings were held back because a scheduled job was testing the network at the time. Held-back firings are fine: tests break things on purpose, and the watcher never starts a run for them. Adjust the thresholds, windows or conditions and test again until it catches this incident with at most """ + str(MAX_FALSE_ALARMS) + """ false alarms. If a few tries do not get there, do not suggest it.
6. Call propose_monitor with exactly the monitor you last tested and passed. Give it an id of lowercase words joined by hyphens, a title that says in plain words what it catches, the device a run should start on, and tags from list_tags if any fit. In why, write two or three plain sentences for the person who decides: what trouble it catches, which numbers the thresholds came from, and how the replay went. Suggest at most """ + str(MAX_PROPOSALS) + """ monitors, and never one that repeats a monitor that exists or a suggestion a person already turned down.
7. Call finish exactly once, last. Saying nothing is worth suggesting is a good answer when the trouble left no trace in any variable (a config change, for example) or when the variables that moved are just as busy at normal times.

The variable forms, with example values only:
{"name": "flaps", "source": "metric", "device": "s1", "metric": "interfaces_interface_state_counters_carrier_transitions", "port": "Ethernet4", "function": "increase", "window": "10m"}
{"name": "ospf_traps", "source": "events", "device": "r1", "protocol": "trap", "trap": "ospfNbrStateChange", "window": "15m"}
{"name": "auth_fallback", "source": "events", "device": "s2", "protocol": "syslog", "text": "AUTHZ_FALLBACK", "window": "30m"}"""


_STR = {"type": "string"}
_VARIABLE = {"type": "object", "description": "One number read from what is already collected: a metric (source metric) or a count of events from the data lake (source events).",
             "properties": {
                 "name": {"type": "string", "description": "Short lowercase name the conditions use, like flaps or ospf_traps"},
                 "source": {"type": "string", "enum": ["metric", "events"]},
                 "device": {"type": "string", "description": "The device it is read from"},
                 "metric": {"type": "string", "description": "source metric: the metric exactly as list_variables names it"},
                 "port": {"type": "string", "description": "source metric: one port exactly as list_variables names it. Leave it out to take all the device's ports together, see combine."},
                 "function": {"type": "string", "enum": list(monitors.FUNCTIONS), "description": "source metric: " + "; ".join(f"{k}: {v}" for k, v in monitors.FUNCTIONS.items())},
                 "window": {"type": "string", "description": "How far back the function or the count looks, such as 5m, 10m or 1h. Required except for function now."},
                 "combine": {"type": "string", "enum": list(monitors.COMBINE), "description": "source metric without a port: how the ports become one number. Default max."},
                 "protocol": {"type": "string", "enum": ["trap", "syslog"], "description": "source events: which kind of event to count"},
                 "trap": {"type": "string", "enum": list(monitors.TRAPS), "description": "source events with protocol trap: which trap"},
                 "text": {"type": "string", "description": "source events: words the event must contain, such as a port name in a trap or the message in a syslog line. Required for syslog."}},
             "required": ["name", "source", "device"]}
_CONDITION = {"type": "object", "properties": {"variable": {"type": "string", "description": "A variable's name"}, "op": {"type": "string", "enum": list(monitors.OPS)}, "value": {"type": "number"}},
              "required": ["variable", "op", "value"]}
_MONITOR = {"id": {"type": "string", "description": "Lowercase words joined by hyphens, like s1-eth4-flapping"},
            "title": {"type": "string", "description": "What it catches, in plain words"},
            "device": {"type": "string", "description": "The device a run starts on when it fires"},
            "interface": {"type": "string", "description": "The port it is about, if it is about one"},
            "tags": {"type": "array", "items": _STR, "description": "Tags from list_tags that describe what it catches"},
            "variables": {"type": "array", "items": _VARIABLE},
            "conditions": {"type": "array", "items": _CONDITION},
            "match": {"type": "string", "enum": ["all", "any"], "description": "all: every condition at once. any: one is enough."},
            "hold_seconds": {"type": "integer", "description": "How long the conditions must keep holding before it fires. 0 fires at once."},
            "cooldown_minutes": {"type": "integer", "description": "The least time between two runs it starts. Default 30."}}
_REASON = {"type": "string", "description": "One sentence: why you are making this call."}


def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


DEFINITIONS = [
    {"type": "function", "function": {"name": "list_variables", "description": "Everything one device reports that a monitor can use: each metric with what it means and its ports, and the traps and syslog messages seen from it over the past week with how often.",
                                      "parameters": _obj({"device": _STR, "reason": _REASON}, ["device", "reason"])}},
    {"type": "function", "function": {"name": "read_variable", "description": "Read one variable: the query it becomes, its value now, around this incident, and its normal range over the past day and week outside test jobs and this incident.",
                                      "parameters": _obj({"variable": _VARIABLE, "reason": _REASON}, ["variable", "reason"])}},
    {"type": "function", "function": {"name": "test_monitor", "description": "Replay a monitor over the past week, minute by minute, with the same rules the watcher uses. Says whether it catches this incident, its false alarms, and what was held back during test jobs.",
                                      "parameters": _obj({**_MONITOR, "reason": _REASON}, ["id", "title", "device", "variables", "conditions", "match", "reason"])}},
    {"type": "function", "function": {"name": "propose_monitor", "description": "Put a monitor you tested on the review page for a person to accept or reject. It must be exactly the monitor that passed test_monitor.",
                                      "parameters": _obj({**_MONITOR, "why": {"type": "string", "description": "Two or three plain sentences: what it catches, where the thresholds came from, how the replay went"}, "reason": _REASON},
                                                         ["id", "title", "device", "variables", "conditions", "match", "why", "reason"])}},
    {"type": "function", "function": {"name": "list_tags", "description": "The tags a monitor can carry.", "parameters": _obj({"reason": _REASON}, ["reason"])}},
    {"type": "function", "function": {"name": "finish", "description": "End your work. Call it exactly once, last.",
                                      "parameters": _obj({"outcome": {"type": "string", "enum": ["suggested", "nothing_worth_it"]},
                                                          "summary": {"type": "string", "description": "Two plain sentences: what you suggested and why, or why nothing was worth suggesting"}}, ["outcome", "summary"])}},
]


# --- the session ------------------------------------------------------------------


class Session:
    def __init__(self, record: dict, model: str):
        self.record, self.model = record, model
        self.around = float(record["event"]["time"])
        self.calls: list[dict] = []
        self.proposals: list[int] = []
        self.tested: dict[str, dict] = {}   # fingerprint -> the test that passed or failed
        self.result: dict | None = None
        self._windows: list[dict] | None = None

    def windows(self) -> list[dict]:
        if self._windows is None:
            from ts.sources import jenkins
            self._windows = jenkins.change_windows(time.time() - monitors.BACKTEST_DAYS * 86400 - 3600)
        return self._windows


def _metric_catalog(device: str) -> list[dict]:
    out = {}
    for label in ("source", "device"):
        for row in monitors.prom_query(f'count by (__name__, interface_name, interface) ({{{label}="{device}"}})'):
            name = row["metric"].get("__name__", "")
            found = monitors.family(name)
            if not found or found[0].device_label != label:
                continue
            fam, groups = found
            item = out.setdefault(name, {"metric": name, "means": fam.means.format(*[g.replace("_", " ") for g in groups]), "kind": fam.kind, "ports": set()})
            port = row["metric"].get(fam.port_label or "")
            if port:
                item["ports"].add(port)
    rows = []
    for item in sorted(out.values(), key=lambda x: x["metric"]):
        rows.append({**item, "ports": sorted(item["ports"]), "use": "increase, rate or changes over a window" if item["kind"] == "counter" else "now, changes, or max_over/min_over/avg_over a window"})
    return rows


_STAMP = re.compile(r"^\w{3}\s+\d+\s+[\d:]+\s+\[[^\]]*\]\s*")
_NUMBERISH = re.compile(r"\b\d+(\.\d+){0,3}\b|\b[0-9a-f]{2}(:[0-9a-f]{2}){5}\b", re.I)


def _event_catalog(device: str) -> dict:
    db = monitors._lake()
    now = time.time()
    have = set(db.list_collection_names())
    traps, lines = {}, {}
    for day in monitors._days(now - 7 * 86400, now):
        if day not in have:
            continue
        for doc in db[day].find({"source_device": device, "protocol": {"$in": ["snmp_trap", "syslog"]}, "timestamp": {"$gte": now - 7 * 86400}}, {"raw_payload": 1, "protocol": 1, "_id": 0}).limit(20000):
            raw = doc.get("raw_payload", "")
            if doc.get("protocol") == "snmp_trap":
                name = next((n for n, oid in monitors.TRAPS.items() if re.search(rf"4\.1\.0 = OID: iso\.{re.escape(oid)}(\t|$)", raw)), None)
                if name:
                    traps[name] = traps.get(name, 0) + 1
            else:
                text = _STAMP.sub("", raw)
                key = _NUMBERISH.sub("#", text)[:90]
                seen = lines.setdefault(key, {"count": 0, "example": text[:160]})
                seen["count"] += 1
    common = sorted(lines.values(), key=lambda x: -x["count"])[:15]
    return {"traps_past_week": [{"trap": n, "means": monitors.TRAP_WORDS[n], "count": c} for n, c in sorted(traps.items(), key=lambda x: -x[1])],
            "traps_possible": list(monitors.TRAPS),
            "syslog_past_week": [{"count": x["count"], "example": x["example"]} for x in common],
            "note": "For a trap, text can narrow it to one port, such as Ethernet4. For syslog, text is words from the message, taken from an example."}


def _list_variables(s: Session, a: dict) -> dict:
    device = str(a.get("device") or "")
    if device not in monitors.DEVICES or device in monitors.FORBIDDEN_DEVICES:
        return {"outcome": "invalid", "saw": f"{device!r} is not a device here. Devices: {', '.join(d for d in monitors.DEVICES if d not in monitors.FORBIDDEN_DEVICES)}."}
    out = {"outcome": "ok", "device": device, "metrics": _metric_catalog(device)}
    try:
        out["events"] = _event_catalog(device)
    except Exception as exc:  # the data lake away: metrics still work
        out["events"] = {"error": f"The data lake could not be read: {type(exc).__name__}"}
    return out


def _outside(times: list[float], values: list, windows: list[dict], around: float) -> list:
    skip = lambda t: abs(t - around) <= 2 * INCIDENT_SPAN or monitors._in_test(t, windows)
    return [v for t, v in zip(times, values) if not skip(t)]


def _read_variable(s: Session, a: dict) -> dict:
    v = monitors.clean_variable(a.get("variable") or {})
    monitors.check_exists(v)
    now = time.time()
    times, values = monitors.history_of(v, now - monitors.BACKTEST_DAYS * 86400, now)
    near = [x for t, x in zip(times, values) if abs(t - s.around) <= INCIDENT_SPAN]
    day = [(t, x) for t, x in zip(times, values) if t >= now - 86400]
    return {"outcome": "ok", "words": monitors.words(v), "reads_as": monitors.promql(v) if v["source"] == "metric" else "a count of matching events in the data lake",
            "now": values[-1] if values else None,
            "around_incident": {**monitors.summary(near), "from": time.strftime("%m-%d %H:%M", time.localtime(s.around - INCIDENT_SPAN)), "to": time.strftime("%m-%d %H:%M", time.localtime(s.around + INCIDENT_SPAN))},
            "normal_last_day": monitors.summary(_outside([t for t, _ in day], [x for _, x in day], s.windows(), s.around)),
            "normal_last_week": monitors.summary(_outside(times, values, s.windows(), s.around)),
            "during_test_jobs_last_week": monitors.summary([x for t, x in zip(times, values) if monitors._in_test(t, s.windows())]),
            "note": "normal_* leaves out test jobs and the hour around this incident."}


def _when(t: float | None) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "-"


def _test_monitor(s: Session, a: dict) -> dict:
    spec = monitors.clean({k: v for k, v in a.items() if k not in ("why", "reason")})
    result = monitors.backtest(spec, around=s.around, windows=s.windows())
    inc = result["incident"]
    false = [e for e in result["episodes"] if e["fired_at"] and not (inc["caught"] and e["fired_at"] == inc["fired_at"])]
    passed = inc["caught"] and result["false_alarms"] <= MAX_FALSE_ALARMS
    if passed:
        verdict = f"Passes: it catches this incident and would have fired {result['false_alarms']} other times in the week."
    elif not inc["caught"]:
        verdict = "Fails: it does not catch this incident." + (" Its conditions held then, but a test job was running, so it was held back; this incident happened during a test." if inc["held_back_then"] else " Read the variables around the incident again: the thresholds are above what it reached, the window is too short, or match all needs conditions that did not happen together.")
    else:
        verdict = f"Fails: {result['false_alarms']} false alarms in the week, more than {MAX_FALSE_ALARMS}. Raise the thresholds, lengthen hold_seconds, or add a condition with match all."
    summary = {"passed": passed, "verdict": verdict, "catches_this_incident": inc["caught"],
               "fired_minutes_from_incident": inc["minutes_from_incident"], "false_alarms_in_week": result["false_alarms"],
               "false_alarm_times": [_when(e["fired_at"]) for e in false][:10], "held_back_during_tests": result["held_back"],
               "conditions_true_share": f"{100 * result['true_share']:.2f}% of the week", "variables": result["variables"], "notes": result["notes"],
               "words": monitors.describe(spec)}
    s.tested[monitors.fingerprint(spec)] = {"spec": spec, "summary": summary, "result": result}
    return {"outcome": "passed" if passed else "failed", **summary}


def _existing(fp: str) -> dict | None:
    for row in monitors.listing():
        if monitors.fingerprint(row["spec"]) == fp:
            return row
    return None


def _propose_monitor(s: Session, a: dict) -> dict:
    if len(s.proposals) >= MAX_PROPOSALS:
        return {"outcome": "refused", "saw": f"You have already suggested {MAX_PROPOSALS}. Call finish."}
    why = " ".join(str(a.get("why") or "").split())
    if len(why) < 40:
        return {"outcome": "invalid", "saw": "why must be two or three plain sentences for the person who decides."}
    spec = monitors.clean({k: v for k, v in a.items() if k not in ("why", "reason")})
    fp = monitors.fingerprint(spec)
    tested = s.tested.get(fp)
    if not tested:
        return {"outcome": "refused", "saw": "This is not a monitor you tested. Call test_monitor with exactly these variables and conditions first."}
    if not tested["summary"]["passed"]:
        return {"outcome": "refused", "saw": "This monitor failed its test: " + tested["summary"]["verdict"]}
    have = _existing(fp)
    if have:
        return {"outcome": "refused", "saw": f"Monitor {have['id']} already watches exactly this."}
    if monitors.get(spec["id"]):
        return {"outcome": "invalid", "saw": f"A monitor called {spec['id']} already exists. Choose another id."}
    seen = review.earlier(fp)
    if seen:
        said = f" A person turned it down: {seen['reason']}" if seen["status"] == review.REJECTED else ""
        return {"outcome": "refused", "saw": f"This was already suggested as #{seen['id']} and is {seen['status']}.{said}"}
    result = tested["result"]
    proof = {"test": tested["summary"], "episodes": result["episodes"], "from": result["from"], "to": result["to"], "incident_at": s.around}
    body = {"monitor": spec, "words": monitors.describe(spec), "variables": {v["name"]: monitors.words(v) for v in spec["variables"]}}
    number = review.add("monitor", spec["title"], scrub.text(why), body, proof, s.record["id"], s.model, fp)
    s.proposals.append(number)
    return {"outcome": "saved", "saw": f"Waiting for review as #{number}."}


def _list_tags(s: Session, a: dict) -> dict:
    return {"outcome": "ok", "tags": tags.known()}


def _finish(s: Session, a: dict) -> dict:
    if a.get("outcome") not in ("suggested", "nothing_worth_it"):
        return {"outcome": "invalid", "saw": "outcome must be suggested or nothing_worth_it."}
    if a["outcome"] == "suggested" and not s.proposals:
        return {"outcome": "invalid", "saw": "Nothing was saved with propose_monitor, so the outcome is nothing_worth_it."}
    s.result = {"outcome": a["outcome"], "summary": str(a.get("summary") or "")[:1000]}
    return {"outcome": "done"}


TOOLS = {"list_variables": _list_variables, "read_variable": _read_variable, "test_monitor": _test_monitor,
         "propose_monitor": _propose_monitor, "list_tags": _list_tags, "finish": _finish}


def call(s: Session, name: str, args) -> dict:
    started = time.time()
    if name not in TOOLS:
        result = {"outcome": "invalid", "saw": f"There is no tool called {name}."}
    elif not isinstance(args, dict):
        result = {"outcome": "invalid", "saw": "The arguments were not valid JSON."}
    else:
        try:
            result = TOOLS[name](s, args)
        except monitors.Invalid as exc:
            result = {"outcome": "invalid", "saw": str(exc)}
        except Exception as exc:  # a broken tool is reported to the model, never fatal
            result = {"outcome": "error", "saw": f"{type(exc).__name__}: {exc}"[:500]}
    result = scrub.deep(result)
    s.calls.append({"n": len(s.calls) + 1, "tool": name, "reason": str((args or {}).get("reason", ""))[:500] if isinstance(args, dict) else "",
                    "args": scrub.deep({k: v for k, v in (args or {}).items() if k != "reason"}) if isinstance(args, dict) else {},
                    "outcome": result.get("outcome"), "saw": str(result.get("saw") or result.get("verdict") or "")[:600], "ms": int((time.time() - started) * 1000)})
    return result


# --- the brief --------------------------------------------------------------------


_PLACES = ("device", "found_device", "peer_device", "far_device", "alarm_device")
_PORTS = {"device": "interface", "found_device": "interface", "peer_device": "peer_interface", "far_device": "far_interface"}


def _named(record: dict) -> list[str]:
    """The devices and ports the investigation touched, most relevant first."""
    seen: list[str] = []
    values = {**{k: str(v) for k, v in record["event"].items() if isinstance(v, (str, int, float))}, **(record.get("values") or {})}
    for key in _PLACES:
        device = values.get(key)
        if device in monitors.DEVICES and device not in monitors.FORBIDDEN_DEVICES:
            port = values.get(_PORTS.get(key, ""), "")
            item = f"{device} {port}".strip() if port and port != "unknown" else device
            if item not in seen:
                seen.append(item)
    for change in (record.get("agent") or {}).get("changes") or []:
        device = change.get("device")
        if device in monitors.DEVICES and device not in seen and device not in monitors.FORBIDDEN_DEVICES:
            seen.append(device)
    return seen[:8]


def brief(record: dict) -> str:
    ev, r, ai = record["event"], record["result"], record.get("agent") or {}
    out = ["THE INCIDENT", f"Run {record['id']}, began {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ev['time']))}, on {ev['device']}: {ev.get('title') or ev.get('event')}",
           f"How it started: {ev.get('trigger') or ev.get('source')}"]
    if ev.get("detail"):
        out.append(f"Detail: {ev['detail']}")
    out.append(f"What the rules concluded ({r['kind']}{', ' + r['label'] if r.get('label') else ''}): {r.get('text') or ''}")
    if record.get("repair"):
        out.append(f"Repair by the rules: {record['repair'].get('status')}")
    if ai.get("root_cause") or ai.get("outcome"):
        out.append(f"What the AI investigation concluded ({ai.get('outcome')}): {ai.get('root_cause') or ''}")
        if (record.get("analysis") or {}).get("summary"):
            out.append(f"Its summary: {record['analysis']['summary']}")
    out.append(f"Devices and ports the investigation named: {', '.join(_named(record)) or ev['device']}")
    out.append(f"Use {time.strftime('%m-%d %H:%M', time.localtime(ev['time']))} as the incident time; read_variable and test_monitor already centre on it.")
    have = monitors.listing()
    if have:
        out += ["", "MONITORS THAT ALREADY EXIST. Do not repeat these:"] + [f"- {m['id']} ({m['status']}): {m['spec']['title']}. {monitors.describe(m['spec'])}" for m in have]
    waiting = review.listing(review.PENDING, "monitor", size=20)["items"]
    if waiting:
        out += ["", "MONITOR SUGGESTIONS ALREADY WAITING FOR A PERSON:"] + [f"- #{p['id']} {p['title']}: {p['body'].get('words', '')}" for p in waiting]
    refused = [x for x in review.rejected_like(" ".join(filter(None, [ev.get("title"), r.get("label"), r.get("text"), "monitor"])), 8) if x["kind"] == "monitor"]
    if refused:
        out += ["", "MONITOR SUGGESTIONS A PERSON TURNED DOWN. Do not suggest these or anything that repeats the same mistake:"]
        out += [f"- #{x['id']} {x['title']}: {x['body'].get('words', '')} Their reason: {x['reason']}" for x in refused]
    return scrub.text("\n".join(out))


# --- deciding which runs, and the worker ---------------------------------------------

QUEUE: queue.Queue = queue.Queue()
_RUNNING = {"on": False}


def _db():
    return monitors._db()


def done_for(run_id: str) -> bool:
    with _db() as db:
        return db.execute("SELECT 1 FROM monitor_sessions WHERE run_id = ?", (run_id,)).fetchone() is not None


def _lately(seconds: float) -> int:
    with _db() as db:
        return db.execute("SELECT COUNT(*) FROM monitor_sessions WHERE time > ?", (time.time() - seconds,)).fetchone()[0]


def _same_lately(record: dict) -> bool:
    """A suggestion session in the last day for the same device and the same cause."""
    ev, label = record["event"], record["result"].get("label") or record["event"].get("event")
    with _db() as db:
        rows = db.execute("SELECT run_id FROM monitor_sessions WHERE time > ?", (time.time() - SAME_AGAIN_WITHIN,)).fetchall()
    for row in rows:
        other = store.load(row["run_id"]) or {}
        if other.get("event", {}).get("device") == ev["device"] and (other.get("result", {}).get("label") or other.get("event", {}).get("event")) == label:
            return True
    return False


def skip_reason(record: dict) -> str | None:
    """Why no monitor should be suggested from this run, or None if one should be looked for."""
    ev, r, ai = record["event"], record["result"], record.get("agent") or {}
    if ev.get("source") == "monitor":
        return "a monitor started this run"
    if ev.get("change_running") or (record.get("repair") or {}).get("expected"):
        return "a job was changing the network at the time"
    from ts.sources import jenkins
    t = float(ev["time"])
    if any(w["start"] - 60 <= t <= w["end"] + monitors.HELD_BACK_GRACE for w in jenkins.change_windows(t - 6 * 3600)):
        return "a test or change job was running when it began"
    found_something = r["kind"] in ("cause", "escalate") or ai.get("outcome") in ("fixed", "workaround", "partly", "needs_person")
    if r["kind"] in NOT_WORTH and not found_something:
        return "the run found nothing wrong"
    if not found_something:
        return "the run found no cause"
    if OFF_SWITCH.exists():
        return "the AI is switched off"
    if done_for(record["id"]):
        return "already looked at"
    if _lately(3600) >= MAX_SESSIONS_PER_HOUR:
        return f"already {MAX_SESSIONS_PER_HOUR} suggestion sessions in the last hour"
    if _same_lately(record):
        return "the same device and cause were looked at in the last day"
    return None


SETTLE = 30  # the run is saved just after it finishes; look once it is


def after_run(record: dict) -> None:
    """Called when a run is finished. Queues it for the worker, which decides whether it is worth a look."""
    ev, r, ai = record["event"], record["result"], record.get("agent") or {}
    if _RUNNING["on"] and ev.get("source") != "monitor" and (r["kind"] in ("cause", "escalate") or ai.get("outcome") in ("fixed", "workaround", "partly", "needs_person")):
        QUEUE.put((record["id"], time.time() + SETTLE))


def _save(run_id: str, started: float, s: Session | None, status: str, model: str, error: str = "") -> None:
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO monitor_sessions VALUES (?,?,?,?,?,?,?,?,?)",
                   (run_id, started, time.time(), model, status, ((s.result or {}).get("summary") if s else "") or "", json.dumps(s.calls if s else []), json.dumps(s.proposals if s else []), error))


def suggest(record: dict, log=print, model: str | None = None) -> dict:
    """One suggestion session for one run. Returns what it did."""
    model = model or aiconfig.choice("monitors").model
    started = time.time()
    s = Session(record, model)
    _save(record["id"], started, s, "working", model)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": brief(record)}]
    error, nudges = "", 0
    llm.start_conversation()
    try:
        while s.result is None and len(s.calls) < MAX_CALLS and time.time() - started < MAX_SECONDS:
            reply = llm.chat(messages, DEFINITIONS, model=model, role="monitors", max_tokens=4000)
            messages.append(reply["message"])
            if not reply["calls"]:
                nudges += 1
                if nudges > 2:
                    error = "The AI stopped calling tools without finishing."
                    break
                messages.append({"role": "user", "content": "Continue by calling a tool. When you are done, call finish."})
                continue
            for item in reply["calls"]:
                result = call(s, item["name"], item["args"])
                messages.append({"role": "tool", "tool_call_id": item["id"], "content": json.dumps(result, default=str)[:12000]})
    except llm.LLMError as exc:
        error = f"The model stopped answering: {exc}"
    finally:
        llm.end_conversation()
    if s.result is None and not error:
        error = "It ran out of time or tool calls."
    status = "done" if s.result else "failed"
    _save(record["id"], started, s, status, model, error)
    log(f"{record['id']}: monitor suggestions {status}: {len(s.proposals)} suggested" + (f"; {error}" if error else "") + (f"; {s.result['summary']}" if s.result else ""))
    return {"status": status, "proposals": s.proposals, "calls": s.calls, "result": s.result, "error": error, "model": model}


def work_forever(log=print) -> None:
    _RUNNING["on"] = True
    while True:
        run_id, due = QUEUE.get()
        time.sleep(max(0.0, due - time.time()))
        record = store.load(run_id)
        if not record or done_for(run_id):
            continue
        why_not = skip_reason(record)
        if why_not:
            log(f"{run_id}: no monitor suggestions: {why_not}")
            continue
        try:
            suggest(record, log)
        except Exception as exc:  # one bad session must not stop the next
            log(f"{run_id}: monitor suggestions failed: {type(exc).__name__}: {exc}")
            _save(run_id, time.time(), None, "failed", "", f"{type(exc).__name__}: {exc}")


def start(log=print) -> None:
    _RUNNING["on"] = True
    threading.Thread(target=work_forever, args=(log,), daemon=True).start()


def sessions(limit: int = 20) -> list[dict]:
    with _db() as db:
        rows = db.execute("SELECT * FROM monitor_sessions ORDER BY time DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "calls": json.loads(r["calls"] or "[]"), "proposals": json.loads(r["proposals"] or "[]")} for r in rows]
