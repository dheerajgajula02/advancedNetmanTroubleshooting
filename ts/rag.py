"""Everything past runs did, cut into pieces a model can find, and handed back in a form it can use.

A run is cut into pieces: what was seen, the cause, what worked, what did not, the lesson,
every command with what it printed, every change, and what people decided. Each piece
starts with a line saying which run it came from, so it still makes sense alone.

Pieces are found two ways at once. Words find exact things like `netem` or `eth3`, which
meaning cannot tell apart. Meaning finds "I can't get online" for "cannot reach the
internet", which words cannot. Runs are then scored from their pieces, and repeats of the
same thing fold into one with a count, so ten flaps cannot fill every slot.

The store grows by itself: each finished run adds its pieces. Storing and finding by words
need no GPU. Meaning catches up when the GPU machine is back.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import Counter
from pathlib import Path

import numpy as np

from ts import embed, history, scrub, store
from ts.vocab import DEVICES, FORBIDDEN_DEVICES

CLOSEST = 5
CASE_CHARS = 1800
MAP_CHARS = 14000         # every run stays in the map; past this, the oldest groups shrink to a name and a count
MAP_LINE = 420
LONGEST_PIECE = 6000      # of one command's output, for finding by words
MEANING_CHARS = 1500      # of a piece, for finding by meaning; the start says what it is
CANDIDATES = 400
BLEND = 60                # the usual constant for blending two rankings
NAMED_EACH = 0.3          # how much more a piece counts for each device, port or address it shares. Small: the host that reports a fault is rarely where it is
NAMED_MOST = 0.9
TRUST = {"fixed": 1.2, "cause": 1.1, "none": 0.6}  # a fix that worked teaches more than a run that found nothing
SAME_CAUSE_SHOWN = 2      # at most this many from one cause, so the list covers different ideas
CLOSE_ENOUGH = 0.72       # how alike by meaning a piece must be to count as a real match. Unrelated questions reached 0.69 here, real ones 0.77
WORDS_NEEDED = 0.5        # or how much of the question it must contain: one shared word like "route" is not a match
EXCERPT = 280
PIECES_PER_RUN_SHOWN = 2
NOTHING = {"", "nothing tried", "none written", "see the run", "not found", "none", "n/a"}
UNKNOWN = "unknown"
ONLY_WORDS = {"checks"}   # the same few checks in every run; they tell runs apart by word, not by meaning

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards(run_id TEXT PRIMARY KEY, time REAL, device TEXT, title TEXT, body TEXT, model TEXT, made REAL);
CREATE TABLE IF NOT EXISTS vectors(key TEXT PRIMARY KEY, kind TEXT, ref TEXT, time REAL, model TEXT, digest TEXT, vec BLOB);
CREATE INDEX IF NOT EXISTS vectors_kind ON vectors(kind);
CREATE TABLE IF NOT EXISTS facets(run_id TEXT PRIMARY KEY, made REAL, parts TEXT);
CREATE TABLE IF NOT EXISTS pieces(key TEXT PRIMARY KEY, run_id TEXT, facet TEXT, time REAL, names TEXT, head TEXT, body TEXT);
CREATE INDEX IF NOT EXISTS pieces_run ON pieces(run_id);
CREATE TABLE IF NOT EXISTS pieced(run_id TEXT PRIMARY KEY, made REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS pieces_fts USING fts5(key UNINDEXED, body, tokenize='porter unicode61');
"""
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*")
PORT = re.compile(r"^(?:eth\d+|ethernet\d+|et\d+|vlan\d+|port-channel\d+|loopback\d+)$")
ADDRESS = re.compile(r"^(?:\d+\.\d+\.\d+\.\d+(?:/\d+)?|[0-9a-f]*:[0-9a-f:]*:[0-9a-f:/]*)$")
_KNOWN = {device.lower() for device in DEVICES} - {device.lower() for device in FORBIDDEN_DEVICES}

_matrices: dict = {}
_lock = threading.Lock()


def _db(path: Path | None = None):
    conn = history._open(path)
    conn.executescript(_SCHEMA)
    return conn


def _when(moment: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(moment))


