"""Every investigation so far, searchable, and what keeps happening."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

DB = Path(os.environ.get("TS_HISTORY", Path(__file__).resolve().parent.parent / "data" / "history.sqlite"))
DAY = 86400
# Causes that mean a person or process changed something, as opposed to something breaking.
HAND_MADE = re.compile(r"^(config\.changed_by_(person|unknown)|config\.drift|interface\.(admin_shut|far_end_shut)|device\.restarted_drifted)")
# Causes, and short flaps, that point at the physical path.
PHYSICAL = re.compile(r"^(interface\.no_link|interface\.errdisabled|link\.)")
PHYSICAL_KINDS = {"interface.down", "link.degraded"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, time REAL, device TEXT, subject TEXT, kind TEXT, rule TEXT,
                                result_kind TEXT, label TEXT, text TEXT, repair TEXT);
CREATE INDEX IF NOT EXISTS runs_where ON runs(device, subject, kind, time);
CREATE TABLE IF NOT EXISTS docs(key TEXT PRIMARY KEY, source TEXT, time REAL, device TEXT, title TEXT, text TEXT, url TEXT);
CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY, value REAL);
CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, status TEXT, created REAL, decided REAL, run_id TEXT, title TEXT,
                                     why TEXT, body TEXT, proof TEXT, model TEXT, fingerprint TEXT, reason TEXT, decided_by TEXT);
CREATE INDEX IF NOT EXISTS proposals_status ON proposals(status, created);
CREATE VIRTUAL TABLE IF NOT EXISTS proposals_fts USING fts5(id UNINDEXED, body, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS tags(name TEXT PRIMARY KEY, description TEXT, added REAL, proposal INTEGER, events TEXT);
CREATE TABLE IF NOT EXISTS run_tags(run_id TEXT, tag TEXT, source TEXT, added REAL, PRIMARY KEY(run_id, tag));
CREATE INDEX IF NOT EXISTS run_tags_tag ON run_tags(tag);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, run_id TEXT, tool TEXT, kind TEXT, args TEXT, outcome TEXT, saw TEXT, output TEXT);
CREATE INDEX IF NOT EXISTS audit_time ON audit(time);
CREATE TABLE IF NOT EXISTS ai_sessions(run_id TEXT PRIMARY KEY, time REAL, device TEXT, label TEXT);
"""
# Stemming lets "dropping" find "dropped". Changing it means rebuilding the search index, so the version is kept in the file.
_SEARCH_INDEX = """
CREATE VIRTUAL TABLE IF NOT EXISTS runs_fts USING fts5(id UNINDEXED, body, tokenize='porter unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(key UNINDEXED, title, text, tokenize='porter unicode61');
"""
SEARCH_VERSION = 2
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*")
_FILLER = frozenset("""the and was were are for from that this with not but has have had its into than then when where which who why how what
                       there their them they been being any all can could would should did does only also about after before again very such
                       so it is be by as an or a no of to in on at his her one two get got per via""".split())
MAX_TERMS = 40


