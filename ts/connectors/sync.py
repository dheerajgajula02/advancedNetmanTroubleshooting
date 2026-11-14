"""Keeps our service desk and their ticketing systems in step, every few seconds.

A pass, for each connected system:
1. New tickets there (made after the connector was first switched on) from someone we know become tickets here.
2. For each linked ticket: what the person wrote there since the last pass is added here as their words; what we
   wrote here goes there (public replies as comments, internal notes as work notes); a change of our status
   changes theirs.
3. Runs the watcher started that were worth an email open a ticket there with the same story.

Our own comments come back when we read theirs, so we keep a fingerprint of everything we sent and skip those.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path

from ts import connectors, tickets
from ts.connectors import ConnectorError

DB = Path(os.environ.get("TS_CONNECTORS_DB", Path(__file__).resolve().parent.parent.parent / "data" / "connectors.sqlite"))
EVERY = 15
CAME_IN = "Came in from "
OUTBOUND: queue.Queue = queue.Queue()
_RUNNING = {"on": False}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS links(system TEXT, ext_id TEXT, number TEXT, url TEXT, ticket INTEGER, created REAL, status TEXT, pushed INTEGER DEFAULT 0,
                                 seen TEXT DEFAULT '[]', sent TEXT DEFAULT '[]', PRIMARY KEY(system, ext_id));
CREATE TABLE IF NOT EXISTS marks(system TEXT PRIMARY KEY, since REAL, last_pass REAL, last_error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS outbound(run_id TEXT, system TEXT, ext_id TEXT, number TEXT, url TEXT, time REAL, PRIMARY KEY(run_id, system));
CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, system TEXT, what TEXT);
"""


def _db(path: Path | None = None):
    path = path or DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _print(text: str) -> str:
    return hashlib.sha256(" ".join((text or "").split()).encode()).hexdigest()[:20]


def _note(system: str, what: str, path: Path | None = None) -> None:
    with _db(path) as db:
        db.execute("INSERT INTO log(time, system, what) VALUES (?,?,?)", (time.time(), system, what[:500]))


def _since(c, path: Path | None) -> float:
    with _db(path) as db:
        row = db.execute("SELECT since FROM marks WHERE system = ?", (c.name,)).fetchone()
        if row:
            return row["since"]
        now = time.time()  # first time on: only tickets from now on, never the old ones already in their system
        db.execute("INSERT INTO marks(system, since, last_pass) VALUES (?,?,?)", (c.name, now, now))
        return now


def _bring_in(c, log, path: Path | None, tickets_path: Path | None) -> int:
    since, made = _since(c, path), 0
    with _db(path) as db:
        known = {r["ext_id"] for r in db.execute("SELECT ext_id FROM links WHERE system = ?", (c.name,))}
    for t in c.new_tickets(since):
        if t["id"] in known:
            continue
        who = connectors.match_person(t["caller_name"], t["caller_email"], tickets.PEOPLE)
        url = c.link(t["id"])
        if who is None:
            with _db(path) as db:
                db.execute("INSERT OR IGNORE INTO links(system, ext_id, number, url, ticket, created, status) VALUES (?,?,?,?,0,?,?)", (c.name, t["id"], t["number"], url, time.time(), "skipped"))
            _note(c.name, f"{t['number']}: left alone, {t['caller_name'] or 'nobody'} is not someone the desk looks after", path)
            continue
        subject, body = t["subject"].strip(), t["body"].strip()
        said = body if subject and body.startswith(subject) else "\n\n".join(x for x in (subject, body) if x)
        number = tickets.create(who["id"], said or "(no description)", path=tickets_path)
        tickets.note(number, "Service desk", "desk", f"{CAME_IN}{c.label} {t['number']}: {url}", private=True, path=tickets_path)
        with _db(path) as db:
            db.execute("INSERT INTO links(system, ext_id, number, url, ticket, created, status) VALUES (?,?,?,?,?,?,?)", (c.name, t["id"], t["number"], url, number, time.time(), "new"))
        _note(c.name, f"{t['number']} from {who['name']} is now T-{number}", path)
        log(f"{c.label} {t['number']} from {who['name']} is now ticket T-{number}")
        made += 1
    return made


def _keep_in_step(c, link: dict, log, path: Path | None, tickets_path: Path | None) -> None:
    number = link["ticket"]
    ticket = tickets.get(number, tickets_path)
    if ticket is None:
        return
    seen, sent = set(json.loads(link["seen"] or "[]")), set(json.loads(link["sent"] or "[]"))
    pushed, status = link["pushed"], link["status"]
    try:
        status = _exchange(c, link, ticket, seen, sent, log, tickets_path)
    finally:  # what was sent and read is kept even when a later call fails, so nothing is sent or read twice
        done = _note_ids(sent)
        pushed = max([pushed] + [i for i in done])
        sent -= {f"note:{i}" for i in done if i <= pushed}  # "pushed" covers them from here on
        with _db(path) as db:
            db.execute("UPDATE links SET pushed = ?, seen = ?, sent = ?, status = ? WHERE system = ? AND ext_id = ?",
                       (pushed, json.dumps(sorted(seen)), json.dumps(sorted(sent)), status, c.name, link["ext_id"]))


