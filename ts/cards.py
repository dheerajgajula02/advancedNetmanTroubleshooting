"""What every past run taught, handed to the AI at the start of the next one.

Each finished run gets a short note written by the model. Each new run is given every past
run grouped by cause, the closest cases and what people accepted, and it can search inside
any past run or open one whole. How runs are stored and found is in rag.py.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ts import brain, history, llm, rag, scrub, store

ACCEPTED_SHOWN = 30
NEAR = 0.62               # how alike a record must be, by meaning, to count without sharing words
WRITERS = 8               # notes written at once when catching up
AUTO = False              # the watcher turns this on; nothing else writes notes on its own

_NOTE_SYSTEM = """You keep the case notes for a network operations team. Write the note for one finished investigation, for an engineer who meets something similar later.

Reply with JSON only: {"symptom": "...", "cause": "...", "cause_key": "...", "evidence": "...", "worked": "...", "failed": "...", "lesson": "..."}.
Each value is one short sentence with device names, ports and exact commands where they matter. Use only what the record shows. If the cause was never found, say so in "cause". If nothing was tried, "worked" and "failed" are "nothing tried".
cause_key names the cause as area.what in lower case, for example firewall.forward_blocked, so runs with the same cause can be grouped. Use a key from the known list whenever it is the same cause, even if the wording differs, and make a new one only for a different cause. If the cause was never found it is exactly "unknown"."""


_db = rag._db


def _when(moment: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(moment))


def _decisions(run_id: str, path: Path | None) -> list[str]:
    with _db(path) as db:
        rows = db.execute("SELECT id, kind, title, status, reason FROM proposals WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    return [f"- #{r['id']} {r['kind']}: {r['title']}: {r['status']}" + (f", because {r['reason']}" if r["reason"] else "") for r in rows]


def account(rec: dict, outputs: bool = False, path: Path | None = None) -> str:
    """One run, told completely. With outputs, what each command printed as well."""
    ev, r = rec["event"], rec["result"]
    out = [f"RUN {rec['id']}, {_when(ev['time'])}, {ev['device']} {ev.get('subject') or ev.get('interface') or ''}".rstrip(), ev.get("title") or "", ev.get("detail") or "",
           f"Ended as: {store.verdict(rec)[0].replace('_', ' ')}. Tags: {', '.join(rec.get('tags') or []) or 'none'}.", "Checks the rules ran:"]
    out += [f"- {s.get('ask')} -> {s.get('outcome')}: {s.get('saw')}" for s in rec.get("steps") or []]
    out.append(f"The rules concluded ({r.get('kind')}{', ' + r['label'] if r.get('label') else ''}): {r.get('text') or ''}")
    out += [f"Also ran {t['rule']}: {(t.get('result') or {}).get('text') or (t.get('result') or {}).get('kind')}" for t in rec.get("tried") or []]
    fix = rec.get("repair")
    if fix:
        out += [f"Repair: {fix.get('status')}. {fix.get('why') or ''}"] + [f"- {s.get('ask')}: {s.get('saw')}" for s in fix.get("steps") or []]
    ai = rec.get("agent") or {}
    if ai.get("calls") or ai.get("root_cause"):
        out += [f"The AI took over ({ai.get('why') or ''}). It finished as {ai.get('outcome')}. Root cause it gave: {ai.get('root_cause') or 'none'}", "What the AI did:"]
        for call in ai.get("calls") or []:
            line = f"- {call.get('tool')} {json.dumps(call.get('args') or {})}: {call.get('reason') or ''} -> {call.get('outcome')}: {call.get('saw') or ''}"
            out.append(line + (f"\n{call['output']}" if outputs and call.get("output") else ""))
        changes = ai.get("changes") or []
        if changes:
            out += ["Changes the AI tried:"] + [f"- {c.get('status')} on {c.get('device')}: {'; '.join(c.get('what') or [])}. {c.get('saw') or ''}" for c in changes]
    decided = _decisions(rec["id"], path)
    if decided:
        out += ["What people decided about its suggestions:", *decided]
    read = rec.get("analysis") or {}
    if read.get("summary"):
        out.append(f"Written read: {read['summary']} Next: {read.get('next') or ''}")
    if rec.get("handled"):
        out.append(f"A person closed this on {_when(rec['handled']['at'])}.")
    return scrub.text("\n".join(line for line in out if line))


def _note_text(rec: dict, parts: dict) -> str:
    ev = rec["event"]
    head = f"{rec['id']} ({ev['device']}, ended {store.verdict(rec)[0].replace('_', ' ')})"
    return scrub.text(f"{head}: {parts.get('symptom')} Cause: {parts.get('cause')} Evidence: {parts.get('evidence')} Worked: {parts.get('worked')} Failed: {parts.get('failed')} Lesson: {parts.get('lesson')}")


def _plain_note(rec: dict) -> dict:
    r, ai = rec["result"], rec.get("agent") or {}
    return {"symptom": rec["event"].get("title"), "cause": ai.get("root_cause") or r.get("text") or r.get("label") or "not found", "cause_key": r.get("label") or rag.UNKNOWN, "evidence": "see the run", "worked": (rec.get("repair") or {}).get("status") or "nothing tried", "failed": "nothing tried", "lesson": "none written"}


def _known_causes(path: Path | None) -> list[str]:
    """The cause names already in use, so the same cause gets the same name."""
    with _db(path) as db:
        known = {row[0] for row in db.execute("SELECT DISTINCT label FROM runs WHERE label != ''")}
    return sorted(known | {group["key"] for group in rag.groups(path=path)} - {rag.UNKNOWN})[:200]


def _facets(rec: dict, parts: dict) -> dict:
    """The note's fields, and the facts about the run that grouping and ranking need."""
    ev, ai, fix = rec["event"], rec.get("agent") or {}, rec.get("repair") or {}
    return {**parts, "cause_key": parts.get("cause_key") or rec["result"].get("label") or rag.UNKNOWN, "device": ev["device"], "subject": ev.get("subject") or ev.get("interface") or "", "kind": ev.get("event") or "", "ended": store.verdict(rec)[0].replace("_", " "),
            "fixed": fix.get("status") == "fixed" or any(c.get("status") == "kept" for c in ai.get("changes") or [])}