def _open(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # this disk takes most of a second to flush, and the store can be rebuilt
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    if conn.execute("PRAGMA user_version").fetchone()[0] < SEARCH_VERSION:
        _rebuild_search(conn)
    return conn


def _rebuild_search(conn: sqlite3.Connection) -> None:
    had_runs = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'runs_fts'").fetchone()
    bodies = [tuple(row) for row in conn.execute("SELECT id, body FROM runs_fts")] if had_runs else []
    conn.executescript("DROP TABLE IF EXISTS runs_fts; DROP TABLE IF EXISTS docs_fts;" + _SEARCH_INDEX)
    with conn:
        conn.executemany("INSERT INTO runs_fts VALUES (?,?)", bodies)
        conn.execute("INSERT INTO docs_fts SELECT key, title, text FROM docs")
        conn.execute(f"PRAGMA user_version = {SEARCH_VERSION}")


def _body(rec: dict) -> str:
    ev, r = rec["event"], rec["result"]
    parts = [ev.get("title", ""), ev.get("detail", ""), ev["device"], str(ev.get("subject") or ev.get("interface")), r.get("label") or "", r.get("text") or ""]
    parts += [f"{s['ask']} {s['saw']}" for s in rec["steps"]]
    ai, read = rec.get("agent") or {}, rec.get("analysis") or {}
    parts += [ai.get("root_cause") or "", read.get("summary") or "", read.get("next") or ""]
    parts += [f"{c.get('status')} {' '.join(c.get('what') or [])}" for c in ai.get("changes") or []]
    return "\n".join(p for p in parts if p)


def add_run(rec: dict, path: Path | None = None) -> None:
    ev, r = rec["event"], rec["result"]
    with _open(path) as db:
        db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (rec["id"], ev["time"], ev["device"], str(ev.get("subject") or ev.get("interface")), ev.get("event", "interface.down"), rec["rule"]["id"],
                    r["kind"], r.get("label") or "", r.get("text") or "", (rec.get("repair") or {}).get("status", "")))
        db.execute("DELETE FROM runs_fts WHERE id = ?", (rec["id"],))
        db.execute("INSERT INTO runs_fts VALUES (?,?)", (rec["id"], _body(rec)))


def add_docs(docs: list[dict], path: Path | None = None) -> int:
    """Store what is new and return how many that was."""
    added = 0
    with _open(path) as db:
        for d in docs:
            key = f"{d['source']}:{d['time']:.0f}:{d['title'][:120]}"
            if db.execute("SELECT 1 FROM docs WHERE key = ?", (key,)).fetchone():
                continue
            db.execute("INSERT INTO docs VALUES (?,?,?,?,?,?,?)", (key, d["source"], d["time"], d.get("device", ""), d["title"], d.get("text", ""), d.get("url", "")))
            db.execute("INSERT INTO docs_fts VALUES (?,?,?)", (key, d["title"], d.get("text", "")))
            added += 1
    return added


def sizes(path: Path | None = None) -> dict:
    with _open(path) as db:
        return {"runs": db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                "docs": db.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
                "by_source": dict(db.execute("SELECT source, COUNT(*) FROM docs GROUP BY source").fetchall())}


def ingested_until(path: Path | None = None) -> float | None:
    with _open(path) as db:
        row = db.execute("SELECT value FROM meta WHERE name = 'ingested_until'").fetchone()
    return row[0] if row else None


def set_ingested_until(moment: float, path: Path | None = None) -> None:
    with _open(path) as db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('ingested_until', ?)", (moment,))


def backfill(records, path: Path | None = None) -> int:
    n = 0
    for rec in records:
        add_run(rec, path)
        n += 1
    return n


def terms(*texts: str, limit: int = MAX_TERMS) -> list[str]:
    """Search words in the order given, so callers put the most telling first. Short names like r3 stay; filler goes."""
    seen: dict[str, None] = {}
    for text in texts:
        for word in _WORD.findall(text or ""):
            word = word.lower().rstrip(".:/-")
            mixed = any(c.isdigit() for c in word) and any(c.isalpha() for c in word)
            if word not in _FILLER and (len(word) >= 3 or mixed):
                seen.setdefault(word)
    return list(seen)[:limit]


def _match(words: list[str]) -> str:
    return " OR ".join(f'"{word}"' for word in words)


def similar(rec: dict, k: int = 5, path: Path | None = None, involved: tuple | list = ()) -> list[dict]:
    """Past runs that read most like this one, favouring the same device and the same answer."""
    ev, r = rec["event"], rec["result"]
    label = r.get("label") or ""
    words = terms(" ".join(involved), ev["device"], str(ev.get("subject") or ""), label.replace(".", " ").replace("_", " "), r.get("text") or "")
    if not words:
        return []
    with _open(path) as db:
        rows = db.execute("""SELECT r.*, bm25(runs_fts) AS rank FROM runs_fts f JOIN runs r ON r.id = f.id
                             WHERE runs_fts MATCH ? AND r.id != ? AND r.time <= ? ORDER BY rank LIMIT 40""",
                          (_match(words), rec["id"], ev["time"])).fetchall()

    def score(row: dict) -> float:
        return -row["rank"] * (1.5 if row["device"] == ev["device"] else 1) * (1.5 if label and row["label"] == label else 1) * (0.6 if row["result_kind"] == "stop" else 1)

    return sorted((dict(row) for row in rows), key=score, reverse=True)[:k]