def _exchange(c, link: dict, ticket: dict, seen: set, sent: set, log, tickets_path: Path | None) -> str:
    """Their new words here, our new words there, our status there. Returns the status theirs now has."""
    number = link["ticket"]
    # 1. their words, here
    for comment in c.comments(link["ext_id"]):
        if comment["id"] in seen:
            continue
        seen.add(comment["id"])
        if _print(comment["body"]) in sent or not comment["body"].strip():
            continue  # our own, come back
        tickets.note(number, ticket["name"], "person", comment["body"].strip(), path=tickets_path)
        log(f"{c.label} {link['number']}: {ticket['name']} wrote back, added to T-{number}")
    # 2. our words, there
    for n in tickets.notes_of(number, private=True, path=tickets_path):
        if n["id"] <= link["pushed"] or f"note:{n['id']}" in sent:
            continue
        if n["role"] != "person" and not n["body"].startswith(CAME_IN):
            body = n["body"][:4000] if not n["private"] else f"[{n['who']}] {n['body']}"[:4000]
            sent.add(_print(body))  # before sending: if the answer is lost, it is still known as ours when it comes back
            c.add_comment(link["ext_id"], body, public=not n["private"])
        sent.add(f"note:{n['id']}")
    # 3. our status, there
    ticket = tickets.get(number, tickets_path)
    if ticket["status"] != link["status"]:
        public = [n["body"] for n in tickets.notes_of(number, private=False, path=tickets_path) if n["role"] != "person"]
        c.set_state(link["ext_id"], ticket["status"], public[-1] if public else "")
        log(f"{c.label} {link['number']}: now {ticket['status']} (T-{number})")
    return ticket["status"]


def _note_ids(sent: set) -> set:
    return {int(x.split(":", 1)[1]) for x in sent if x.startswith("note:")}


def _send_out(c, log, path: Path | None, pending: list[dict]) -> None:
    from ts import report
    for record in pending:
        with _db(path) as db:
            if db.execute("SELECT 1 FROM outbound WHERE run_id = ? AND system = ?", (record["id"], c.name)).fetchone():
                continue
        subject, body = report.compose(record)
        fixed = report.tag(record) in ("FIXED", "FIXED BY AI")
        made = c.open_ticket(subject, body, fixed)
        with _db(path) as db:
            db.execute("INSERT OR IGNORE INTO outbound VALUES (?,?,?,?,?,?)", (record["id"], c.name, made["id"], made["number"], made["url"], time.time()))
        _note(c.name, f"run {record['id']} opened {made['number']} ({'resolved, fixed' if fixed else 'open, needs a person'})", path)
        log(f"{c.label}: run {record['id']} opened {made['number']} ({'resolved' if fixed else 'open'})")


def sync_once(found: list | None = None, log=print, path: Path | None = None, tickets_path: Path | None = None, pending: list[dict] | None = None) -> dict:
    """One pass over every connected system. Returns what happened, per system."""
    found = connectors.configured() if found is None else found
    if pending is None:
        pending = []
        while not OUTBOUND.empty():
            pending.append(OUTBOUND.get_nowait())
    out = {}
    for c in found:
        error = ""
        try:
            made = _bring_in(c, log, path, tickets_path)
            with _db(path) as db:
                links = [dict(r) for r in db.execute("SELECT * FROM links WHERE system = ? AND ticket > 0 AND status != 'closed'", (c.name,))]
            for link in links:
                try:
                    _keep_in_step(c, link, log, path, tickets_path)
                except ConnectorError as exc:  # one ticket they refuse must not hold up the others
                    error = f"{link['number']}: {exc}"
            _send_out(c, log, path, pending)
            out[c.name] = {"new": made, "linked": len(links), **({"error": error} if error else {})}
        except ConnectorError as exc:
            error = str(exc)
            out[c.name] = {"error": error}
        with _db(path) as db:
            db.execute("UPDATE marks SET last_pass = ?, last_error = ? WHERE system = ?", (time.time(), error, c.name))
    return out


def after_run(record: dict) -> None:
    """A run is finished. One the watcher started on its own and worth an email goes to their systems too."""
    from ts import report
    if not _RUNNING["on"] or record["event"].get("source") == "ticket":
        return  # a ticket's run belongs to the ticket, which is already linked
    if report.should_send(record):
        OUTBOUND.put(json.loads(json.dumps(record, default=str)))


def work_forever(log=print) -> None:
    quiet: dict[str, float] = {}
    while True:
        try:
            for name, result in sync_once(log=log).items():
                if "error" in result and time.time() - quiet.get(result["error"], 0) > 600:
                    quiet[result["error"]] = time.time()
                    log(f"{name}: {result['error']}")
        except Exception as exc:  # the desk goes on without them
            log(f"connectors: {type(exc).__name__}: {exc}")
        time.sleep(EVERY)


def start(log=print) -> bool:
    if not connectors.configured():
        return False
    _RUNNING["on"] = True
    threading.Thread(target=work_forever, args=(log,), daemon=True).start()
    log("connectors on: " + ", ".join(c.label for c in connectors.configured()))
    return True


def status(path: Path | None = None) -> dict:
    with _db(path) as db:
        return {"systems": [dict(r) for r in db.execute("SELECT * FROM marks")],
                "links": [dict(r) for r in db.execute("SELECT system, number, url, ticket, status, created FROM links ORDER BY created DESC LIMIT 30")],
                "outbound": [dict(r) for r in db.execute("SELECT * FROM outbound ORDER BY time DESC LIMIT 30")],
                "log": [dict(r) for r in db.execute("SELECT * FROM log ORDER BY id DESC LIMIT 30")]}