def names_in(*texts: str) -> list[str]:
    """The devices, ports and addresses named in some text, in lower case."""
    found: dict[str, None] = {}
    for text in texts:
        for word in _NAME.findall(text or ""):
            word = word.lower().rstrip(".:")
            if word in _KNOWN or PORT.match(word) or ADDRESS.match(word):
                found.setdefault(word)
                if word.startswith("ethernet"):  # EOS logs write Ethernet1 as Et1
                    found.setdefault("et" + word.removeprefix("ethernet"))
    return list(found)


def _said(value) -> str:
    """A note's field, or nothing when all it says is that there was nothing to say."""
    text = str(value or "").strip()
    return "" if text.lower().rstrip(".") in NOTHING else text


def cause_key(parts: dict) -> str:
    key = re.sub(r"[^a-z0-9_.-]+", "_", str(parts.get("cause_key") or "").strip().lower()).strip("_.")
    return key or UNKNOWN


# --- vectors ----------------------------------------------------------------

def store_vectors(items: list[tuple[str, str, str, float, str]], path: Path | None = None) -> int:
    """items: (key, kind, ref, time, text). Only what is new or changed goes to the GPU."""
    if not items or not embed.enabled():
        return 0
    wanted_model = embed.model()
    with _db(path) as db:
        known = {r["key"]: r["digest"] for r in db.execute("SELECT key, digest FROM vectors WHERE model = ?", (wanted_model,))}
    digests = {key: hashlib.sha1(text.encode()).hexdigest() for key, _kind, _ref, _when_, text in items}
    todo = [item for item in items if known.get(item[0]) != digests[item[0]]]
    made = 0
    for start in range(0, len(todo), 96):
        part = todo[start:start + 96]
        got = embed.vectors([item[4] for item in part])
        if not got:
            break
        with _db(path) as db:
            db.executemany("INSERT OR REPLACE INTO vectors VALUES (?,?,?,?,?,?,?)", [(key, kind, ref, moment, wanted_model, digests[key], np.asarray(vec, "<f4").tobytes()) for (key, kind, ref, moment, _text), vec in zip(part, got)])
        made += len(part)
    return made


def _matrix(kind: str, path: Path | None):
    wanted = embed.model()
    with _db(path) as db:
        stamp = tuple(db.execute("SELECT count(*), max(rowid) FROM vectors WHERE kind = ? AND model = ?", (kind, wanted)).fetchone())
        name = (kind, str(path), wanted)
        with _lock:
            held = _matrices.get(name)
        if held and held[0] == stamp:
            return held
        rows = db.execute("SELECT ref, time, vec FROM vectors WHERE kind = ? AND model = ?", (kind, wanted)).fetchall()
    if not rows:
        return None
    grid = np.vstack([np.frombuffer(r["vec"], "<f4") for r in rows])
    grid = grid / np.maximum(np.linalg.norm(grid, axis=1, keepdims=True), 1e-9)
    held = (stamp, [r["ref"] for r in rows], np.asarray([r["time"] for r in rows]), grid)
    with _lock:
        _matrices[name] = held
    return held


def nearest(text: str, kind: str, k: int, before: float | None = None, exclude=(), path: Path | None = None, skip=None) -> list[tuple[str, float]]:
    """The k stored things most alike in meaning, as (ref, likeness). Empty when the GPU machine is away."""
    if not embed.enabled():
        return []
    held = _matrix(kind, path)
    got = embed.vectors([text], "search_query") if held else None
    if not got:
        return []
    _stamp, refs, times, grid = held
    query = np.asarray(got[0], "f4")
    scores = grid @ (query / max(float(np.linalg.norm(query)), 1e-9))
    out: list[tuple[str, float]] = []
    for at in np.argsort(-scores):
        if refs[at] not in exclude and (before is None or times[at] < before) and not (skip and skip(refs[at])):
            out.append((refs[at], float(scores[at])))
            if len(out) == k:
                break
    return out


# --- cutting a run into pieces ----------------------------------------------

def their_words(detail: str) -> str:
    """What a person said, without who said it. Their name and floor say nothing about the fault."""
    return re.sub(r"^[^:]{0,160}\breported:\s*", "", detail or "")


def _call_text(call: dict) -> str:
    args = call.get("args") or {}
    what = " ".join(str(v) for v in args.values() if isinstance(v, (str, int, float))) if args else ""
    return f"{call.get('tool')} {what}. Why: {call.get('reason') or ''} Result {call.get('outcome')}: {call.get('saw') or ''}\n{call.get('output') or ''}"[:LONGEST_PIECE].strip()