def search_docs(words: list[str], k: int = 80, before: float | None = None, path: Path | None = None, among: list[str] | None = None) -> list[dict]:
    """Records from any source, older than a moment, ranked by how well they match. A title match counts four times.

    With `among`, only records that also contain one of those words."""
    if not words or among == []:
        return []
    query = f"({_match(among)}) AND ({_match(words)})" if among else _match(words)
    with _open(path) as db:
        rows = db.execute("""SELECT d.*, bm25(docs_fts, 0, 4, 1) AS rank FROM docs_fts f JOIN docs d ON d.key = f.key
                             WHERE docs_fts MATCH ? AND d.time < ? ORDER BY rank LIMIT ?""",
                          (query, before or time.time(), k)).fetchall()
    return [dict(row) for row in rows]


def search_runs(text: str, k: int = 8, path: Path | None = None) -> list[dict]:
    """Past runs matching some words, best first. For anyone looking something up by hand."""
    words = terms(text)
    if not words:
        return []
    with _open(path) as db:
        rows = db.execute("""SELECT r.* FROM runs_fts f JOIN runs r ON r.id = f.id WHERE runs_fts MATCH ? ORDER BY bm25(runs_fts) LIMIT ?""", (_match(words), k)).fetchall()
    return [dict(row) for row in rows]


def run_exists(run_id: str, path: Path | None = None) -> bool:
    with _open(path) as db:
        return db.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is not None


def tag_run(run_id: str, tag: str, source: str, path: Path | None = None) -> None:
    with _open(path) as db:
        db.execute("INSERT OR IGNORE INTO run_tags VALUES (?,?,?,?)", (run_id, tag, source, time.time()))


def tags_of(run_id: str, path: Path | None = None) -> list[str]:
    with _open(path) as db:
        return [row[0] for row in db.execute("SELECT tag FROM run_tags WHERE run_id = ? ORDER BY added", (run_id,))]


