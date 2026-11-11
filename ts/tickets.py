"""Tickets: a person says something is wrong, and that starts an investigation.

They live in the same database as every run, so the desk and the console see one
truth. The watcher works them one at a time, oldest first, each all the way through
before the next begins: read it, investigate from that person's computer, fix what
can be fixed, check it from their computer again, and ask them whether they can see it
working. If they say no, the ticket goes round again, starting from everything said so far.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from ts import brain, configs, engine, history, lab, llm, scrub, status, store
from ts.blocks import routing, services
from ts.intake import event_for
from ts.sweep import INTERNET_V4, INTERNET_V6

PEOPLE = [
    {"id": "priya", "name": "Priya Raman", "team": "Finance", "where": "Head office, floor 3", "host": "h1"},
    {"id": "marcus", "name": "Marcus Bell", "team": "Sales", "where": "Head office, floor 2", "host": "h2"},
    {"id": "aiko", "name": "Aiko Tanaka", "team": "Engineering", "where": "Head office, floor 4", "host": "h3"},
    {"id": "diego", "name": "Diego Flores", "team": "Customer support", "where": "Head office, floor 1", "host": "h4"},
    {"id": "lena", "name": "Lena Novak", "team": "Design", "where": "Annex, floor 1", "host": "h5"},
    {"id": "sam", "name": "Sam Okafor", "team": "Warehouse", "where": "Warehouse office", "host": "h6"},
    {"id": "ravi", "name": "Ravi Menon", "team": "Warehouse", "where": "Loading dock", "host": "h7"},
]
NEW, WORKING, CONFIRMING, WORKAROUND, SOLVED, NO_FAULT, NEEDS_AGENT, CLOSED = "new", "working", "confirming", "workaround", "solved", "no_fault", "needs_agent", "closed"
OPEN = (NEW, WORKING, CONFIRMING, WORKAROUND, NEEDS_AGENT)
LONGEST_WAIT = 2400    # seconds a ticket waits for its run before a person is asked to take it
ASK_WAIT = 480         # the longest the investigation waits for an answer to one question. Longer than anyone spends at their computer, so an answer is never missed
NO_REPLY_AFTER = 86400 # seconds without an answer before we stop waiting for them to confirm
MAX_ROUNDS = 3         # times a ticket goes round again before an engineer takes it

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, updated REAL, person TEXT, name TEXT, team TEXT, place TEXT, host TEXT,
                                   subject TEXT, body TEXT, status TEXT, stage TEXT, priority TEXT, kind TEXT, run_id TEXT, assignee TEXT, solved REAL, checked INTEGER);
CREATE TABLE IF NOT EXISTS ticket_notes(id INTEGER PRIMARY KEY AUTOINCREMENT, ticket INTEGER, time REAL, who TEXT, role TEXT, body TEXT, private INTEGER, closing INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ticket_notes_ticket ON ticket_notes(ticket);
"""

_TRIAGE = """You read tickets for a company's network service desk. Someone wrote in about their computer. Decide what kind of trouble it is.

Reply with JSON only: {"kind": "internet" | "address" | "other" | "not_network", "title": "a plain title of 3 to 8 words", "priority": "P1" | "P2" | "P3"}.
internet: the internet does not work at all from this computer.
address: the computer gets no network connection or no IP address.
other: any other network trouble, such as slowness, drops, or one site or service failing.
not_network: nothing to do with the network, such as a forgotten password.
P1: they cannot work and it is urgent. P2: they cannot work. P3: it is an annoyance."""

_DID_IT_WORK = """Someone reported a network problem. The service desk asked them whether it is working now, and this is what they wrote back. Decide what they are saying. Reply with JSON only: {"resolved": true} if what they tried is working for them, or {"resolved": false} if something they tried is not working, is only partly working, or still seems wrong. Something they say they have not tried or cannot check from where they are is not a failure."""

_ASK_THEM = """You write to someone who reported a network problem to their company's service desk. Write 2 or 3 plain sentences, as a person would. Do not greet them, thank them, apologise or sign off. If changes were made, say we made some changes and, if the record makes it clear, what was wrong in everyday words. If nobody found a problem, say that. Then ask them to try it and tell us whether they can see it working now. If the record says the change is temporary, say honestly that it should keep them going for now and that an engineer still has to do the proper repair. Do not use device names, commands or jargon. Send it with send_closing_message."""

