"""Ask a language model to read everything found so far and say what it adds up to."""

from __future__ import annotations

import time

from ts import aiconfig, brain, capture, cards, llm, scrub
from ts.blocks.services import AUTOMATION_SOURCE, PERSON_SOURCE

SYSTEM = """You are a senior network engineer reviewing an automated investigation of an incident on a production network.
You get what triggered it, each check that ran, the conclusion, anything done to repair it, what else was happening around that time, and how often this has happened before.

Reply with one JSON object and nothing else:
{"summary": "two or three plain sentences a tired on-call engineer can act on",
 "related": ["only items from the context that really bear on this incident, each one short"],
 "assessment": "one of: none, scheduled_job, bad_hardware, tampering, misconfiguration, software, unknown",
 "why": "one sentence giving the evidence for that assessment",
 "next": "what a person should do next, or 'nothing' if it is already resolved and nothing suggests it will return",
 "used": ["the [R1] or [M1] labels of the older records you actually relied on, or an empty list"]}

Rules: use only what you are given, and never invent an event, a person or a cause. The CONCLUSION comes from checks run on the devices, so treat it as established and do not contradict it.
SIMILAR PAST RUNS and FROM MEMORY are older records found by keyword search, so some will be irrelevant. Ignore those. A shared device name alone is not a connection. When one really does change the picture, say so in the summary and list its label under "used".
Choosing the assessment:
- scheduled_job only if HISTORY says it recurs at the same time on different days, or a job named in the context plainly made this exact change. A job merely running nearby is not evidence.
- tampering only if the CONCLUSION is a config change or shutdown made by a person or unknown source, and HISTORY shows it repeating or the context shows suspicious access. A single hand change with no such sign is misconfiguration.
- bad_hardware if the CONCLUSION is a link, port or cable fault, or packet loss, with no config change behind it. It does not need to have repeated.
- software if a service or process died by itself.
- none if it cleared by itself and nothing suggests it will return. unknown if the evidence really does not say.
Several unrelated faults can be happening at once, so an event elsewhere is not related unless the evidence connects them.
""" + f"""Logins and commands from {AUTOMATION_SOURCE} are the automation server doing routine work, including running nat.sh, so they are not suspicious and not evidence of who changed something. {PERSON_SOURCE} is the console people use."""


def _clock(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def _day(t: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t))


def _remembered(item: dict) -> str:
    seen = f" (seen {item['times_seen']} times, last {_day(item['last_seen'])})" if item.get("times_seen", 1) > 1 else ""
    return f"[{item['ref']}] {_day(item['time'])} [{item['source']}] {item['title']}" + (f" | {item['text'][:1500]}" if item.get("text") else "") + seen


def brief(rec: dict, docs: list[dict], similar: list[dict], pattern: dict, memory: list[dict] | None = None) -> str:
    ev, r, fix = rec["event"], rec["result"], rec.get("repair")
    out = [f"INCIDENT at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ev['time']))}", ev.get("title") or f"{ev['device']} {ev.get('interface')} went down",
           ev.get("detail", ""), "", "CHECKS"]
    out += [f"{s['n']}. {s['ask']} -> {s['outcome']}: {s['saw']}" for s in rec["steps"]]
    out += ["", f"CONCLUSION ({r['kind']}{', ' + r['label'] if r.get('label') else ''}): {r.get('text', '')}"]
    if fix:
        out += ["", f"REPAIR: {fix['status']}. {fix.get('why', '')}"] + [f"- {s['ask']}: {s['saw']}" for s in fix.get("steps", [])]
    out += ["", f"HISTORY: {pattern.get('count_24h', 1)} times in 24 hours, {pattern.get('count_7d', 1)} in 7 days. {pattern.get('note', '')}"]
    if similar:
        out += ["SIMILAR PAST RUNS"] + [f"[{s.get('ref', '-')}] {_day(s['time'])} {s['device']} {s['subject']}: {s['result_kind']} {s['text']}" for s in similar[:5]]
    if memory:
        out += ["", "FROM MEMORY"] + [_remembered(item) for item in memory]
    if docs:
        out += ["", "AROUND THE SAME TIME"] + [f"{_clock(d['time'])} [{d['source']}] {d['title']}" + (f" | {d['text'][:500]}" if d.get("text") else "") for d in docs[-50:]]
    past = cards.render(rec)
    if past:
        out += ["", past]
    return scrub.text("\n".join(out))[:capture.BRIEF_LIMIT]


def analyse(rec: dict, docs: list[dict], similar: list[dict], pattern: dict, memory: list[dict] | None = None, model: str | None = None) -> dict | None:
    started, memory = time.time(), memory or []
    prompt = brief(rec, docs, similar, pattern, memory)
    earlier = brain.context_for("notes", f"{rec['event'].get('title')} {rec['result'].get('text') or ''}")
    if earlier:
        prompt = f"{prompt}\n\n{earlier}"
    episode = capture.start("analyst", rec["id"], model or aiconfig.choice("notes").model, {"prompt_cut": capture.was_cut(prompt)})
    capture.inputs(capture.sections(SYSTEM, prompt, [], rec))
    try:
        reply = llm.complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], model=model, max_tokens=8000, timeout=180)
    except Exception:
        capture.close(episode, "failed")
        raise
    parsed = llm.parse_json(reply["text"])
    capture.close(episode, "done" if parsed and "summary" in parsed else "failed", parsed)
    if not parsed or "summary" not in parsed:
        return None
    brain.remember("notes", "read", f"{rec['event'].get('title')}: {parsed['summary']} Next: {parsed.get('next') or 'nothing'}")
    real = {item.get("ref") for item in [*similar, *memory]}
    return {**parsed, "used": [ref for ref in parsed.get("used") or [] if ref in real], "model": reply["model"], "seconds": reply["seconds"], "reasoning": reply["reasoning"][:4000],
            "started": started, "finished": time.time(), "prompt": prompt,
            "given": {"checks": len(rec["steps"]), "repair": bool(rec.get("repair")), "similar": len(similar), "memory": len(memory), "live": len(docs)}}
