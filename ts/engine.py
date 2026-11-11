"""Run a diagnose rule against one event and record every step.

The record is the stored process: for each step, the question, the exact
command or query, what came back, the outcome and what the rule did next.
The UI replays incidents from these records.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from fnmatch import fnmatchcase

from ts import scrub, tags as tag_book
from ts.lab import Result
from ts.schema import PLACEHOLDER, DiagnoseRule, Outcome
from ts.vocab import FORBIDDEN_DEVICES

UNSET = "unknown"


@dataclass
class Context:
    event_time: float
    event: dict = field(default_factory=dict)


def fill(text: str, values: dict, unset: str | None = UNSET) -> str:
    """Replace {name} with its value; unknown ones become `unset`, or stay as written when unset is None."""
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1)) or (m.group(0) if unset is None else unset), text)


def fill_args(args: dict, values: dict, unset: str | None = UNSET) -> dict:
    """The same for a whole set of arguments, some of which may be lists."""
    return {k: [fill(item, values, unset) for item in v] if isinstance(v, list) else fill(v, values, unset) for k, v in args.items()}


def _missing(value, values: dict) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return sorted({n for item in items for n in PLACEHOLDER.findall(item) if not values.get(n)})


def _outcome_view(o: Outcome, rule: DiagnoseRule, values: dict, unset: str | None = UNSET) -> dict:
    view = {"kind": o.kind, "target": o.target, "params": dict(o.params)}
    if o.kind == "cause":
        merged = {**values, **{k: fill(v, values, unset) for k, v in o.params.items()}}
        view["label"] = fill(o.target, values, unset)
        view["text"] = fill(rule.causes.get(o.target, o.target), merged, unset)
    elif o.kind in ("stop", "escalate"):
        view["text"] = fill(o.target, values, unset)
    elif o.kind == "goto":
        view["text"] = f"Continue with rule {o.target}"
    return view


def rule_map(rule: DiagnoseRule, values: dict) -> dict:
    """The rule as a map: every step and every exit, with placeholders filled where known.

    A placeholder nothing has filled stays as {name}, and the UI draws it as a blank.
    """
    unset = None
    steps = []
    for s in rule.steps:
        block = s.check.block
        steps.append({
            "id": s.id, "phase": s.phase, "ask": fill(s.ask or block.description, values, unset),
            "check": s.check.render(), "description": block.description,
            "exits": {name: _outcome_view(o, rule, values, unset) for name, o in s.then.items() if o.kind != "next"},
            "continue_on": [o for o in block.outcomes if o not in s.then or s.then[o].kind == "next"],
        })
    return {"id": rule.id, "version": rule.version, "mode": rule.mode, "description": rule.description,
            "title": rule.title, "trigger": rule.trigger, "steps": steps, "end": _outcome_view(rule.end, rule, values, unset),
            "position": rule.position, "tags": list(rule.tags), "source": rule.source.by}


def run(rule: DiagnoseRule, event: dict) -> dict:
    values = {k: str(v) for k, v in event.items() if isinstance(v, (str, int, float)) and k != "raw"}
    ctx = Context(event_time=float(event["time"]), event=event)
    index = {s.id: i for i, s in enumerate(rule.steps) if s.id}
    records, result = [], None
    started = time.time()
    i = 0
    while i < len(rule.steps):
        step = rule.steps[i]
        block = step.check.block
        args = fill_args(step.check.args, values)
        t0 = time.time()
        missing = _missing([x for v in step.check.args.values() for x in (v if isinstance(v, list) else [v])], values)
        if missing:
            res = Result("error", f"Could not run: {', '.join(missing)} was never found.")
        else:
            try:
                res = block.func(ctx, **args)
            except Exception as exc:  # a failed check is recorded, not fatal
                res = Result("error", f"The check failed: {type(exc).__name__}: {exc}")
        if res.outcome not in block.all_outcomes:
            res = Result("error", f"The check returned {res.outcome!r}, which it does not declare.")
        values.update({k: str(v) for k, v in res.values.items()})
        chosen = step.then.get(res.outcome) or Outcome(kind="next")
        records.append({
            "n": i + 1, "id": step.id, "phase": step.phase,
            "ask": fill(step.ask or block.description, values),
            "check": step.check.render(), "ran_with": args,
            "outcome": res.outcome, "saw": res.saw, "values": res.values,
            "evidence": [e.as_dict() for e in res.evidence],
            "next": _outcome_view(chosen, rule, values),
            "started": t0, "ms": int((time.time() - t0) * 1000),
        })
        if chosen.kind == "next":
            i += 1
        elif chosen.kind == "jump":
            i = index[chosen.target]
        else:
            result = {**_outcome_view(chosen, rule, values), "step": i + 1}
            break
    if result is None:
        result = {**_outcome_view(rule.end, rule, values), "step": "end"}
    return scrub.deep({
        "id": incident_id(event),
        "event": {k: v for k, v in event.items()},
        "rule": rule_map(rule, values),
        "mode": rule.mode,
        "started": started, "finished": time.time(),
        "steps": records, "result": result, "values": values,
    })


def _applies(rule, event: dict) -> bool:
    for key, wanted in rule.match.items():
        patterns = wanted if isinstance(wanted, list) else [wanted]
        if key == "tags":
            if not tag_book.covers(patterns, tag_book.for_event(event.get("event", ""))):
                return False
        elif not any(fnmatchcase(str(event.get(key, "")), p) for p in patterns):
            return False
    return True


def chain_for(rules, event: dict) -> list:
    """Every diagnose rule that runs for this event, in order: those placed at the beginning, the main rule, those placed at the end."""
    if event.get("device") in FORBIDDEN_DEVICES:
        return []
    live = [r for r in sorted(rules, key=lambda r: (-r.priority, r.id)) if r.table == "diagnose" and r.mode not in ("proposed", "retired") and _applies(r, event)]
    main = next((r for r in live if r.position == "main"), None)
    return [r for r in live if r.position == "beginning"] + ([main] if main else []) + [r for r in live if r.position == "end"]


def cleared_at_confirm(record: dict) -> bool:
    """The rule looked and the problem was not there. Nothing further is needed."""
    r = record["result"]
    return r["kind"] == "stop" and isinstance(r.get("step"), int) and record["steps"][r["step"] - 1]["phase"] == "confirm"


def _tried(record: dict) -> dict:
    return {"rule": record["rule"]["id"], "title": record["rule"].get("title"), "position": record["rule"].get("position", "main"), "mode": record["mode"],
            "result": record["result"], "steps": record["steps"]}


def run_chain(chain: list, event: dict) -> dict:
    """Run each rule in turn until one reaches a cause. A rule in shadow runs and is recorded, but decides nothing."""
    attempts, final, main = [], None, None
    for rule in chain:
        record = run(rule, event)
        attempts.append(record)
        main = record if rule.position == "main" else main
        if rule.mode == "shadow" and rule.position != "main":
            continue
        if record["result"]["kind"] == "cause" or (rule.position == "main" and cleared_at_confirm(record)):
            final = record
            break
    final = final or main or attempts[-1]
    final["tried"] = [_tried(r) for r in attempts if r is not final]
    return final


def no_rule(event: dict) -> dict:
    """A record for an event no rule covers, so it can still be looked at."""
    now = time.time()
    values = {k: str(v) for k, v in event.items() if isinstance(v, (str, int, float)) and k != "raw"}
    return scrub.deep({"id": incident_id(event), "event": dict(event), "rule": {"id": "none", "title": "No rule covers this yet", "steps": [], "position": "main", "tags": []},
                       "mode": "none", "started": now, "finished": now, "steps": [], "values": values, "tried": [],
                       "result": {"kind": "stop", "target": "", "params": {}, "text": "No rule covers this kind of problem yet.", "step": "end"}})


def incident_id(event: dict) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(float(event["time"])))
    what = event.get("subject") or event.get("interface") or "device"
    return f"{stamp}-{event['device']}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', what)}"
