"""The parts of one run, in order, as a person follows them: in the UI and as the stages of a Jenkins build.

What started it, the first look at what moved from known-good, each rule that ran, the repair, the AI's
check, the support agent, and the report. Each part is done, failed (it did not work, or found it still
broken), running, waiting (it will come once the part before it is done), or skipped. The stages are
worked out from the run's record alone, so a page or the Jenkins build can follow a run as it is saved.

The Jenkins side (follow, advance) keeps one build of troubleshooting-run per run. The build holds no
executor: each stage waits on an input step, which this process answers once that part of the run is
done and the part after it is known. A run that is still going therefore shows as a running build.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.parse

from ts import jenkins_api, pipelines

AI_MONITOR = "AI monitor"


def _clock(t) -> str:
    return time.strftime("%H:%M:%S", time.localtime(float(t))) if t else ""


def from_ai_monitor(record: dict) -> dict | None:
    """The origin of the monitor that started this run, when the AI suggested that monitor."""
    made = (record.get("event") or {}).get("monitor_origin") or {}
    return made if made.get("by") == "ai" else None


def mark(record: dict) -> str:
    return AI_MONITOR if from_ai_monitor(record) else ""


def badge(record: dict) -> str:
    """One line for the Jenkins build and the top of the run page: what started it."""
    ev = record.get("event") or {}
    made = from_ai_monitor(record)
    if made:
        return (f"Started by monitor {ev.get('monitor')}, which the AI suggested (suggestion #{made.get('proposal')}) and "
                f"{made.get('accepted_by') or 'a person'} accepted. {ev.get('title') or ''}").strip()
    if ev.get("source") == "monitor":
        return f"Started by monitor {ev.get('monitor')}, which a person set up. {ev.get('title') or ''}".strip()
    if ev.get("source") == "ticket":
        return f"Started by ticket {ev.get('ticket') or ''}: {ev.get('title') or ''}".strip()
    return f"Started by an alarm: {ev.get('title') or ev.get('event') or ''}".strip()


def _trigger(record: dict) -> dict:
    ev = record.get("event") or {}
    source = ev.get("source") or "alarm"
    made = from_ai_monitor(record)
    if made:
        label = f"Trigger: AI monitor {ev.get('monitor')}"
    elif source == "monitor":
        label = f"Trigger: monitor {ev.get('monitor')}"
    elif source == "ticket":
        label = f"Trigger: ticket {ev.get('ticket') or ''}".strip()
    else:
        label = f"Trigger: {'alarm' if source in ('trap', 'health', 'events') else source}"
    where = " ".join(str(x) for x in (ev.get("device"), ev.get("interface")) if x)
    text = [badge(record), f"{ev.get('title') or ev.get('event')} on {where or 'the network'}, at {_clock(ev.get('time'))}."]
    if made and made.get("from_run"):
        text.append(f"The AI suggested this monitor while working run {made['from_run']}.")
    return {"key": "trigger", "label": label, "state": "done", "text": "\n".join(text), "ai_monitor": bool(made), "at": ev.get("time")}


def _first_look(record: dict) -> dict | None:
    before = record.get("before_rules")
    if not record.get("learned"):
        return None
    if not before:
        return {"key": "first_look", "label": "First look: known-good", "state": "skipped", "text": "No first look was taken."}
    look = (before.get("look") or {}).get("changed") or {}
    lines = [look.get("summary") or "No comparison with known-good was possible."]
    repairs = before.get("repairs") or []
    lines += [f"Put {r['device']} back to known-good: {r.get('status')}. {r.get('why') or ''}".rstrip() for r in repairs]
    if not before.get("drift"):
        lines.append("Nothing had moved from known-good, so the rules start from the network as it is meant to be.")
    failed = any(r.get("status") not in ("fixed", "already") for r in repairs)
    links = [r["jenkins"]["url"] for r in repairs if (r.get("jenkins") or {}).get("url")]
    return {"key": "first_look", "label": "First look: known-good", "state": "failed" if failed else "done", "text": "\n".join(lines),
            "link": links[0] if links else "", "at": before.get("time")}


def _result_words(result: dict) -> str:
    return result.get("text") or result.get("label") or result.get("kind") or ""


def _rules(record: dict) -> list[dict]:
    out = []
    final = {"rule": record["rule"]["id"], "title": record["rule"].get("title"), "mode": record.get("mode"), "source": record["rule"].get("source"),
             "result": record.get("watch_only_result") or record["result"], "steps": record.get("steps") or [], "jenkins": record.get("jenkins"),
             "position": record["rule"].get("position")}
    tried = {t["rule"]: t for t in record.get("tried") or []}
    order = record.get("order") or [*tried, final["rule"]]
    for rid in order:
        t = final if rid == final["rule"] else tried.get(rid)
        if not t:
            continue
        ai, watch = t.get("source") == "ai", t.get("mode") == "shadow"
        r = t["result"]
        label = f"Rule: {rid}" + (" [AI]" if ai else "") + (" [watch-only]" if watch else "")
        lines = [f"{t.get('title') or rid}" + (" (written by the AI)" if ai else "") + (" (watch-only: it is recorded but decides nothing)" if watch else "")]
        lines += [f"{s.get('n')}. {s.get('ask')} -> {s.get('outcome')}" for s in t.get("steps") or []]
        lines.append(f"Ended: {r.get('kind')}: {_result_words(r)}")
        j = t.get("jenkins") or {}
        if j.get("ran") == "here":
            lines.append(f"Ran in the troubleshooter, not in Jenkins: {j.get('why')}")
        out.append({"key": f"rule:{rid}", "label": label, "state": "done", "text": "\n".join(lines), "link": j.get("url") or "", "ai": ai, "watch_only": watch,
                    "rule": rid, "result": r.get("kind")})
    if record["rule"]["id"] == "none":
        out = [{"key": "rule:none", "label": "Rules: none cover this", "state": "skipped", "text": _result_words(record["result"])}]
    return out


REPAIR_STATES = {"fixed": "done", "already": "done", "held": "skipped", "would_fix": "skipped", "none": "skipped"}


def _repair(record: dict) -> dict | None:
    fix = record.get("repair")
    if not fix:
        return None
    status = fix.get("status") or ""
    lines = [f"{fix.get('title') or fix.get('rule') or 'Repair'}: {status}.", fix.get("why") or ""]
    j = fix.get("jenkins") or {}
    return {"key": "repair", "label": f"Repair: {fix.get('rule') or 'none'}", "state": REPAIR_STATES.get(status, "failed"), "text": "\n".join(x for x in lines if x),
            "link": j.get("url") or "", "status": status}


def _check(record: dict) -> dict | None:
    if not (record.get("learned") and (record.get("event") or {}).get("source") != "ticket"):  # aicheck.wanted, without its imports
        return None
    c = record.get("check") or {}
    ai = record.get("agent")
    if not c and not ai:
        return None  # the rules settled it, so there was nothing to check
    if not c or c.get("status") == "working":
        return {"key": "check", "label": "AI check: is it still broken?", "state": "running" if c else "waiting",
                "text": "The AI is looking at whether the trouble is still there." if c else "Waiting for the AI to look."}
    verdict = c.get("verdict")
    words = {"working": "It is working now.", "broken": "It is still broken.", "unsure": "It could not tell."}.get(verdict, verdict)
    lines = [f"{words} {c.get('summary') or ''}".strip()]
    if c.get("evidence"):
        lines.append(f"Evidence: {c['evidence']}")
    lines.append(f"{len(c.get('calls') or [])} reads with {c.get('model') or 'the model'}" + (f", {c['finished'] - c['started']:.0f} s" if c.get("finished") and c.get("started") else "") + ".")
    if c.get("error"):
        lines.append(c["error"])
    return {"key": "check", "label": "AI check: is it still broken?", "state": "done" if verdict == "working" else "failed", "text": "\n".join(lines), "verdict": verdict}


OUTCOME_WORDS = {"fixed": "The AI fixed it.", "workaround": "The AI put a workaround in place.", "needs_person": "It needs a person.", "nothing_wrong": "Nothing was wrong by the time the AI looked."}


def _agent(record: dict) -> dict | None:
    ai = record.get("agent") or {}
    status = ai.get("status")
    if not status or (status == "not_needed" and record.get("check")):
        return None  # the check found it working, so the support agent never came
    label = "AI support agent"
    if status in ("queued",):
        return {"key": "agent", "label": label, "state": "waiting", "text": f"Waiting for its turn. Why: {ai.get('why') or ''}".strip()}
    if status == "working":
        return {"key": "agent", "label": label, "state": "running", "text": f"Working: {len(ai.get('calls') or [])} steps so far. Why: {ai.get('why') or ''}".strip()}
    if status in ("skipped", "not_needed"):
        return {"key": "agent", "label": label, "state": "skipped", "text": ai.get("note") or ai.get("error") or status}
    outcome = ai.get("outcome") or ""
    lines = [OUTCOME_WORDS.get(outcome, outcome or status), f"Why it was called: {ai.get('why') or ''}"]
    if (record.get("analysis") or {}).get("summary"):
        lines.append(record["analysis"]["summary"])
    lines.append(f"{len(ai.get('calls') or [])} steps, {len(ai.get('changes') or [])} changes, {len(ai.get('proposals') or [])} suggestions" + (f", with {ai['model']}" if ai.get("model") else "") + ".")
    if ai.get("error"):
        lines.append(ai["error"])
    good = status == "done" and outcome in ("fixed", "workaround", "nothing_wrong")
    return {"key": "agent", "label": label, "state": "done" if good else "failed", "text": "\n".join(lines), "outcome": outcome}


def _report(record: dict) -> dict:
    label = "Report"
    if not (record.get("concluded") or "email" in record or "ai_note" in record):  # runs from before "concluded" was kept have one of these
        return {"key": "report", "label": label, "state": "waiting", "text": "Comes once the parts before it are done."}
    mail = record.get("email") or {}
    if mail.get("sent"):
        text = f"Emailed: {mail.get('subject') or 'the report'}."
    elif mail:
        text = f"The email did not go: {mail.get('why') or 'no reason given'}."
    else:
        text = record.get("ai_note") or "Nothing here needed a report."
    return {"key": "report", "label": label, "state": "failed" if mail and not mail.get("sent") else "done", "text": text}


def of(record: dict) -> list[dict]:
    """Every part of the run, in order, with unique labels."""
    parts = [_trigger(record), _first_look(record), *_rules(record), _repair(record), _check(record), _agent(record), _report(record)]
    out, seen = [], set()
    for p in parts:
        if not p:
            continue
        label, n = p["label"], 2
        while label in seen:
            label, n = f"{p['label']} ({n})", n + 1
        seen.add(label)
        out.append({**p, "label": label[:120]})
    return out


SETTLED = ("done", "failed", "skipped")


def ready(parts: list[dict]) -> int:
    """How many stages can be told to Jenkins: the ones done so far, in order. The part after a done one is always already
    known (the report is always last, and it is done only once everything is), so each can name the stage that follows."""
    n = 0
    while n < len(parts) and parts[n]["state"] in SETTLED:
        n += 1
    return n


# --- the Jenkins build that follows a run ----------------------------------------------

ENABLED = False          # set by the watcher, so tests and one-off commands leave Jenkins alone
START_WITHIN = 20        # seconds Jenkins has to start the build
INPUT_TRIES = 40         # a stage's input appears a moment after the one before it is answered
_WORK: queue.Queue = queue.Queue()
_TOLD: dict[str, int] = {}   # run id -> how many stages Jenkins has been told
_LOG = print
_THREAD: threading.Thread | None = None
_DOWN = {"until": 0.0}       # after Jenkins would not start a build, runs do not wait on it again for a while
DOWN_FOR = 300


def _submit(number: int, stage_no: int, part: dict, nxt: str) -> None:
    saw = (part.get("text") or "") + (f"\nIts own build: {part['link']}" if part.get("link") else "")
    params = [{"name": "SAW", "value": saw}, {"name": "STATE", "value": "failed" if part["state"] == "failed" else "done"},
              {"name": "NEXT", "value": nxt}, {"name": "BADGE", "value": ""}]
    body = urllib.parse.urlencode({"json": json.dumps({"parameter": params}), "proceed": "Done"}).encode()
    path = f"{jenkins_api.job_path(pipelines.RUN_PIPELINE)}/{number}/input/Stage{stage_no}/submit"
    for _ in range(INPUT_TRIES):
        if jenkins_api._request("POST", path, body, "application/x-www-form-urlencoded", want="text") is not None:
            return
        time.sleep(0.5)  # a 404, which comes back as None: the build has not reached this stage's input yet
    raise jenkins_api.Unavailable(f"build #{number} never reached stage {stage_no}")


def _work_forever() -> None:
    while True:
        run_id, number, parts, told = _WORK.get()
        try:
            told = max(told, _TOLD.get(run_id, 0))
            upto = ready(parts)
            while told < upto:
                nxt = parts[told + 1]["label"] if told + 1 < len(parts) else ""
                _submit(number, told + 1, parts[told], nxt)
                told += 1
                _TOLD[run_id] = told
        except Exception as exc:  # the run goes on without its Jenkins view
            _LOG(f"{run_id}: the Jenkins run view stopped: {type(exc).__name__}: {exc}")
            _TOLD[run_id] = 10_000


def _start(record: dict, parts: list[dict]) -> dict:
    try:
        queue_url = jenkins_api.start(pipelines.RUN_PIPELINE, {"RUN": record["id"], "FIRST": parts[0]["label"], "MARK": mark(record), "BADGE": badge(record)[:300]})
        number, why = jenkins_api.wait_started(queue_url, START_WITHIN)
    except jenkins_api.Unavailable as exc:
        return {"pipeline": pipelines.RUN_PIPELINE, "error": str(exc)}
    if not number:
        jenkins_api.cancel(queue_url)
        return {"pipeline": pipelines.RUN_PIPELINE, "error": why}
    return {"pipeline": pipelines.RUN_PIPELINE, "number": number, "url": jenkins_api.link(pipelines.RUN_PIPELINE, number), "started": time.time()}


def advance(record: dict) -> None:
    """Bring the run's Jenkins build up to where the run is. Starts the build the first time. Never raises."""
    global _THREAD
    if not ENABLED:
        return
    try:
        parts = of(record)
        info = record.get("jenkins_run")
        if info is None:
            if time.time() < _DOWN["until"]:
                record["jenkins_run"] = {"pipeline": pipelines.RUN_PIPELINE, "error": "Jenkins would not start a build a few minutes ago."}
                return
            info = record["jenkins_run"] = _start(record, parts)
            if info.get("number"):
                _LOG(f"{record['id']}: following in Jenkins as {pipelines.RUN_PIPELINE} #{info['number']}")
            else:
                _DOWN["until"] = time.time() + DOWN_FOR
                _LOG(f"{record['id']}: no Jenkins run view: {info.get('error')}")
        if not info.get("number"):
            return
        if _THREAD is None:
            _THREAD = threading.Thread(target=_work_forever, daemon=True)
            _THREAD.start()
        _WORK.put((record["id"], info["number"], parts, int(info.get("told") or 0)))
        info["told"] = max(int(info.get("told") or 0), _TOLD.get(record["id"], 0))
    except Exception as exc:  # the run matters more than its view
        _LOG(f"{record.get('id')}: the Jenkins run view failed: {type(exc).__name__}: {exc}")


def enable(log=print) -> None:
    global ENABLED, _LOG
    ENABLED, _LOG = True, log