def write_note(rec: dict, path: Path | None = None) -> str:
    """The model's note on one run, or a plain one to be redone later if the model does not answer."""
    model, parts = "", None
    if llm.SECRETS.exists():
        try:
            known = ", ".join(_known_causes(path)) or "none yet"
            reply = llm.complete([{"role": "system", "content": _NOTE_SYSTEM}, {"role": "user", "content": "\n\n".join(filter(None, [f"Known cause_key values: {known}", brain.context_for("notes", f"{rec['event'].get('title')} {rec['result'].get('text') or ''}", heading="NOTES YOU WROTE EARLIER ON SIMILAR RUNS. Name the same thing the same way, and use them only if they really bear on this:"), account(rec, path=path)[:30000]]))}], max_tokens=6000, temperature=0.1, timeout=120, retries=1)
            parts, model = llm.parse_json(reply["text"]), reply["model"]
        except llm.LLMError:
            parts = None
    if not parts or not parts.get("symptom"):
        parts, model = _plain_note(rec), ""
    body = _note_text(rec, parts)
    if model:
        brain.remember("notes", "note", body)
    with _db(path) as db:
        db.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?)", (rec["id"], rec["event"]["time"], rec["event"]["device"], rec["event"].get("title") or "", body, model, time.time()))
    facets = _facets(rec, parts)
    rag.set_parts(rec["id"], facets, path)
    rag.save_pieces(rec["id"], rag.pieces_of(rec, facets, rag._decisions(rec["id"], path)), path)  # findable by words at once, with no GPU
    return body


# --- the vectors ------------------------------------------------------------

nearest = rag.nearest


def index(path: Path | None = None) -> int:
    """Bring finding by meaning up to date with the records, suggestions, tickets and past runs."""
    with _db(path) as db:
        items = [(f"doc:{r['key']}", "doc", r["key"], r["time"], f"{r['title']} {r['text'] or ''}") for r in db.execute("SELECT * FROM docs")]
        items += [(f"proposal:{r['id']}", "proposal", str(r["id"]), r["created"], f"{r['title']} {r['why'] or ''} {r['reason'] or ''}") for r in db.execute("SELECT * FROM proposals")]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'tickets'").fetchone():
            items += [(f"ticket:{r['id']}", "ticket", str(r["id"]), r["created"], f"{r['subject']} {r['body']}") for r in db.execute("SELECT * FROM tickets")]
        db.execute("DELETE FROM vectors WHERE kind = 'card'")  # one vector for a whole run; pieces replaced it
    return rag.store_vectors(items, path) + rag.sync(path)["vectors"]


def closest_runs(text: str, exclude=(), k: int = rag.CLOSEST, path: Path | None = None) -> tuple[list[str], bool]:
    """Past runs most like this text, best first. Says whether meaning took part."""
    hits, by_meaning = rag.find(text, rag.names_in(text), exclude=set(exclude), k=k, path=path)
    return [hit["run_id"] for hit in hits], by_meaning