def all_run_tags(path: Path | None = None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    with _open(path) as db:
        for run_id, tag in db.execute("SELECT run_id, tag FROM run_tags ORDER BY added"):
            out.setdefault(run_id, []).append(tag)
    return out


def tag_counts(path: Path | None = None) -> dict[str, int]:
    with _open(path) as db:
        return dict(db.execute("SELECT tag, COUNT(*) FROM run_tags GROUP BY tag").fetchall())


def audit(run_id: str, tool: str, kind: str, args: str, outcome: str, saw: str, output: str, path: Path | None = None) -> None:
    with _open(path) as db:
        db.execute("INSERT INTO audit(time, run_id, tool, kind, args, outcome, saw, output) VALUES (?,?,?,?,?,?,?,?)", (time.time(), run_id, tool, kind, args, outcome, saw, output))


def ai_changes_lately(within: float = 3600, path: Path | None = None) -> int:
    with _open(path) as db:
        return db.execute("SELECT COUNT(*) FROM audit WHERE kind = 'change' AND outcome IN ('kept', 'reverted') AND time >= ?", (time.time() - within,)).fetchone()[0]


def ai_session_started(run_id: str, device: str, label: str, path: Path | None = None) -> None:
    with _open(path) as db:
        db.execute("INSERT OR REPLACE INTO ai_sessions VALUES (?,?,?,?)", (run_id, time.time(), device, label))


def ai_sessions_lately(within: float = 3600, device: str | None = None, label: str | None = None, path: Path | None = None) -> int:
    query, args = "SELECT COUNT(*) FROM ai_sessions WHERE time >= ?", [time.time() - within]
    if device is not None:
        query, args = query + " AND device = ? AND label = ?", [*args, device, label or ""]
    with _open(path) as db:
        return db.execute(query, args).fetchone()[0]


def fixes_lately(device: str, label: str, within: float = 3600, path: Path | None = None) -> int:
    with _open(path) as db:
        return db.execute("SELECT COUNT(*) FROM runs WHERE device = ? AND label = ? AND repair = 'fixed' AND time >= ?",
                          (device, label, time.time() - within)).fetchone()[0]


def _busiest_two_hours(times: list[float]) -> float:
    hours = Counter(time.localtime(t).tm_hour for t in times)
    return max((hours[h] + hours[(h + 1) % 24]) / len(times) for h in range(24)) if times else 0.0


def _count(rows: list[dict], since: float) -> int:
    return sum(row["time"] >= since for row in rows)


def pattern(rec: dict, docs: list[dict] | None = None, path: Path | None = None) -> dict:
    """Is this a one-off, or does it keep happening, and what does the way it repeats suggest?"""
    ev, r = rec["event"], rec["result"]
    now, subject, kind = ev["time"], str(ev.get("subject") or ev.get("interface")), ev.get("event", "interface.down")
    with _open(path) as db:
        rows = [dict(x) for x in db.execute("SELECT * FROM runs WHERE device = ? AND subject = ? AND kind = ? AND time >= ? AND time < ? AND id != ?",
                                            (ev["device"], subject, kind, now - 7 * DAY, now, rec["id"])).fetchall()]
    times = [row["time"] for row in rows] + [now]
    day_count = sum(t >= now - DAY for t in times)
    week_count, days = len(times), len({time.localtime(t).tm_yday for t in times})
    label = r.get("label") or ""
    hand_made = [row for row in rows if HAND_MADE.match(row["label"])]
    physical = [row for row in rows if not HAND_MADE.match(row["label"])]
    put_back = sum(row["repair"] == "fixed" for row in rows)
    jobs = [d["title"] for d in docs or [] if d["source"] == "jenkins" and "changes the network" in d.get("text", "")]
    what = f"{ev['device']} {subject}"
    verdict, note = "none", ""
    if week_count >= 3 and days >= 3 and _busiest_two_hours(times) >= 0.7:
        hour = Counter(time.localtime(t).tm_hour for t in times).most_common(1)[0][0]
        verdict, note = "scheduled", (f"{what} has come up {week_count} times in 7 days, on {days} different days, nearly always around {hour}:00. "
                                      "That looks like a scheduled job rather than a fault.")
    elif HAND_MADE.match(label) and (_count(hand_made, now - DAY) + 1 >= 2 or len(hand_made) + 1 >= 3):
        week_count, day_count = len(hand_made) + 1, _count(hand_made, now - DAY) + 1
        again = f", and it was put back {put_back} times and came back" if put_back else ""
        verdict, note = "tampering", (f"The same kind of hand change on {what} has now happened {week_count} times in 7 days, {day_count} of them in the last day{again}. "
                                      "Someone or something keeps doing this, so check for unauthorized access.")
    elif kind in PHYSICAL_KINDS and not HAND_MADE.match(label) and (not label or PHYSICAL.match(label)) and (_count(physical, now - DAY) + 1 >= 3 or len(physical) + 1 >= 5):
        week_count, day_count = len(physical) + 1, _count(physical, now - DAY) + 1
        verdict, note = "bad_hardware", (f"{what} has failed {week_count} times in 7 days, {day_count} of them in the last day, with no config change behind it. "
                                         "That points at failing hardware: the port, the optics or the cable.")
    elif week_count >= 3:
        verdict, note = "repeating", f"{what} has come up {week_count} times in 7 days, {day_count} of them in the last day."
    return {"verdict": verdict, "note": note, "count_24h": day_count, "count_7d": week_count, "put_back_before": put_back, "jobs_at_the_time": jobs[:6]}