def pieces_of(rec: dict, parts: dict, decisions: list[dict] | None = None) -> list[dict]:
    """Every piece of one finished run. Each starts with a line that says where it came from."""
    ev, r, ai, fix = rec["event"], rec["result"], rec.get("agent") or {}, rec.get("repair") or {}
    subject = ev.get("subject") or ev.get("interface") or ""
    head = f"{ev['device']} {subject} | {ev.get('event') or ''} | {_when(ev['time'])[:10]} | ended {parts.get('ended') or store.verdict(rec)[0]} | cause {cause_key(parts)}"
    found: list[tuple[str, str]] = [("symptom", " ".join(filter(None, [ev.get("title"), their_words(ev.get("detail")), _said(parts.get("symptom"))]))),
                                    ("cause", " ".join(filter(None, [_said(parts.get("cause")), _said(ai.get("root_cause")), r.get("text"), _said(parts.get("evidence"))]))),
                                    ("worked", _said(parts.get("worked"))), ("failed", _said(parts.get("failed"))), ("lesson", _said(parts.get("lesson")))]
    if fix:
        found.append(("repair", f"Repair {fix.get('status')}: {fix.get('why') or ''} " + " ".join(f"{s.get('ask')}: {s.get('saw')}" for s in fix.get("steps") or [])))
    found.append(("checks", " ".join(f"{s.get('ask')} -> {s.get('outcome')}: {s.get('saw')}." for s in rec.get("steps") or [])))
    found += [("command", _call_text(call)) for call in ai.get("calls") or [] if call.get("tool") != "finish"]
    found += [("change", f"Change {c.get('status')} on {c.get('device')}: {'; '.join(c.get('what') or [])}. {c.get('saw') or ''}") for c in ai.get("changes") or []]
    found += [("decision", f"Suggested {d['kind']}: {d['title']}. People {d['status']} it" + (f" because {d['reason']}" if d.get("reason") else "") + f". {d.get('why') or ''}") for d in decisions or []]
    numbers: Counter = Counter()
    out = []
    for facet, text in found:
        if not text.strip():
            continue
        numbers[facet] += 1
        out.append({"key": f"{rec['id']}|{facet}|{numbers[facet]}", "run_id": rec["id"], "facet": facet, "time": ev["time"], "names": " ".join(names_in(head, text)), "head": scrub.text(head), "body": scrub.text(text)})
    return out


def save_pieces(run_id: str, pieces: list[dict], path: Path | None = None) -> None:
    fresh = {p["key"]: (p["head"], p["body"]) for p in pieces}
    with _db(path) as db:
        stale = [row["key"] for row in db.execute("SELECT key, head, body FROM pieces WHERE run_id = ?", (run_id,)) if fresh.get(row["key"]) != (row["head"], row["body"])]
        db.executemany("DELETE FROM vectors WHERE key = ?", [(f"piece:{key}",) for key in stale])
        db.execute("DELETE FROM pieces WHERE run_id = ?", (run_id,))
        db.execute("DELETE FROM pieces_fts WHERE key IN (SELECT key FROM pieces_fts WHERE key >= ? AND key < ?)", (f"{run_id}|", f"{run_id}}}"))
        db.executemany("INSERT INTO pieces VALUES (?,?,?,?,?,?,?)", [(p["key"], p["run_id"], p["facet"], p["time"], p["names"], p["head"], p["body"]) for p in pieces])
        db.executemany("INSERT INTO pieces_fts VALUES (?,?)", [(p["key"], f"{p['head']}\n{p['body']}") for p in pieces])
        db.execute("INSERT OR REPLACE INTO pieced VALUES (?,?)", (run_id, time.time()))


def _decisions(run_id: str, path: Path | None) -> list[dict]:
    with _db(path) as db:
        return [dict(r) for r in db.execute("SELECT id, kind, title, status, reason, why FROM proposals WHERE run_id = ? ORDER BY id", (run_id,))]


def set_parts(run_id: str, parts: dict, path: Path | None = None) -> None:
    with _db(path) as db:
        db.execute("INSERT OR REPLACE INTO facets VALUES (?,?,?)", (run_id, time.time(), json.dumps(parts)))