def docs_like(text: str, k: int, before: float | None = None, path: Path | None = None) -> list[dict]:
    """Records from the monitoring, jobs and git that mean something close to this, shaped like a keyword search result."""
    hits = [(ref, likeness) for ref, likeness in nearest(text, "doc", k, before=before, path=path) if likeness >= NEAR]
    if not hits:
        return []
    with _db(path) as db:
        rows = {r["key"]: dict(r) for r in db.execute(f"SELECT * FROM docs WHERE key IN ({','.join('?' * len(hits))})", [ref for ref, _ in hits])}
    return [{**rows[ref], "rank": -likeness * 20, "near": True} for ref, likeness in hits if ref in rows]


def notes_for(run_ids: list[str], path: Path | None = None) -> dict[str, str]:
    if not run_ids:
        return {}
    with _db(path) as db:
        return {r["run_id"]: r["body"] for r in db.execute(f"SELECT run_id, body FROM cards WHERE run_id IN ({','.join('?' * len(run_ids))})", run_ids)}


# --- what a run is given ----------------------------------------------------

def _about(rec: dict) -> str:
    ev, r = rec["event"], rec["result"]
    return " ".join(filter(None, [ev.get("title"), rag.their_words(ev.get("detail")), ev.get("event"), r.get("label"), r.get("text")]))[:3000]


def prepare(rec: dict, path: Path | None = None) -> dict:
    """Pick the past runs this one will be given, and keep a record of how they were found."""
    began, ev = time.time(), rec["event"]
    text = _about(rec)
    hits, by_meaning = rag.find(text, rag.names_in(text, ev["device"], str(ev.get("subject") or "")), before=ev["time"], exclude={rec["id"]}, path=path)
    with _db(path) as db:
        notes = db.execute("SELECT count(*) FROM cards WHERE run_id != ? AND time < ?", (rec["id"], ev["time"])).fetchone()[0]
    rec["rag"] = {"notes": notes, "closest": [hit["run_id"] for hit in hits], "hits": hits, "by_meaning": by_meaning, "seconds": round(time.time() - began, 2)}
    return rec["rag"]


def render(rec: dict, path: Path | None = None) -> str:
    """The past, as the model reads it: the whole history grouped, then what people accepted, then the closest cases, which it reads last. Empty when prepare has not run."""
    info = rec.get("rag")
    if not info:
        return ""
    with _db(path) as db:
        accepted = db.execute("SELECT kind, title, why FROM proposals WHERE status = 'accepted' ORDER BY decided DESC LIMIT ?", (ACCEPTED_SHOWN,)).fetchall()
    grouped, _runs = rag.history_map(rec["event"]["time"], {rec["id"]}, path)
    out = [grouped] if grouped else []
    if accepted:
        out += ["", "WHAT PEOPLE ACCEPTED (these are now part of how the network is run)"] + [f"- {a['kind']}: {a['title']}. {(a['why'] or '')[:160]}" for a in accepted]
    told = rag.cases(info.get("hits") or [], path)
    if told:
        out += ["", "CLOSEST PAST RUNS (the ones most like this, by meaning and by exact words. Start from what worked, and do not repeat what failed)", *told]
    return scrub.text("\n".join(out).strip())


# --- keeping up -------------------------------------------------------------

def _remember(rec: dict) -> None:
    try:
        write_note(rec)
        index()
    except Exception:  # memory must never stop the run that fed it; catch_up tries again
        pass


def remember_soon(rec: dict) -> None:
    """Write this run's note off to the side, so the next run has it and this one is not held up."""
    if AUTO:
        threading.Thread(target=_remember, args=(json.loads(json.dumps(rec, default=str)),), daemon=True).start()


def catch_up(log=lambda _message: None, limit: int | None = 40, path: Path | None = None) -> dict:
    """Write the notes that are missing or were only plain, then bring the vectors up to date."""
    with _db(path) as db:
        good = {r["run_id"] for r in db.execute("SELECT c.run_id FROM cards c JOIN facets f ON f.run_id = c.run_id WHERE c.model != ''")}
    todo = [row["id"] for row in store.summaries(store.DATA_DIR) if row["id"] not in good and row.get("state") != "working"][:limit]
    def one(run_id: str) -> None:
        record = store.load(run_id, store.DATA_DIR)
        if record:
            write_note(record, path)

    with ThreadPoolExecutor(WRITERS) as pool:
        list(pool.map(one, todo))
    made = index(path)
    if todo or made:
        log(f"memory: wrote {len(todo)} notes, indexed {made} items")
    return {"notes": len(todo), "indexed": made}
