"""What each AI agent remembers of its own work, and how it finds it again.

Every agent has its own memory and nothing here reads across agents. Finding works by
words and by meaning together, like the search over past runs, and carries on by words
alone when the machine that works out meaning is away. A memory failing never stops the
work it was helping with.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from ts import embed, scrub

DB = Path(__file__).resolve().parent.parent / "data" / "brains.sqlite"
BODY = 1200
KEEP = 3000          # memories per agent, the oldest going first
KEEP_STRETCHES = 8   # for each thing an agent does regularly
CANDIDATES = 40
BLEND = 60           # the usual constant for blending two rankings
CLOSE_ENOUGH = 0.72  # the same line the search over past runs settled on: unrelated questions reached 0.69, real ones 0.77
WORDS_NEEDED = 0.5   # how much of the question a memory must share: one common word is not a match
BATCH = 64
EARLIER_WORK = "FROM YOUR OWN EARLIER WORK. Use it only if it really bears on this:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories(id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, time REAL NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memories_agent ON memories(agent, time);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_words USING fts5(body, tokenize='porter');
CREATE TABLE IF NOT EXISTS vectors(id INTEGER PRIMARY KEY, agent TEXT NOT NULL, model TEXT NOT NULL, vec BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS vectors_agent ON vectors(agent, model);
CREATE TABLE IF NOT EXISTS stretches(id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, what TEXT NOT NULL, how TEXT NOT NULL, first REAL NOT NULL, last REAL NOT NULL, times INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS stretches_agent ON stretches(agent, what, id);
"""
_WORD = re.compile(r"[a-z0-9][a-z0-9_.:/-]{2,}")
_COMMON = set("the and for was were are has have had did does this that with from what when where why how who you your they them their any not but can could would should about been its it's still just after before into out than then there here".split())


