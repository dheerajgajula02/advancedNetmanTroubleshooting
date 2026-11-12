"""After rules the AI wrote have run: is the trouble still there?

A short look by a model that cannot change anything, before the support agent (ts/agent.py) is
started. The rules the AI wrote are the predictable path for something seen before; this check
decides whether that path was enough. Working means the run ends there. Broken or unsure means
the support agent takes over. Every step is recorded on the run as record["check"].
"""

from __future__ import annotations

import json
import time

from ts import aiconfig, llm, scrub, tools

ROLE = "check"
MAX_READS = 8
MAX_SECONDS = 240
MAX_PER_HOUR = 20  # past this, the check is skipped and the support agent's own limits apply
VERDICTS = ("working", "broken", "unsure")
_DONE: list[float] = []

SYSTEM = """You check whether a problem on a production network is still happening. You cannot change anything, and finding the cause is not your job: automated rules already looked, and what they found is in your brief. Answer one question: is the trouble still there?

- working: what went wrong has stopped and stayed stopped. For something that comes and goes, that takes evidence over the last ten minutes or so: how long a link or neighbor has been up, that it has not dropped again since, that traffic flows. One good test at one moment proves little about something that flaps.
- broken: it is still happening, it came back recently, or traffic still fails.
- unsure: you could not tell. Say what you could not see.

Use the check_* tools and show, at most {reads} of them, then call verdict. Every call except verdict takes a reason: one sentence on what you expect it to tell you. Never invent a result."""

_VERDICT = {"type": "function", "function": {"name": "verdict", "description": "Your answer. Call it exactly once, last.", "parameters": {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": list(VERDICTS)},
    "summary": {"type": "string", "description": "One or two plain sentences: is it still happening, and what shows it"},
    "evidence": {"type": "string", "description": "The readings that decided it, with the times they cover"}}, "required": ["verdict", "summary", "evidence"]}}}


def wanted(record: dict) -> bool:
    """A run whose rules include one the AI wrote, raised by an alarm or a monitor. A person who wrote in has already said it is broken."""
    return bool(record.get("learned")) and record["event"].get("source") != "ticket"


def _lines(steps: list[dict]) -> list[str]:
    return [f"  {s.get('n')}. {s.get('ask')} -> {s.get('outcome')}: {str(s.get('saw') or '')[:300]}" for s in steps]


def brief(record: dict) -> str:
    ev, out = record["event"], []
    out += ["THE ALARM", f"{ev.get('title') or ev.get('event')} on {ev.get('device')}" + (f" {ev['interface']}" if ev.get("interface") else "") + f", at {time.strftime('%H:%M:%S', time.localtime(float(ev['time'])))}. It is now {time.strftime('%H:%M:%S')}."]
    if ev.get("detail"):
        out.append(str(ev["detail"])[:1500])
    before = record.get("before_rules") or {}
    if before:
        look = (before.get("look") or {}).get("changed") or {}
        out += ["", "FIRST LOOK, before the rules", look.get("summary") or "No comparison with known-good was possible."]
        out += [f"- Put {r['device']} back to known-good: {r.get('status')}. {r.get('why') or ''}".rstrip() for r in before.get("repairs") or []]
    out += ["", "WHAT THE RULES FOUND"]
    for t in [*(record.get("tried") or []), {"rule": record["rule"]["id"], "mode": record.get("mode"), "source": record["rule"].get("source"), "result": record.get("watch_only_result") or record["result"], "steps": record["steps"]}]:
        r = t["result"]
        out.append(f"- {t['rule']}" + (" (written by the AI)" if t.get("source") == "ai" else "") + (" (watch-only)" if t.get("mode") == "shadow" else "") + f": {r.get('text') or r.get('label') or r['kind']}")
        out += _lines(t.get("steps") or [])
    fix = record.get("repair")
    if fix:
        out += ["", "THE REPAIR", f"{fix.get('title') or fix.get('rule')}: {fix.get('status')}. {fix.get('why') or ''}".rstrip()]
    return scrub.text("\n".join(out))[:12000]


def _definitions() -> list[dict]:
    reads = [t for t in tools.definitions() if t["function"]["name"] == "show" or t["function"]["name"].startswith("check_")]
    return [*reads, _VERDICT]


def _recently() -> int:
    now = time.time()
    _DONE[:] = [t for t in _DONE if now - t < 3600]
    return len(_DONE)


def run(record: dict, rules, log=print, root=None) -> dict:
    """Look, decide, and record it on the run. Never raises: a check that could not decide says unsure."""
    started = time.time()
    if _recently() >= MAX_PER_HOUR:
        record["check"] = {"status": "skipped", "verdict": "unsure", "summary": f"{MAX_PER_HOUR} checks ran in the last hour, so this one went straight on.", "started": started, "finished": time.time(), "calls": []}
        return record["check"]
    _DONE.append(started)
    chosen = aiconfig.choice(ROLE)
    session = tools.Session(record, rules, **({"root": root} if root else {}), model=chosen.model)
    prompt = brief(record)
    messages = [{"role": "system", "content": SYSTEM.format(reads=MAX_READS)}, {"role": "user", "content": prompt}]
    definitions, answer, error, nudges = _definitions(), None, "", 0
    record["check"] = {"status": "working", "model": chosen.model, "started": started, "calls": session.calls, "prompt": prompt}
    llm.start_conversation()
    try:
        while answer is None and time.time() - started < MAX_SECONDS:
            reply = llm.chat(messages, definitions, role=ROLE, timeout=120)
            messages.append(reply["message"])
            if not reply["calls"]:
                nudges += 1
                if nudges > 2:
                    error = "The model stopped without giving a verdict."
                    break
                messages.append({"role": "user", "content": "Continue by calling a tool. When you can tell, call verdict."})
                continue
            for item in reply["calls"]:
                args = item["args"] if isinstance(item["args"], dict) else {}
                if item["name"] == "verdict":
                    if args.get("verdict") in VERDICTS:
                        answer = args
                        given = {"outcome": "done"}
                    else:
                        given = {"outcome": "invalid", "saw": f"verdict must be one of {', '.join(VERDICTS)}."}
                elif sum(c["kind"] == "read" for c in session.calls) >= MAX_READS:
                    given = {"outcome": "refused", "saw": f"You have used all {MAX_READS} reads. Call verdict now."}
                elif item["name"] != "show" and not item["name"].startswith("check_"):
                    given = {"outcome": "invalid", "saw": "Only check_* tools, show and verdict are available here."}
                else:
                    given = tools.call(session, item["name"], args)
                messages.append({"role": "tool", "tool_call_id": item["id"], "content": json.dumps(given)[:8000]})
                if answer:
                    break
    except llm.LLMError as exc:
        error = f"The model stopped answering: {exc}"
        log(f"{record['id']}: AI check: {error}")
    llm.end_conversation()
    if answer is None and not error:
        error = "The check ran out of time."
    answer = answer or {"verdict": "unsure", "summary": error, "evidence": ""}
    record["check"] = scrub.deep({"status": "done" if not error else "failed", "verdict": answer["verdict"], "summary": answer.get("summary", ""), "evidence": answer.get("evidence", ""),
                                  "model": chosen.model, "started": started, "finished": time.time(), "calls": session.calls, "prompt": prompt, "error": error})
    return record["check"]
