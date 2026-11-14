"""What happens once the rules have reached the end of an investigation.

Repair with the rules, gather what else was going on, compare with every earlier run.
If the rules could not resolve it, the AI takes over. Then the model's read and the
email. Each part can fail by itself without losing the run or stopping the others.
"""

from __future__ import annotations

import time

from ts import agent, aicheck, analyst, capture, cards, firstlook, fixer, history, memory, monitor_ai, report, scrub, sources, tags
from ts.ruleset import DEFAULT_RULES_DIR

KEEP_DOCS = 60  # how many context items stay on the run for the UI


def _safely(record: dict, log):
    def safely(name, work, default=None):
        try:
            return work()
        except Exception as exc:  # the investigation itself already succeeded, so keep what we have
            log(f"{record['id']}: {name} failed: {type(exc).__name__}: {exc}")
            return default
    return safely


FIRST_LOOK_FRESH = 60  # seconds a comparison of every device's config is reused, for alarms that come in a burst


def _put_back(device: str, event: dict, rules, run_id: str, log) -> dict:
    """Return one drifted device to known-good with the usual repair, as its own build."""
    sub = {"id": run_id, "event": {**event, "device": device}, "values": {"device": device}, "steps": [],
           "result": {"kind": "cause", "label": "config.drift", "params": {"device": device}}}
    values = fixer.fix_values(sub)
    rule = fixer.find_rule(rules, sub, values)
    if rule is None:
        return {"device": device, "status": "none", "why": "No repair rule puts this back."}
    return {"device": device, **fixer.APPLY(sub, rule, values, log)}


def before_rules(event: dict, rules, run_id: str, log=print) -> dict:
    """What a run takes before the rules the AI wrote: has anything moved from known-good? Whatever has, nothing on record
    explains and nothing holds on purpose, goes back first, so those rules start from the network as it is meant to be."""
    look = firstlook.take(event, None, run_id, fresh_within=FIRST_LOOK_FRESH)
    drift = list((look.get("changed") or {}).get("drift") or [])
    repairs = []
    for device in drift:
        try:
            repairs.append(_put_back(device, event, rules, run_id, log))
        except Exception as exc:  # the rules still run; the AI sees the drift in its first look
            repairs.append({"device": device, "status": "failed", "why": f"{type(exc).__name__}: {exc}"})
    if any(r.get("status") == "fixed" for r in repairs):
        firstlook.forget_recent()
    return scrub.deep({"time": time.time(), "look": look, "drift": drift, "repairs": repairs})


def tag(record: dict, source: str = "auto") -> list[str]:
    for name in tags.for_record(record):
        history.tag_run(record["id"], name, source)
    record["tags"] = history.tags_of(record["id"])
    return record["tags"]


def finish(record: dict, rules, log=print, inline: bool = True, root=DEFAULT_RULES_DIR) -> None:
    """Everything up to the hand-over. With inline False, a run the AI should take is left marked queued for the caller to start."""
    safely = _safely(record, log)
    repair = safely("repair", lambda: fixer.attempt(record, rules, log))
    if repair:
        record["repair"] = repair
    docs, status = safely("sources", lambda: sources.gather(record["event"]), ([], {}))
    record["sources"] = status
    record["context"] = docs[-KEEP_DOCS:]
    similar = safely("history", lambda: history.similar(record, involved=memory.involved(record)), []) or []
    record["similar"] = [{"ref": f"R{n}", **{k: s[k] for k in ("id", "time", "device", "subject", "result_kind", "label", "text")}} for n, s in enumerate(similar, 1)]
    record["retrieved"], record["retrieval"] = safely("memory", lambda: memory.recall(record), ([], None))
    record["pattern"] = safely("pattern", lambda: history.pattern(record, docs), {"verdict": "none", "note": "", "count_24h": 1, "count_7d": 1})
    safely("index", lambda: history.add_run(record))
    safely("index", lambda: history.add_docs(docs))
    safely("tags", lambda: tag(record))
    safely("past runs", lambda: cards.prepare(record))
    why = safely("handover", lambda: agent.needed(record))
    checking = aicheck.wanted(record)
    if not why and checking and _learned_repair_worked(record, rules):
        why = "A repair the AI wrote ran. The AI checks that the trouble is really gone."
    if not why and checking and _repair_only_watched(record):
        why = "The rules found the cause, but its repair is in observe-only mode, so nothing was changed. The AI checks whether it is still broken."
    if why:
        # With rules the AI wrote, the AI checks first whether it is still broken; the support agent's limits apply after that.
        stopped = None if checking else safely("handover", lambda: agent.blocked(record), "The AI could not be started.")
        if stopped:
            record["agent"] = {"status": "skipped", "why": why, "note": stopped, "outcome": "needs_person", "calls": [], "changes": [], "proposals": []}
        else:
            record["agent"] = {"status": "queued", "why": why, "calls": [], "changes": [], "proposals": []}
            if inline:
                take_over(record, rules, log, root)
            return
    conclude(record, log)


