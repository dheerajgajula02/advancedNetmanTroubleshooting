"""The email: what happened, what was tried, what it came to, and what happened after."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from ts import scrub

MAILER_URL = os.environ.get("MAILER_URL", "http://127.0.0.1:8025/send")
TOKEN_FILE = Path(os.environ.get("MAILER_TOKEN_FILE", "/opt/lab3/mailer.token"))
STATE = Path(os.environ.get("TS_MAIL_STATE", Path(__file__).resolve().parent.parent / "data" / "mail-state.json"))
REPLAY = os.environ.get("TS_REPLAY_URL", "http://10.9.8.182:8770/#/incident/")
REVIEW = REPLAY.split("#")[0] + "#/review"
MAX_PER_HOUR = 20
DAY = 86400
QUIET_LABELS = {"config.changed_by_automation", "ospf.link_down"}  # automation's own work, and runs that only point at another run
LOUD_PATTERNS = {"tampering", "bad_hardware"}


def _when(t: float) -> str:
    return time.strftime("%-I:%M:%S %p %Z on %b %-d", time.localtime(t))


def _pattern_key(rec: dict) -> str:
    return f"{rec['pattern']['verdict']}:{rec['event']['device']}:{rec['event'].get('subject')}"


def should_send(rec: dict) -> bool:
    r, fix, pattern, ai = rec["result"], rec.get("repair"), rec.get("pattern") or {}, rec.get("agent") or {}
    loud = pattern.get("verdict") in LOUD_PATTERNS and time.time() - _state()["keys"].get(_pattern_key(rec), 0) >= DAY
    if rec["event"].get("change_running"):
        return False  # a job was changing the network when it happened, and it reports its own failures
    if fix and fix.get("expected"):
        # Held because a deployment had just finished or another job was in the way. That is usually the job's
        # doing, but tampering or failing hardware still needs a person to hear about it (once a day per port).
        return loud
    if ai.get("status") == "not_needed":
        return False  # it cleared by itself, or another run already sorted it out
    if ai.get("status") in ("done", "failed", "skipped"):
        return True
    if fix:
        return fix["status"] != "already"
    if r["kind"] in ("cause", "escalate") and r.get("label") not in QUIET_LABELS:
        return True
    return loud


def tag(rec: dict) -> str:
    r, fix, verdict, ai = rec["result"], rec.get("repair"), (rec.get("pattern") or {}).get("verdict"), rec.get("agent") or {}
    if ai.get("status") == "done" and ai.get("outcome") == "fixed":
        return "FIXED BY AI"
    if ai.get("status") in ("done", "failed", "skipped") and ai.get("outcome") != "nothing_wrong":
        return "NEEDS YOU"
    if verdict == "tampering":
        return "SUSPICIOUS"
    if verdict == "bad_hardware":
        return "BAD HARDWARE"
    if fix and fix["status"] == "fixed":
        return "FIXED"
    if fix and fix["status"] == "failed":
        return "FIX FAILED"
    if verdict == "repeating":
        return "REPEATING"
    return "NEEDS YOU" if r["kind"] in ("cause", "escalate") else "INFO"


def compose(rec: dict) -> tuple[str, str]:
    ev, r, fix = rec["event"], rec["result"], rec.get("repair")
    pattern, analysis, ai = rec.get("pattern") or {}, rec.get("analysis") or {}, rec.get("agent") or {}
    title = ev.get("title") or f"{ev['device']} {ev.get('interface')} went down"
    out = ["WHAT HAPPENED", f"{title}, at {_when(ev['time'])}."]
    if ev.get("detail"):
        out.append(ev["detail"])
    out += ["", "WHAT I CHECKED"]
    for s in rec["steps"]:
        out.append(f"{s['n']}. {s['ask']}")
        out.append(f"   {s['saw']}")
        if s.get("evidence"):
            out.append(f"   Ran: {s['evidence'][0]['ran']}")
    out += ["", "WHAT IT CAME TO", r.get("text") or r.get("label") or r["kind"]]
    out += ["", "WHAT I DID ABOUT IT"]
    if fix:
        for i, s in enumerate((x for x in fix.get("steps", []) if x.get("kind") != "require"), 1):
            out.append(f"{i}. {s['ask']}")
            out.append(f"   {s['saw']}")
        out.append({"fixed": "It is back to its known-good state, and I checked that it stayed that way.", "failed": "That did not work, so I undid it. A person needs to look.",
                    "held": "I did not change anything.", "would_fix": "This fix is in observe-only mode, so I did not change anything.",
                    "already": "Nothing needed changing, it was already back."}.get(fix["status"], ""))
        if fix.get("why") and fix["status"] != "fixed":
            out.append(fix["why"])
    else:
        out.append("Nothing. No automatic fix exists for this, so it needs a person.")
    if ai.get("status") in ("done", "failed"):
        out += ["", "WHAT THE AI DID", f"(The rules could not resolve this, so {ai.get('model', 'the AI')} took over at {time.strftime('%-I:%M:%S %p', time.localtime(ai['started']))}. {ai.get('why', '')})"]
        if ai.get("root_cause"):
            out.append(f"It found: {ai['root_cause']}")
        out.append(f"It ran {len(ai.get('calls') or [])} steps.")
        for change in ai.get("changes") or []:
            out.append(f"- {change['status'].upper()} on {change['device']}: {'; '.join(change['what'])}")
            out.append(f"  {change['saw']}")
        if ai.get("proposals"):
            out.append(f"It suggested {len(ai['proposals'])} thing(s) for you to accept or reject: {REVIEW}")
        if ai.get("error"):
            out.append(ai["error"])
    elif ai.get("status") == "skipped":
        out += ["", "WHAT THE AI DID", f"Nothing. {ai.get('note', '')}"]
    if pattern.get("note") or pattern.get("count_7d", 1) > 1:
        out += ["", "HAS THIS HAPPENED BEFORE", pattern.get("note") or f"{pattern['count_7d']} times in the last 7 days."]
    if analysis:
        at = f" at {time.strftime('%-I:%M:%S %p', time.localtime(analysis['started']))}" if analysis.get("started") else ""
        out += ["", "WHAT I MAKE OF IT", f"(The AI starts here. {analysis.get('model', 'The model')} read this run{at}. Everything above came from rules and checks on the devices.)", analysis.get("summary", "")]
        if analysis.get("why"):
            out.append(analysis["why"])
        if analysis.get("next") and analysis["next"].lower() != "nothing":
            out += ["", "WHAT YOU SHOULD DO", analysis["next"]]
        if analysis.get("related"):
            out += ["", "WHAT ELSE WAS GOING ON"] + [f"- {x}" for x in analysis["related"]]
        recalled = [m for m in rec.get("retrieved") or [] if m["ref"] in (analysis.get("used") or [])]
        if recalled:
            out += ["", "WHAT IT FOUND IN MEMORY"] + [f"- {time.strftime('%b %-d %-I:%M %p', time.localtime(m['time']))}, {m['source']}: {m['title']}" for m in recalled]
    out += ["", "SEE THE WHOLE RUN", REPLAY + rec["id"]]
    return scrub.text(f"[{tag(rec)}] {title}")[:200], scrub.text("\n".join(out))


def _state() -> dict:
    try:
        saved = json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        saved = {}
    return {"sent": [t for t in saved.get("sent", []) if t >= time.time() - 3600],
            "keys": {k: t for k, t in saved.get("keys", {}).items() if t >= time.time() - DAY}}


def send(rec: dict) -> dict:
    state = _state()
    if len(state["sent"]) >= MAX_PER_HOUR:
        return {"sent": False, "why": f"{MAX_PER_HOUR} emails went out in the last hour, so this one was held back"}
    subject, body = compose(rec)
    request = urllib.request.Request(MAILER_URL, data=json.dumps({"subject": subject, "body": body}).encode(),
                                     headers={"Content-Type": "application/json", "X-Mailer-Token": TOKEN_FILE.read_text().strip()})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            return {"sent": False, "why": f"the mailer answered {response.status}"}
    state["sent"].append(time.time())
    if (rec.get("pattern") or {}).get("verdict") in LOUD_PATTERNS:
        state["keys"][_pattern_key(rec)] = time.time()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    return {"sent": True, "subject": subject, "at": time.time()}