_REPLY = """You write the reply from a company's network service desk to the person who opened a ticket. Write 2 to 4 plain sentences, as a person would. Do not greet them, thank them, apologise or sign off. Do not use device names, commands or jargon: say "a router at the edge of our network", not r3. Say honestly what the record says: if it is fixed and was checked from their computer, say so; if nobody found a problem, say that; if it is not solved, say an engineer is looking at it. End by asking them to reply if it is still not working, unless it is not solved. Reply with the message only."""


_CLOSING = [{"type": "function", "function": {"name": "send_closing_message", "description": "The troubleshooting is finished. Send the one message that closes this conversation. Their next reply is the last message on it.", "parameters": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}}}]

_ADDED = {"waiting_note": "INTEGER DEFAULT 0", "then_status": "TEXT DEFAULT ''", "rounds": "INTEGER DEFAULT 0"}  # waiting_note: the message of ours they have not answered


def _db(path: Path | None = None):
    conn = history._open(path)
    conn.executescript(_SCHEMA)
    have = {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
    for name, kind in _ADDED.items():
        if name not in have:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {kind}")
    if "closing" not in {row[1] for row in conn.execute("PRAGMA table_info(ticket_notes)")}:
        conn.execute("ALTER TABLE ticket_notes ADD COLUMN closing INTEGER DEFAULT 0")
    return conn


def person(person_id: str) -> dict | None:
    return next((p for p in PEOPLE if p["id"] == person_id), None)


def get(number: int, path: Path | None = None) -> dict | None:
    with _db(path) as db:
        row = db.execute("SELECT * FROM tickets WHERE id = ?", (number,)).fetchone()
    return dict(row) if row else None


def _set(number: int, path: Path | None = None, **fields) -> None:
    fields["updated"] = time.time()
    with _db(path) as db:
        db.execute(f"UPDATE tickets SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), number))


def note(number: int, who: str, role: str, body: str, private: bool = False, path: Path | None = None, closing: bool = False) -> int:
    """role: person, desk (the automatic reply), or agent. closing: the message that ends the conversation, so whatever they send next is the last word on it."""
    with _db(path) as db:
        made = db.execute("INSERT INTO ticket_notes(ticket, time, who, role, body, private, closing) VALUES (?,?,?,?,?,?,?)", (number, time.time(), who, role, body, int(private), int(closing))).lastrowid
        db.execute("UPDATE tickets SET updated = ? WHERE id = ?", (time.time(), number))
    return made


def _their_words_since(number: int, note_id: int, path: Path | None = None) -> str:
    with _db(path) as db:
        rows = db.execute("SELECT body FROM ticket_notes WHERE ticket = ? AND id > ? AND role = 'person' ORDER BY id", (number, note_id)).fetchall()
    return "\n\n".join(r["body"] for r in rows)


def ask(number: int, message: str, wait: float = ASK_WAIT, path: Path | None = None) -> str | None:
    """Write to the person and wait for their answer. None if it has not come in time."""
    ticket = get(number, path)
    if ticket is None:
        return None
    asked = note(number, "Service desk", "desk", scrub.text(message), path=path)
    first = ticket["name"].split()[0]
    _set(number, path, waiting_note=asked, stage=f"Waiting for {first} to reply")
    ends = time.time() + wait
    try:
        while time.time() < ends:
            said = _their_words_since(number, asked, path)
            if said:
                return scrub.text(said)
            time.sleep(min(2.0, max(0.0, ends - time.time())) or 0.01)
        return None
    finally:
        _set(number, path, waiting_note=0, stage="Investigating")


def create(person_id: str, body: str, path: Path | None = None) -> int:
    who, body = person(person_id), (body or "").strip()
    if who is None:
        raise ValueError("Choose who is reporting this.")
    if len(body) < 4:
        raise ValueError("Say what is going wrong.")
    first = " ".join(body.split())[:70]
    with _db(path) as db:
        number = db.execute("INSERT INTO tickets(created, updated, person, name, team, place, host, subject, body, status, stage, priority, kind, run_id, assignee, solved, checked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (time.time(), time.time(), who["id"], who["name"], who["team"], who["where"], who["host"], first, body, NEW, "Waiting its turn", "", "", "", "", 0, 0)).lastrowid
    note(number, who["name"], "person", body, path=path)
    note(number, "Service desk", "desk", "We have this and are looking into it. We will write here if we need anything from you.", path=path)
    return number


def notes_of(number: int, private: bool = True, path: Path | None = None) -> list[dict]:
    with _db(path) as db:
        rows = db.execute("SELECT * FROM ticket_notes WHERE ticket = ? ORDER BY id", (number,)).fetchall()
    return [dict(r) for r in rows if private or not r["private"]]


def listing(path: Path | None = None) -> list[dict]:
    with _db(path) as db:
        return [dict(r) for r in db.execute("SELECT * FROM tickets ORDER BY id DESC")]


def snapshot(path: Path | None = None) -> dict:
    """Everything the queue page shows."""
    rows = listing(path)
    waiting = sorted((t["id"] for t in rows if t["status"] == NEW))
    for row in rows:
        row["place_in_line"] = waiting.index(row["id"]) + 1 if row["id"] in waiting else 0
    state = status.read()
    return {"tickets": rows, "now": next((t["id"] for t in rows if t["status"] == WORKING), None), "waiting": len(waiting), "worker_alive": time.time() - (state.get("alive") or 0) < 40, "time": time.time()}


def _run_view(run_id: str) -> dict | None:
    rec = store.load(run_id, store.DATA_DIR) if run_id else None
    if rec is None:
        return None
    ai, fix = rec.get("agent") or {}, rec.get("repair") or {}
    look = rec.get("first_look") or {}
    return {"id": run_id, "first_look": {"working": (look.get("working") or {}).get("summary"), "changed": (look.get("changed") or {}).get("summary")} if look else None, "state": store.verdict(rec)[0], "conclusion": rec["result"].get("text") or "", "checks": [{"ask": s.get("ask"), "outcome": s.get("outcome"), "saw": s.get("saw")} for s in rec.get("steps") or []],
            "repair": fix.get("status"), "ai": {"status": ai.get("status"), "outcome": ai.get("outcome"), "root_cause": ai.get("root_cause"), "calls": [{"tool": c.get("tool"), "reason": c.get("reason"), "outcome": c.get("outcome"), "saw": c.get("saw"), "kind": c.get("kind")} for c in ai.get("calls") or []],
                                                                                                                                                                                                                              "changes": [{"status": c.get("status"), "device": c.get("device"), "what": c.get("what")} for c in ai.get("changes") or []]} if ai else None}


def view(number: int, for_person: bool = False, path: Path | None = None) -> dict | None:
    ticket = get(number, path)
    if ticket is None:
        return None
    out = {**ticket, "notes": notes_of(number, not for_person, path)}
    if for_person:
        return {k: out[k] for k in ("id", "created", "updated", "name", "subject", "status", "stage", "notes", "checked", "waiting_note")}
    return {**out, "run": _run_view(ticket["run_id"])}


# --- working a ticket -------------------------------------------------------

def _with_memory(asking: str, about: str) -> str:
    """Whatever the desk remembers doing about something like this, added to what it is asked."""
    earlier = brain.context_for("desk", about)
    return f"{asking}\n\n{earlier}" if earlier else asking


def triage(ticket: dict) -> dict:
    """What kind of trouble this is. Without the model, it goes straight to an investigation with the words as written."""
    fallback = {"kind": "other", "title": ticket["subject"], "priority": "P2"}
    try:
        reply = llm.complete([{"role": "system", "content": _TRIAGE}, {"role": "user", "content": _with_memory(f"{ticket['name']}, {ticket['team']}, wrote:\n{ticket['body']}", ticket["body"])}], max_tokens=3000, temperature=0.1, timeout=90, retries=1, role="desk")
    except llm.LLMError:
        return fallback
    plan = llm.parse_json(reply["text"]) or {}
    if plan.get("kind") not in ("internet", "address", "other", "not_network"):
        return fallback
    brain.remember("desk", "triaged", f"T-{ticket['id']}: {ticket['name']} wrote: {ticket['body'][:400]} I sorted it as {plan['kind']}, {plan.get('priority') or 'P2'}.")
    return {"kind": plan["kind"], "title": str(plan.get("title") or ticket["subject"])[:90], "priority": plan.get("priority") if plan.get("priority") in ("P1", "P2", "P3") else "P2"}


def _internet_target(host: str) -> tuple[str, str]:
    _ev, reserved = services.dhcp_reservations(host)
    return (INTERNET_V6, "::/0") if reserved and "v4" not in reserved else (INTERNET_V4, "0.0.0.0/0")


def _so_far(ticket: dict) -> str:
    """Everything an earlier go at this ticket learned, for the next one."""
    rec = store.load(ticket["run_id"], store.DATA_DIR) if ticket.get("run_id") else None
    ai = (rec or {}).get("agent") or {}
    first = ticket["name"].split()[0]
    lines = [f"THIS IS A FOLLOW-UP. An earlier investigation finished, but {first} says it is still not working."]
    if rec:
        lines += [f"The earlier investigation is run {rec['id']}, and open_run reads it.", f"What it concluded: {ai.get('root_cause') or rec['result'].get('text') or 'nothing firm'}", f"Changes it kept: {[c.get('what') for c in ai.get('changes') or [] if c.get('status') == 'kept'] or 'none'}"]
    lines += ["", "THE CONVERSATION ON THIS TICKET SO FAR"] + [f"{n['who']}: {n['body']}" for n in notes_of(ticket["id"], private=False)]
    return "\n".join(lines)


def event_from(ticket: dict, plan: dict, follow_up: bool = False) -> dict:
    """The ticket as an event for the agent, starting from that person's computer. No rule runs first: it goes straight to the agent."""
    host, said = ticket["host"], scrub.text(f"{ticket['name']} ({ticket['team']}, {ticket['place']}, using {ticket['host']}) reported: {ticket['body']}" + (f"\n\n{_so_far(ticket)}" if follow_up else ""))
    trigger = f"Ticket T-{ticket['id']} from {ticket['name']}"
    event = event_for("ticket.report", host, "ticket", f"{host}: {plan['title']}" + (" (still not working)" if follow_up else ""), trigger, said, ticket_id=str(ticket["id"]))
    now = time.time()
    return {**event, "source": "ticket", "ticket": ticket["id"], "reporter": ticket["name"], "follow_up": follow_up, "time": now, "trap_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "change_running": lab.change_running()}


def _already_on_it(plan: dict, host: str) -> str | None:
    """A run already working on this, so the ticket joins it rather than starting the same work twice."""
    wanted = {"internet": "internet.unreachable", "address": "dhcp.failure"}.get(plan["kind"])
    for row in store.summaries(store.DATA_DIR)[:40]:
        if row["state"] == "working" and (row["device"] == host or (wanted and row["kind"] == wanted)):
            return row["id"]
    return None


def _wait_for(run_id: str, number: int) -> dict | None:
    """Until the run has finished, keeping the ticket's stage current."""
    ends = time.time() + LONGEST_WAIT
    while time.time() < ends:
        rec = store.load(run_id, store.DATA_DIR)
        ai = (rec or {}).get("agent") or {}
        if rec and ai.get("status") not in ("queued", "working"):
            return rec
        if ai.get("status") == "working" and not get(number)["waiting_note"]:
            _set(number, stage=f"Investigating, {len(ai.get('calls') or [])} steps so far")
        elif ai.get("status") == "queued":
            _set(number, stage="Rules could not settle it, waiting for the AI")
        time.sleep(3)
    return None


def _works_from_their_computer(ticket: dict, plan: dict) -> bool | None:
    """The last word: try it from where the person is sitting. None when there is no such test."""
    try:
        if plan["kind"] == "internet":
            return routing.can_ping(ticket["host"], _internet_target(ticket["host"])[0])[1]
        if plan["kind"] == "address":
            return bool(services.host_addresses(ticket["host"])[1])
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def _facts(ticket: dict, outcome: str, rec: dict | None, checked: bool | None) -> list[str]:
    ai = (rec or {}).get("agent") or {}
    facts = [f"What they wrote: {ticket['body']}", f"Outcome: {outcome}", f"Checked from their computer afterwards: {'yes, it works' if checked else 'no, still failing' if checked is False else 'there is no direct test for this'}"]
    if rec:
        facts += [f"Conclusion: {rec['result'].get('text') or ''}", f"Root cause: {ai.get('root_cause') or 'none given'}", f"Repair: {(rec.get('repair') or {}).get('status') or 'none'}", f"Changes kept: {[c.get('what') for c in ai.get('changes') or [] if c.get('status') == 'kept']}",
                  f"Is the change temporary, with a proper repair still to come: {'yes' if ai.get('outcome') == 'workaround' else 'no'}"]
    return facts


def _ask_if_working(ticket: dict, then: str, rec: dict | None, checked: bool | None) -> str:
    """We think it is sorted. They are the ones who can say."""
    number, first = ticket["id"], ticket["name"].split()[0]
    made_changes = then != NO_FAULT
    try:
        turn = llm.chat([{"role": "system", "content": _ASK_THEM}, {"role": "user", "content": _with_memory("\n".join(_facts(ticket, then, rec, checked)), f"{ticket['name']} {ticket['body']}")}], _CLOSING, max_tokens=3000, temperature=0.3, timeout=90, retries=1, role="desk")
        message = next((c["args"]["message"] for c in turn["calls"] if c["name"] == "send_closing_message" and isinstance(c["args"], dict) and c["args"].get("message")), turn["text"]).strip()
    except llm.LLMError:
        message = ""
    message = message or ("We made some changes. Can you see it working now?" if made_changes else "We could not find a problem from our side. Is it working for you now?")
    asked = note(number, "Service desk", "desk", message, closing=True)
    brain.remember("desk", "told", f"T-{number}, {first}: {message}")
    _set(number, status=CONFIRMING, then_status=then, waiting_note=asked, stage=f"Waiting for {first} to confirm", checked=int(checked is True))
    return CONFIRMING


_NOT_WORKING = re.compile(r"\b(not|still|no|isn'?t|doesn'?t|can'?t|cannot|won'?t|fail\w*|broken|same|drop\w*|slow)\b", re.I)


def _it_works(ticket: dict, said: str) -> bool:
    try:
        plan = llm.parse_json(llm.complete([{"role": "system", "content": _DID_IT_WORK}, {"role": "user", "content": _with_memory(said, said)}], max_tokens=2000, temperature=0.0, timeout=60, retries=1, role="desk")["text"]) or {}
    except llm.LLMError:
        plan = {}
    return plan["resolved"] is True if isinstance(plan.get("resolved"), bool) else not _NOT_WORKING.search(said)


def _settle(ticket: dict, then: str, words: str, closing: str | None) -> str:
    number = ticket["id"]
    _set(number, status=then, then_status="", waiting_note=0, stage=words, solved=time.time() if then in (SOLVED, NO_FAULT) else 0)
    if closing:
        note(number, "Service desk", "desk", closing)
    return then


def _keep_how_it_turned_out(ticket: dict, said: str, worked: bool) -> None:
    """Whether a conclusion held up is only known once the person answers, so it is worth remembering on both sides of the desk."""
    number, first = ticket["id"], ticket["name"].split()[0]
    rec = store.load(ticket["run_id"], store.DATA_DIR) if ticket.get("run_id") else None
    concluded = ((rec or {}).get("agent") or {}).get("root_cause") or "no firm cause"
    brain.remember("desk", "judged", f"T-{number}: {first} wrote: {said[:400]} I took that as {'it works' if worked else 'still not working'}.")
    brain.remember("support", "outcome", f"T-{number} from {first} on {ticket['host']}: I concluded {concluded}. Asked whether it was working, they said: {said[:400]} So it {'held up' if worked else 'did not hold up'}.")


def earlier_from(number: int, limit: int = 5, path: Path | None = None) -> list[str]:
    """What the same person has written in about before, and how each one ended, newest first."""
    me = get(number, path)
    if me is None:
        return []
    with _db(path) as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM tickets WHERE host = ? AND id < ? ORDER BY id DESC LIMIT ?", (me["host"], number, limit))]
    out = []
    for row in rows:
        rec = store.load(row["run_id"], store.DATA_DIR) if row.get("run_id") else None
        cause = ((rec or {}).get("agent") or {}).get("root_cause")
        out.append(f"T-{row['id']}, {brain.when(row['created'])}: {row['subject']}. Ended as {row['status'].replace('_', ' ')}." + (f" Cause recorded: {cause}" if cause else ""))
    return out


def _hear_back(ticket: dict, investigate, log=print) -> str:
    """They answered "can you see it working?"."""
    number, first = ticket["id"], ticket["name"].split()[0]
    said = _their_words_since(number, ticket["waiting_note"])
    worked = _it_works(ticket, said)
    _keep_how_it_turned_out(ticket, said, worked)
    if worked:
        then = ticket["then_status"] or SOLVED
        words = {SOLVED: "Fixed and confirmed by " + first, WORKAROUND: "Working for now, needs an engineer", NO_FAULT: "No fault found"}[then]
        log(f"ticket T-{number}: confirmed by {first}")
        return _settle(ticket, then, words, None)  # nothing more to say: the ticket's status shows it, and a last word only invites another
    rounds = (ticket.get("rounds") or 0) + 1
    _set(number, rounds=rounds, then_status="", waiting_note=0)
    if rounds >= MAX_ROUNDS:
        log(f"ticket T-{number}: still not working after {rounds} goes")
        return _settle({**ticket, "rounds": rounds}, NEEDS_AGENT, "Needs an engineer", "An engineer is taking this over from here.") and NEEDS_AGENT
    note(number, "Service desk", "desk", "We are taking another look now.")
    plan = {"kind": ticket.get("kind") or "other", "title": ticket["subject"], "priority": ticket.get("priority") or "P2"}
    return work_one({**get(number), "status": WORKING}, investigate, log, plan, follow_up=True)


def _tell_them(ticket: dict, outcome: str, rec: dict | None, checked: bool | None) -> str:
    facts = _facts(ticket, outcome, rec, checked)
    try:
        told = llm.complete([{"role": "system", "content": _REPLY}, {"role": "user", "content": _with_memory("\n".join(facts), f"{ticket['name']} {ticket['body']}")}], max_tokens=3000, temperature=0.3, timeout=90, retries=1, role="desk")["text"]
        brain.remember("desk", "told", f"T-{ticket['id']}, {ticket['name'].split()[0]}: {told}")
        return told
    except llm.LLMError:
        return {SOLVED: "This is fixed now, and we checked it from your computer. Please reply if it is still not working.", NO_FAULT: "We checked from your computer and could not find a problem. Please reply if it is still not working."}.get(outcome, "We could not fix this automatically, so an engineer is taking a look.")


def _conclude(ticket: dict, plan: dict, rec: dict | None) -> str:
    state = store.verdict(rec)[0] if rec else "none"
    checked = _works_from_their_computer(ticket, plan)
    kept_a_change = any(c.get("status") == "kept" for c in ((rec or {}).get("agent") or {}).get("changes") or [])
    number, ai = ticket["id"], (rec or {}).get("agent") or {}
    if rec:
        note(number, "Service desk", "desk", f"Investigation finished as {state.replace('_', ' ')}. Root cause: {ai.get('root_cause') or rec['result'].get('text') or 'not found'}" + (f" For the engineer: {(rec.get('analysis') or {}).get('next')}" if state == "workaround" and (rec.get("analysis") or {}).get("next") else ""), private=True)
    if state == "workaround":
        return _ask_if_working(ticket, WORKAROUND, rec, checked)
    if (state == "fixed" and checked is not False) or (kept_a_change and checked is True):  # the second is the AI being cut off after the fix worked: their computer is the proof
        return _ask_if_working(ticket, SOLVED, rec, checked)
    if (state in ("cleared", "info", "watching") and checked is not False) or (checked is True and state != "needs_you"):
        return _ask_if_working(ticket, NO_FAULT, rec, checked)
    _set(number, status=NEEDS_AGENT, stage="Needs an engineer", then_status="", waiting_note=0, solved=0, checked=int(checked is True))
    note(number, "Service desk", "desk", _tell_them(ticket, NEEDS_AGENT, rec, checked))
    return NEEDS_AGENT


def work_one(ticket: dict, investigate, log=print, plan: dict | None = None, follow_up: bool = False) -> str:
    number = ticket["id"]
    if ticket.get("run_id") and plan is None:  # the watcher restarted while this one was running
        plan = {"kind": ticket.get("kind") or "other", "title": ticket["subject"], "priority": ticket.get("priority") or "P2"}
        return _conclude(ticket, plan, _wait_for(ticket["run_id"], number))
    _set(number, status=WORKING, stage="Having another look" if follow_up else "Reading what was reported")
    plan = plan or triage(ticket)
    _set(number, kind=plan["kind"], priority=plan["priority"], subject=plan["title"])
    if plan["kind"] == "not_network":
        _set(number, status=NEEDS_AGENT, stage="Not a network problem")
        note(number, "Service desk", "desk", "This does not look like a network problem, so one of our team will pick it up with you.")
        return NEEDS_AGENT
    run_id = _already_on_it(plan, ticket["host"])
    if run_id:
        _set(number, run_id=run_id, stage="Joined an investigation already running")
        note(number, "Service desk", "desk", f"Joined run {run_id}, which was already working on this.", private=True)
    else:
        _set(number, stage=f"Starting from {ticket['host']}")
        event = event_from(ticket, plan, follow_up)
        run_id = engine.incident_id(event)
        _set(number, run_id=run_id)
        log(f"ticket T-{number}: {investigate(event)}")
    outcome = _conclude({**get(number), "run_id": run_id}, plan, _wait_for(run_id, number))
    log(f"ticket T-{number}: {outcome}")
    return outcome


def _answered(path: Path | None = None) -> dict | None:
    """The oldest ticket where the person has answered "can you see it working?"."""
    with _db(path) as db:
        row = db.execute("""SELECT t.* FROM tickets t WHERE t.status = ? AND t.waiting_note > 0
                            AND EXISTS (SELECT 1 FROM ticket_notes n WHERE n.ticket = t.id AND n.id > t.waiting_note AND n.role = 'person') ORDER BY t.id LIMIT 1""", (CONFIRMING,)).fetchone()
    return dict(row) if row else None


def _stop_waiting(log=print, path: Path | None = None) -> None:
    """Nobody can wait forever for someone to confirm."""
    with _db(path) as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM tickets WHERE status = ? AND updated < ?", (CONFIRMING, time.time() - NO_REPLY_AFTER))]
    for ticket in rows:
        settled, words = {SOLVED: (NEEDS_AGENT, "Fixed on our side, but they never said it works"), WORKAROUND: (WORKAROUND, "Working for now, needs an engineer"), NO_FAULT: (NO_FAULT, "No fault found")}[ticket["then_status"] or SOLVED]  # only their word makes a ticket solved
        _settle(ticket, settled, words, None)
        log(f"ticket T-{ticket['id']}: no reply, so it is {settled}")


def work_forever(investigate, log=print) -> None:
    """One ticket at a time, oldest first. A ticket left half done by a restart is finished first."""
    with _db() as db:
        unfinished = [dict(r) for r in db.execute("SELECT * FROM tickets WHERE status = ? ORDER BY id", (WORKING,))]
    for ticket in unfinished:
        if ticket["run_id"]:
            work_one(ticket, investigate, log)
        else:
            _set(ticket["id"], status=NEW, stage="Waiting its turn")
    while True:
        try:
            with _db() as db:
                row = db.execute("SELECT * FROM tickets WHERE status = ? ORDER BY id LIMIT 1", (NEW,)).fetchone()
            answered = _answered()
            if answered:
                _hear_back(answered, investigate, log)
            elif row:
                work_one(dict(row), investigate, log)
            else:
                _stop_waiting(log)
                time.sleep(2)
        except Exception as exc:  # one bad ticket must not stop the desk
            log(f"ticket worker: {type(exc).__name__}: {exc}")
            time.sleep(5)


# --- what people do ---------------------------------------------------------

def reply(number: int, who: str, role: str, body: str, private: bool = False, path: Path | None = None) -> None:
    ticket, body = get(number, path), (body or "").strip()
    if ticket is None:
        raise ValueError("There is no such ticket.")
    if not body:
        raise ValueError("Write something first.")
    note(number, who, role, body, private, path)


def take(number: int, agent: str, path: Path | None = None) -> None:
    if get(number, path) is None:
        raise ValueError("There is no such ticket.")
    _set(number, path, assignee=agent)
    note(number, "Service desk", "desk", f"{agent} took this ticket.", private=True, path=path)


def close(number: int, who: str, path: Path | None = None) -> None:
    if get(number, path) is None:
        raise ValueError("There is no such ticket.")
    _set(number, path, status=CLOSED, stage="Closed", waiting_note=0, then_status="")
    note(number, "Service desk", "desk", f"{who} closed this ticket.", private=True, path=path)
    configs.release(ticket=number)  # the real repair is done, so a stop-gap may go back to known-good


def reopen(number: int, who: str, body: str, path: Path | None = None) -> None:
    """It is still broken: back in line for a fresh investigation."""
    ticket = get(number, path)
    if ticket is None:
        raise ValueError("There is no such ticket.")
    note(number, who, "person", (body or "").strip() or "It is still not working.", path=path)
    _set(number, path, status=NEW, stage="Waiting its turn", run_id="", solved=0, checked=0, waiting_note=0, then_status="", rounds=0)