def _learned_repair_worked(record: dict, rules) -> bool:
    fix = record.get("repair") or {}
    return fix.get("status") == "fixed" and any(r.id == fix.get("rule") and r.source.by == "ai" for r in rules)


def _repair_only_watched(record: dict) -> bool:
    """The rules found a cause a machine can fix, and its repair is observe-only, so the network was left as it was.
    After rules the AI wrote, that still goes to the AI check: a known cause with nothing done about it is not dealt with."""
    r, fix = record["result"], record.get("repair") or {}
    if record["event"].get("change_running") or fix.get("expected"):
        return False
    return r["kind"] == "cause" and not agent.PERSON_ONLY.match(r.get("label") or "") and fix.get("status") == "would_fix"


def _check_first(record: dict, rules, log, root) -> bool:
    """The AI's check, for a run with rules the AI wrote. True when the support agent should go on to work it."""
    safely = _safely(record, log)
    found = safely("ai check", lambda: aicheck.run(record, rules, log, root)) or {"verdict": "unsure", "summary": "The check crashed."}
    log(f"{record['id']}: AI check says {found['verdict']}: {found.get('summary', '')}")
    why = (record.get("agent") or {}).get("why", "")
    if found["verdict"] == "working":
        if (record.get("repair") or {}).get("status") == "fixed":
            record.pop("agent", None)  # the rules' repair did it; the run reads, and is reported, as fixed by the rules
        else:
            record["agent"] = {"status": "not_needed", "why": why, "started": found.get("started"), "finished": time.time(), "calls": [], "changes": [], "proposals": [],
                               "outcome": "nothing_wrong", "note": f"The AI check found it working, so the support agent was not started. {found.get('summary', '')}".strip()}
        return False
    stopped = safely("handover", lambda: agent.blocked(record), "The AI could not be started.")
    if stopped:
        record["agent"] = {"status": "skipped", "why": why, "note": stopped, "outcome": "needs_person", "calls": [], "changes": [], "proposals": []}
        return False
    return True


def check_pending(record: dict) -> bool:
    """A run with rules the AI wrote whose check has not given its answer yet."""
    return aicheck.wanted(record) and (record.get("check") or {}).get("status") in (None, "working")


def take_over(record: dict, rules, log=print, root=DEFAULT_RULES_DIR, saved=lambda record: None) -> None:
    """The AI's turn, then the report. With rules the AI wrote, the AI first checks whether it is still broken.
    saved is called whenever the run has moved on, so a page following it sees each part as it happens."""
    safely = _safely(record, log)
    if check_pending(record):
        go_on = _check_first(record, rules, log, root)
        safely("save", lambda: saved(record))
        if not go_on:
            conclude(record, log)
            return
        log(f"{record['id']}: the AI is taking over: {record['agent'].get('why')}")
    record.setdefault("agent", {"status": "queued", "why": "", "calls": [], "changes": [], "proposals": []})["status"] = "working"
    safely("save", lambda: saved(record))
    done = safely("ai", lambda: agent.take_over(record, rules, log, root=root) or True)
    if not done:
        record["agent"] = {**record.get("agent", {}), "status": "failed", "error": "The AI step crashed."}
    conclude(record, log)


def conclude(record: dict, log=print) -> None:
    safely = _safely(record, log)
    if report.should_send(record):
        if not record.get("analysis"):
            analysis = safely("analysis", lambda: analyst.analyse(record, record.get("context") or [], record.get("similar") or [], record.get("pattern") or {}, record.get("retrieved") or []))
            if analysis:
                record["analysis"] = scrub.deep(analysis)
            else:
                record["ai_note"] = "The AI was asked to read this run, but it gave no usable answer."
        record["email"] = safely("email", lambda: report.send(record), {"sent": False, "why": "the mailer failed"})
    else:
        record["ai_note"] = "Nothing here needed a report, so the AI was not asked. Rules and checks did all of it."
    record["concluded"] = time.time()
    safely("index", lambda: history.add_run(record))
    safely("past runs", lambda: cards.remember_soon(record))
    safely("monitor ideas", lambda: monitor_ai.after_run(record))
    safely("ticketing systems", lambda: __import__("ts.connectors.sync", fromlist=["after_run"]).after_run(record))
    capture.run(record)