def parts_of(run_ids: list[str], path: Path | None = None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with _db(path) as db:
        for start in range(0, len(run_ids), 900):
            part = run_ids[start:start + 900]
            out.update({r["run_id"]: json.loads(r["parts"]) for r in db.execute(f"SELECT run_id, parts FROM facets WHERE run_id IN ({','.join('?' * len(part))})", part)})
    return out


def sync(path: Path | None = None) -> dict:
    """Cut up every run that is new or has changed, then give what changed its vectors."""
    with _db(path) as db:
        todo = [r["run_id"] for r in db.execute("""SELECT f.run_id FROM facets f LEFT JOIN pieced p ON p.run_id = f.run_id
                                                   WHERE p.made IS NULL OR f.made > p.made
                                                      OR EXISTS (SELECT 1 FROM proposals d WHERE d.run_id = f.run_id AND d.decided > p.made)""")]
    known = parts_of(todo, path)
    for run_id in todo:
        record = store.load(run_id, store.DATA_DIR)
        if record:
            save_pieces(run_id, pieces_of(record, known[run_id], _decisions(run_id, path)), path)
    with _db(path) as db:  # only what has no vector from this model, so a pass costs the same however long the history gets
        items = [(f"piece:{r['key']}", "piece", r["key"], r["time"], f"{r['head']}\n{r['body']}")
                 for r in db.execute(f"""SELECT p.key, p.time, p.head, substr(p.body, 1, {MEANING_CHARS}) AS body FROM pieces p LEFT JOIN vectors v ON v.key = 'piece:' || p.key AND v.model = ?
                                         WHERE v.key IS NULL AND p.facet NOT IN ({','.join('?' * len(ONLY_WORDS))})""", (embed.model(), *sorted(ONLY_WORDS)))]
    return {"runs": len(todo), "vectors": store_vectors(items, path)}


def status(path: Path | None = None) -> dict:
    """How much the memory holds, and whether finding by meaning is keeping up."""
    with _db(path) as db:
        pieces = db.execute("SELECT count(*) FROM pieces WHERE facet NOT IN ('checks')").fetchone()[0]
        total = db.execute("SELECT count(*) FROM pieces").fetchone()[0]
        runs = db.execute("SELECT count(DISTINCT run_id) FROM pieces").fetchone()[0]
        groups = len({cause_key(json.loads(r["parts"])) for r in db.execute("SELECT parts FROM facets")})
        having = db.execute("SELECT count(*) FROM vectors WHERE kind = 'piece' AND model = ?", (embed.model(),)).fetchone()[0] if embed.enabled() else 0
    return {"runs": runs, "pieces": total, "with_meaning": having, "waiting": max(0, pieces - having), "causes": groups, "model": embed.model(), "reachable": embed.reachable()}


# --- finding ----------------------------------------------------------------

def _by_words(words: list[str], before: float | None, exclude: set, path: Path | None) -> list[str]:
    if not words:
        return []
    with _db(path) as db:
        rows = db.execute("""SELECT p.key, p.run_id FROM pieces_fts f JOIN pieces p ON p.key = f.key
                             WHERE pieces_fts MATCH ? AND p.time < ? ORDER BY bm25(pieces_fts) LIMIT ?""",
                          (history._match(words), before or time.time() + 60, CANDIDATES)).fetchall()
    return [r["key"] for r in rows if r["run_id"] not in exclude]


def _rows(keys: list[str], path: Path | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with _db(path) as db:
        for start in range(0, len(keys), 900):
            part = keys[start:start + 900]
            out.update({r["key"]: dict(r) for r in db.execute(f"SELECT * FROM pieces WHERE key IN ({','.join('?' * len(part))})", part)})
    return out


def _excerpt(body: str, words: list[str]) -> str:
    """The part of a piece that matched, so a long command output comes back as the line that mattered."""
    low = body.lower()
    at = min((i for i in (low.find(w) for w in words) if i >= 0), default=0)
    start = max(0, at - 60)
    return ("..." if start else "") + " ".join(body[start:start + EXCERPT].split()) + ("..." if start + EXCERPT < len(body) else "")


def _covers(row: dict, words: list[str]) -> bool:
    """Does this piece contain enough of the question to be about the same thing?"""
    text = f"{row['head']} {row['body']}".lower()
    return bool(words) and sum(word in text for word in words) / len(words) >= WORDS_NEEDED


def _standing(parts: dict) -> str:
    if parts.get("fixed"):
        return "fixed"
    return "cause" if cause_key(parts) != UNKNOWN else "none"


def _same_case(hit: dict) -> tuple:
    """Two runs are the same case when one fault on one place raised them. A cause never found still groups by place."""
    return (hit["key"], hit["device"], hit["subject"])


def find(text: str, names=(), before: float | None = None, exclude=(), k: int = CLOSEST, path: Path | None = None) -> tuple[list[dict], bool]:
    """The past runs that bear on this, best first, each with the pieces that matched. Says whether meaning took part."""
    skip, words = set(exclude), history.terms(text)
    worded = _by_words(words, before, skip, path)
    meant = nearest(text, "piece", CANDIDATES, before=before, path=path, skip=lambda key: key.split("|", 1)[0] in skip)
    likeness = dict(meant)
    score: Counter = Counter()
    for ranked in (worded, [key for key, _ in meant]):
        for position, key in enumerate(ranked):
            score[key] += 1 / (BLEND + position)
    rows, wanted = _rows(list(score), path), set(names)
    runs: dict[str, list[tuple[float, dict]]] = {}
    for key, value in score.items():
        row = rows.get(key)
        if row:
            runs.setdefault(row["run_id"], []).append((value * (1 + min(NAMED_MOST, NAMED_EACH * len(wanted & set(row["names"].split())))), row))
    parts = parts_of(list(runs), path)
    hits = []
    for run_id, found in runs.items():
        found.sort(key=lambda item: -item[0])
        about = parts.get(run_id) or {}
        total = sum(value * 0.5 ** n for n, (value, _row) in enumerate(found[:3])) * TRUST[_standing(about)]
        hits.append({"run_id": run_id, "score": total, "time": found[0][1]["time"], "key": cause_key(about), "device": about.get("device") or found[0][1]["head"].split(" ", 1)[0], "subject": about.get("subject") or "",
                     "ended": about.get("ended") or "", "fixed": bool(about.get("fixed")), "close": any(_covers(row, words) or likeness.get(row["key"], 0) >= CLOSE_ENOUGH for _v, row in found[:3]),
                     "why": [{"facet": row["facet"], "text": _excerpt(row["body"], words)} for _v, row in found[:PIECES_PER_RUN_SHOWN]], "like": []})
    hits.sort(key=lambda h: (-h["score"], -h["time"]))
    kept: list[dict] = []
    same: dict[tuple, dict] = {}
    per_cause: Counter = Counter()
    for hit in hits:
        twin = same.get(_same_case(hit))
        if twin:
            twin["like"].append(hit["run_id"])
            continue
        if hit["key"] != UNKNOWN and per_cause[hit["key"]] >= SAME_CAUSE_SHOWN:
            continue
        same[_same_case(hit)] = hit
        per_cause[hit["key"]] += 1
        kept.append(hit)
    return kept[:k], bool(meant)


# --- what the model reads ---------------------------------------------------

def _habit(times: list[float]) -> str:
    days = {time.localtime(t).tm_yday for t in times}
    if len(times) < 3 or len(days) < 3:
        return ""
    hours = Counter(time.localtime(t).tm_hour for t in times)
    start = max(range(24), key=lambda h: hours[h] + hours[(h + 1) % 24])
    if (hours[start] + hours[(start + 1) % 24]) / len(times) < 0.7:
        return ""
    return f"on {len(days)} different days, nearly always {start:02d}:00-{(start + 2) % 24:02d}:00"


def _place_list(places: list[str]) -> str:
    return ", ".join(f"{name} x{count}" if count > 1 else name for name, count in Counter(places).most_common(5)) + (", ..." if len(set(places)) > 5 else "")


def groups(before: float | None = None, exclude=(), path: Path | None = None) -> list[dict]:
    """All past runs, one group per cause. Runs whose cause was never found group by place, so a flap stays visible."""
    with _db(path) as db:
        rows = db.execute("SELECT c.run_id, c.time, f.parts FROM cards c JOIN facets f ON f.run_id = c.run_id WHERE c.time < ? ORDER BY c.time", (before or time.time() + 60,)).fetchall()
    found: dict[tuple, dict] = {}
    for row in rows:
        if row["run_id"] in exclude:
            continue
        parts = json.loads(row["parts"])
        key = cause_key(parts)
        place = f"{parts.get('device')} {parts.get('subject')}".strip()
        name = (key,) if key != UNKNOWN else (key, parts.get("kind"), place)
        group = found.setdefault(name, {"key": key, "kind": parts.get("kind"), "times": [], "places": [], "fixed": 0, "latest": "", "worked": "", "failed": "", "lesson": ""})
        group["times"].append(row["time"])
        group["places"].append(place)
        group["fixed"] += bool(parts.get("fixed"))
        group["latest"] = row["run_id"]
        for field in ("worked", "failed", "lesson"):
            group[field] = _said(parts.get(field)) or group[field]
    return sorted(found.values(), key=lambda g: -max(g["times"]))


def _map_line(group: dict) -> str:
    times, count = group["times"], len(group["times"])
    title = f"cause never found: {group['kind']} on {group['places'][0]}" if group["key"] == UNKNOWN else group["key"]
    facts = [f"{count} run{'s' if count != 1 else ''}", _place_list(group["places"]) if group["key"] != UNKNOWN else "", f"last {_when(max(times))}", _habit(times), f"fixed {group['fixed']} times" if group["fixed"] else ""]
    line = f"- {title} ({'; '.join(filter(None, facts))})." + (f" Worked: {group['worked']}" if group["worked"] else "") + (f" Failed: {group['failed']}" if group["failed"] else "") + (f" Lesson: {group['lesson']}" if group["lesson"] else "")
    return line[:MAP_LINE].rstrip() + (f" Latest: {group['latest']}")


def history_map(before: float | None = None, exclude=(), path: Path | None = None) -> tuple[str, int]:
    """Every past run, in a space that does not grow with them. Older groups shrink to a name and a count, so none is ever dropped."""
    found = groups(before, exclude, path)
    runs = sum(len(g["times"]) for g in found)
    lines, used = [], 0
    rest: list[dict] = []
    for group in found:
        line = _map_line(group)
        if used + len(line) > MAP_CHARS:
            rest.append(group)
            continue
        lines.append(line)
        used += len(line)
    if rest:
        lines.append(f"- Older, by count ({sum(len(g['times']) for g in rest)} runs): " + ", ".join(f"{g['key'] if g['key'] != UNKNOWN else g['places'][0]} x{len(g['times'])}" for g in rest[:150]))
    head = f"EVERY PAST RUN, GROUPED BY CAUSE ({runs} runs in {len(found)} groups, newest group first. search_runs looks through every command, output, change and decision in them, open_run reads one whole)"
    return "\n".join([head, *lines]) if found else "", runs


def case(hit: dict, record: dict | None, decisions: list[dict], parts: dict) -> str:
    """One past run as an engineer would want it told: what it was, why, how that was shown, and what to do or avoid."""
    out = [f"CASE {hit['run_id']} ({_when(hit['time'])}, {hit['device']} {hit['subject']}), ended {hit['ended'] or 'unknown'}, cause {hit['key']}" + (", fixed and checked" if hit["fixed"] else "")]
    out += [f"{label}: {_said(parts.get(field))}" for label, field in (("Seen", "symptom"), ("Cause", "cause"), ("How that was shown", "evidence"), ("What worked", "worked"), ("What did not work", "failed"), ("Lesson", "lesson")) if _said(parts.get(field))]
    ai = (record or {}).get("agent") or {}
    fix = (record or {}).get("repair") or {}
    if fix:
        out.append(f"The rules' repair: {fix.get('status')}. {(fix.get('why') or '')[:200]}".strip())
    out += [f"Change {c.get('status')} on {c.get('device')}: {'; '.join(c.get('what') or [])}" for c in ai.get("changes") or []]
    out += [f"People {d['status']} the suggested {d['kind']} \"{d['title']}\"" + (f": {d['reason']}" if d.get("reason") else "") for d in decisions]
    shown = [w for w in hit.get("why") or [] if w["facet"] in ("command", "change", "checks", "decision")]
    out += [f"Matched in a {w['facet']}: {w['text']}" for w in shown]
    if hit.get("like"):
        out.append(f"Same thing, same place, {len(hit['like'])} more time(s): {', '.join(hit['like'][:4])}")
    return "\n".join(out)[:CASE_CHARS]


def cases(hits: list[dict], path: Path | None = None) -> list[str]:
    parts = parts_of([h["run_id"] for h in hits], path)
    return [case(hit, store.load(hit["run_id"], store.DATA_DIR), _decisions(hit["run_id"], path), parts.get(hit["run_id"]) or {}) for hit in hits]