@contextmanager
def _db(path: Path | None = None):
    target = Path(path or DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(_SCHEMA)
        with db:
            yield db
    finally:
        db.close()


def remember(agent: str, kind: str, body, when: float | None = None, path: Path | None = None) -> None:
    text = scrub.text(str(body or "")).strip()[:BODY]
    if not text:
        return
    try:
        with _db(path) as db:
            made = db.execute("INSERT INTO memories(agent, time, kind, body) VALUES (?,?,?,?)", (agent, when or time.time(), kind, text)).lastrowid
            db.execute("INSERT INTO memory_words(rowid, body) VALUES (?,?)", (made, text))
            for row in db.execute("SELECT id FROM memories WHERE agent = ? ORDER BY time DESC, id DESC LIMIT -1 OFFSET ?", (agent, KEEP)).fetchall():
                db.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
                db.execute("DELETE FROM memory_words WHERE rowid = ?", (row["id"],))
                db.execute("DELETE FROM vectors WHERE id = ?", (row["id"],))
    except (sqlite3.Error, OSError) as exc:
        print(f"memory of {agent}: {type(exc).__name__}: {exc}", flush=True)


def _words(text: str) -> list[str]:
    return list(dict.fromkeys(w for w in _WORD.findall((text or "").lower()) if w not in _COMMON))[:14]


def _by_words(db, agent: str, words: list[str]) -> list[int]:
    if not words:
        return []
    rows = db.execute("""SELECT m.id, m.body FROM memory_words w JOIN memories m ON m.id = w.rowid
                         WHERE memory_words MATCH ? AND m.agent = ? ORDER BY bm25(memory_words) LIMIT ?""", (" OR ".join(f'"{w}"' for w in words), agent, CANDIDATES)).fetchall()
    enough = max(1, math.ceil(len(words) * WORDS_NEEDED))
    return [r["id"] for r in rows if sum(w[:5] in r["body"].lower() for w in words) >= enough]


def _catch_up(agent: str, model: str, path: Path | None) -> None:
    """Meaning is worked out when it is first needed, so writing a memory never waits for the GPU."""
    for _ in range(6):
        with _db(path) as db:
            rows = db.execute("""SELECT m.id, m.body FROM memories m LEFT JOIN vectors v ON v.id = m.id AND v.model = ?
                                 WHERE m.agent = ? AND v.id IS NULL ORDER BY m.id LIMIT ?""", (model, agent, BATCH)).fetchall()
        if not rows:
            return
        got = embed.vectors([r["body"] for r in rows])
        if not got:
            return
        with _db(path) as db:
            db.executemany("INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)", [(r["id"], agent, model, np.asarray(vec, "<f4").tobytes()) for r, vec in zip(rows, got)])


def _by_meaning(agent: str, about: str, path: Path | None) -> list[int]:
    if not embed.enabled():
        return []
    model = embed.model()
    _catch_up(agent, model, path)
    got = embed.vectors([about], "search_query")
    if not got:
        return []
    with _db(path) as db:
        rows = db.execute("SELECT id, vec FROM vectors WHERE agent = ? AND model = ?", (agent, model)).fetchall()
    if not rows:
        return []
    grid = np.vstack([np.frombuffer(r["vec"], "<f4") for r in rows])
    grid = grid / np.maximum(np.linalg.norm(grid, axis=1, keepdims=True), 1e-9)
    query = np.asarray(got[0], "f4")
    scores = grid @ (query / max(float(np.linalg.norm(query)), 1e-9))
    return [rows[i]["id"] for i in np.argsort(-scores)[:CANDIDATES] if scores[i] >= CLOSE_ENOUGH]


def recall(agent: str, about: str, k: int = 5, path: Path | None = None) -> list[dict]:
    """What this agent remembers about something, best first. Only ever its own."""
    try:
        with _db(path) as db:
            worded = _by_words(db, agent, _words(about))
        meant = _by_meaning(agent, about, path)
        score: dict[int, float] = defaultdict(float)
        for ranked in (worded, meant):
            for place, key in enumerate(ranked):
                score[key] += 1 / (BLEND + place)
        best = sorted(score, key=score.get, reverse=True)[:k]
        if not best:
            return []
        with _db(path) as db:
            rows = {r["id"]: dict(r) for r in db.execute(f"SELECT * FROM memories WHERE agent = ? AND id IN ({','.join('?' * len(best))})", (agent, *best))}
        return [rows[key] for key in best if key in rows]
    except (sqlite3.Error, OSError) as exc:
        print(f"memory of {agent}: {type(exc).__name__}: {exc}", flush=True)
        return []


def recent(agent: str, n: int = 10, path: Path | None = None) -> list[dict]:
    try:
        with _db(path) as db:
            return [dict(r) for r in db.execute("SELECT * FROM memories WHERE agent = ? ORDER BY time DESC, id DESC LIMIT ?", (agent, n))]
    except (sqlite3.Error, OSError):
        return []


def summary(agent: str, path: Path | None = None) -> dict:
    """How much this agent remembers, for whoever looks after it."""
    with _db(path) as db:
        kinds = {r["kind"]: r["n"] for r in db.execute("SELECT kind, count(*) AS n FROM memories WHERE agent = ? GROUP BY kind", (agent,))}
        latest = db.execute("SELECT max(time) FROM memories WHERE agent = ?", (agent,)).fetchone()[0]
    return {"count": sum(kinds.values()), "kinds": kinds, "latest": latest}


def listing(agent: str, kind: str = "", n: int = 300, path: Path | None = None) -> list[dict]:
    with _db(path) as db:
        return [dict(r) for r in db.execute("SELECT * FROM memories WHERE agent = ? AND (? = '' OR kind = ?) ORDER BY time DESC, id DESC LIMIT ?", (agent, kind, kind, n))]


def change(agent: str, memory: int, body: str, path: Path | None = None) -> bool:
    """Rewrite one memory. It is found by its new words at once, and by its new meaning the next time anything is looked up."""
    text = scrub.text(str(body or "")).strip()[:BODY]
    if not text:
        raise ValueError("A memory cannot be empty. Use forget to remove it.")
    with _db(path) as db:
        if not db.execute("UPDATE memories SET body = ? WHERE id = ? AND agent = ?", (text, memory, agent)).rowcount:
            return False
        db.execute("DELETE FROM memory_words WHERE rowid = ?", (memory,))
        db.execute("INSERT INTO memory_words(rowid, body) VALUES (?,?)", (memory, text))
        db.execute("DELETE FROM vectors WHERE id = ?", (memory,))
    return True


def forget(agent: str, memory: int, path: Path | None = None) -> bool:
    with _db(path) as db:
        if not db.execute("DELETE FROM memories WHERE id = ? AND agent = ?", (memory, agent)).rowcount:
            return False
        db.execute("DELETE FROM memory_words WHERE rowid = ?", (memory,))
        db.execute("DELETE FROM vectors WHERE id = ?", (memory,))
    return True


def when(moment: float, now: float | None = None) -> str:
    """As someone would say it: today is only a time, this week is a day and a time."""
    now = now or time.time()
    here = time.localtime(moment)
    return time.strftime("%H:%M" if here[:3] == time.localtime(now)[:3] else "%A %H:%M" if now - moment < 6 * 86400 else "%d %b %H:%M", here)


def lines(found: list[dict]) -> list[str]:
    return [f"- {when(m['time'])}: {m['body']}" for m in found]


def context_for(agent: str, about: str, k: int = 3, path: Path | None = None, heading: str = EARLIER_WORK) -> str:
    """What comes to mind, ready to go in front of the agent. Empty when nothing does."""
    found = recall(agent, about, k, path)
    return "\n".join([heading, *lines(found)]) if found else ""


def used(agent: str, what: str, how: str, moment: float | None = None, path: Path | None = None) -> dict | None:
    """One more go at something done regularly. Long runs of the same result are one stretch, as they are in anyone's memory. Returns the stretch that just ended, if how it went has changed."""
    moment = moment or time.time()
    with _db(path) as db:
        row = db.execute("SELECT * FROM stretches WHERE agent = ? AND what = ? ORDER BY id DESC LIMIT 1", (agent, what)).fetchone()
        if row and row["how"] == how:
            db.execute("UPDATE stretches SET last = ?, times = times + 1 WHERE id = ?", (moment, row["id"]))
            return None
        db.execute("INSERT INTO stretches(agent, what, how, first, last, times) VALUES (?,?,?,?,?,1)", (agent, what, how, moment, moment))
        db.execute("DELETE FROM stretches WHERE agent = ? AND what = ? AND id NOT IN (SELECT id FROM stretches WHERE agent = ? AND what = ? ORDER BY id DESC LIMIT ?)", (agent, what, agent, what, KEEP_STRETCHES))
        return dict(row) if row else None


def stretches(agent: str, path: Path | None = None) -> dict[str, list[dict]]:
    """Everything this agent does regularly, each with how it has gone, oldest first."""
    out: dict[str, list[dict]] = {}
    try:
        with _db(path) as db:
            for row in db.execute("SELECT * FROM stretches WHERE agent = ? ORDER BY id", (agent,)):
                out.setdefault(row["what"], []).append(dict(row))
    except (sqlite3.Error, OSError):
        pass
    return out
