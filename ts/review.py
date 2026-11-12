"""The review queue: what the AI has suggested, and what a person decided about it.

Nothing the AI suggests takes effect until someone accepts it. A rejection keeps its
reason, and that reason is searched and handed back to the AI the next time it is
working on something similar, so it does not suggest the same thing again.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ts import capture, history, rulebook, store, tags
from ts.ruleset import DEFAULT_RULES_DIR

KINDS = ("rule", "tag", "backdate", "monitor")
PENDING, ACCEPTED, REJECTED = "pending", "accepted", "rejected"


class Duplicate(Exception):
    """This has been suggested before."""

    def __init__(self, row: dict):
        super().__init__(row["title"])
        self.row = row


class NotPending(Exception):
    """Someone has already decided this."""


def fingerprint(*parts) -> str:
    """The same for two suggestions that mean the same thing, however they are worded."""
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _row(row) -> dict:
    out = dict(row)
    out["body"], out["proof"] = json.loads(out["body"] or "{}"), json.loads(out["proof"] or "{}")
    return out


def _search_text(title: str, why: str, body: dict, reason: str = "") -> str:
    return "\n".join([title, why, reason, json.dumps(body)])


def earlier(print_: str, path: Path | None = None) -> dict | None:
    """The most recent suggestion with this fingerprint."""
    with history._open(path) as db:
        row = db.execute("SELECT * FROM proposals WHERE fingerprint = ? ORDER BY id DESC LIMIT 1", (print_,)).fetchone()
    return _row(row) if row else None


def add(kind: str, title: str, why: str, body: dict, proof: dict | None = None, run_id: str = "", model: str = "", print_: str = "", path: Path | None = None) -> int:
    """Queue a suggestion. Raises Duplicate if the same thing is waiting, was accepted, or was rejected."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    print_ = print_ or fingerprint(kind, title)
    seen = earlier(print_, path)
    if seen:
        raise Duplicate(seen)
    with history._open(path) as db:
        cursor = db.execute("INSERT INTO proposals(kind, status, created, run_id, title, why, body, proof, model, fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (kind, PENDING, time.time(), run_id, title, why, json.dumps(body), json.dumps(proof or {}), model, print_))
        number = cursor.lastrowid
        db.execute("INSERT INTO proposals_fts VALUES (?,?)", (number, _search_text(title, why, body)))
    return number


def get(number: int, path: Path | None = None) -> dict | None:
    with history._open(path) as db:
        row = db.execute("SELECT * FROM proposals WHERE id = ?", (number,)).fetchone()
    return _row(row) if row else None


def listing(status: str | None = None, kind: str | None = None, page: int = 1, size: int = 20, path: Path | None = None) -> dict:
    where, args = [], []
    if status:
        where, args = [*where, "status = ?"], [*args, status]
    if kind:
        where, args = [*where, "kind = ?"], [*args, kind]
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with history._open(path) as db:
        total = db.execute(f"SELECT COUNT(*) FROM proposals{clause}", args).fetchone()[0]
        rows = db.execute(f"SELECT * FROM proposals{clause} ORDER BY COALESCE(decided, created) DESC, id DESC LIMIT ? OFFSET ?", [*args, size, (max(page, 1) - 1) * size]).fetchall()
    return {"total": total, "page": max(page, 1), "size": size, "items": [_row(r) for r in rows]}


def counts(path: Path | None = None) -> dict:
    with history._open(path) as db:
        rows = db.execute("SELECT status, COUNT(*) FROM proposals GROUP BY status").fetchall()
        by_kind = db.execute("SELECT kind, COUNT(*) FROM proposals WHERE status = ? GROUP BY kind", (PENDING,)).fetchall()
    return {PENDING: 0, ACCEPTED: 0, REJECTED: 0, **dict(rows), "waiting_by_kind": dict(by_kind)}


def rejected_like(text: str, k: int = 6, path: Path | None = None) -> list[dict]:
    """Rejected suggestions that read most like this. What the AI is shown before it suggests anything."""
    words = history.terms(text)
    if not words:
        return []
    with history._open(path) as db:
        rows = db.execute("""SELECT p.* FROM proposals_fts f JOIN proposals p ON p.id = f.id
                             WHERE proposals_fts MATCH ? AND p.status = ? ORDER BY bm25(proposals_fts) LIMIT ?""", (history._match(words), REJECTED, k)).fetchall()
    return [{"id": r["id"], "kind": r["kind"], "title": r["title"], "why": r["why"], "reason": r["reason"], "decided": r["decided"], "body": json.loads(r["body"] or "{}")} for r in rows]


def rejected_runs(tag: str, path: Path | None = None) -> set[str]:
    """Past runs a person has already said this tag does not belong on."""
    out: set[str] = set()
    with history._open(path) as db:
        for row in db.execute("SELECT body FROM proposals WHERE kind IN ('tag', 'backdate') AND status = ?", (REJECTED,)):
            body = json.loads(row["body"] or "{}")
            if body.get("tag") == tag:
                out |= {r["run_id"] for r in body.get("runs", [])}
    return out


def _settle(number: int, status: str, reason: str, by: str, extra: dict | None, path: Path | None) -> None:
    with history._open(path) as db:
        row = db.execute("SELECT title, why, body, proof, kind, run_id FROM proposals WHERE id = ?", (number,)).fetchone()
        proof = {**json.loads(row["proof"] or "{}"), **(extra or {})}
        db.execute("UPDATE proposals SET status = ?, decided = ?, reason = ?, decided_by = ?, proof = ? WHERE id = ?", (status, time.time(), reason, by, json.dumps(proof), number))
        db.execute("DELETE FROM proposals_fts WHERE id = ?", (number,))
        db.execute("INSERT INTO proposals_fts VALUES (?,?)", (number, _search_text(row["title"], row["why"], json.loads(row["body"]), reason)))
    capture.label(row["run_id"], "person", {"proposal": number, "kind": row["kind"], "title": row["title"], "decision": status, "reason": reason, "by": by}, f"proposal:{number}:{status}")


def _waiting(number: int, path: Path | None) -> dict:
    found = get(number, path)
    if found is None:
        raise KeyError(number)
    if found["status"] != PENDING:
        raise NotPending(f"This was already {found['status']}.")
    return found


def _apply_tag(tag: str, runs: list[str], number: int, path: Path | None) -> int:
    done = 0
    for run_id in runs:
        if store.exists(run_id) or history.run_exists(run_id, path):
            history.tag_run(run_id, tag, f"review:{number}", path)
            done += 1
    return done


def _close_runs(run_ids: list[str], number: int) -> list[str]:
    """A person's decision settles the runs it was about, so they stop waiting for one."""
    closed = []
    for run_id in run_ids:
        record = store.load(run_id, store.DATA_DIR) if run_id else None
        if record and store.verdict(record)[0] == "needs_you":
            store.set_handled(run_id, True, f"review:{number}", store.DATA_DIR)
            closed.append(run_id)
    return closed


def accept(number: int, by: str = "operator", mode: str = "shadow", runs: list[str] | None = None, root: Path = DEFAULT_RULES_DIR, path: Path | None = None) -> dict:
    """Do what was suggested. `mode` is how a rule starts out, `runs` which past runs a tag goes on (all of them if not given)."""
    found = _waiting(number, path)
    body, kind = found["body"], found["kind"]
    if kind == "monitor":
        from ts import grafana_monitors, monitors
        made = monitors.add(body["monitor"], found["why"], by, number, found["run_id"])
        outcome = {"monitor": made["id"], "grafana": grafana_monitors.sync_quietly(), "closed": []}
        _settle(number, ACCEPTED, "", by, outcome, path)
        return outcome
    if kind == "rule":
        if mode not in ("shadow", "active"):
            raise rulebook.RuleRefused("A rule starts out as shadow or active.")
        installed = rulebook.install(Path(root), body["rules"], mode, by)
        outcome = {"installed": installed, "mode": mode}
    else:
        tag = body["tag"]
        chosen = [r["run_id"] for r in body.get("runs", [])] if runs is None else list(runs)
        if kind == "tag":
            with history._open(path) as db:
                db.execute("INSERT OR REPLACE INTO tags VALUES (?,?,?,?,?)", (tag, body["description"], time.time(), number, ",".join(body.get("events") or [])))
        outcome = {"tagged": _apply_tag(tag, chosen, number, path), "runs": chosen}
    passed = kind != "rule" or (found["proof"].get("replay") or {}).get("ok")  # a rule's test: it reached this cause on what was recorded
    outcome["closed"] = _close_runs(([found["run_id"]] if kind == "rule" else outcome["runs"]) if passed else [], number)
    _settle(number, ACCEPTED, "", by, outcome, path)
    return outcome


def reject(number: int, reason: str, by: str = "operator", path: Path | None = None) -> None:
    """Turn it down. The reason is required, because it is what stops the same suggestion coming back."""
    reason = (reason or "").strip()
    if len(reason) < 4:
        raise ValueError("Say why, so it is not suggested again.")
    _waiting(number, path)
    _settle(number, REJECTED, reason, by, None, path)


def tag_catalog(path: Path | None = None) -> list[dict]:
    """Every tag with what it means and how many runs carry it, area by area."""
    used = history.tag_counts(path)
    known = tags.known(path)
    return [{"tag": tag, "area": tag.split("/", 1)[0], "name": tag.split("/", 1)[1], "description": text, "runs": used.get(tag, 0), "built_in": tag in tags.BUILT_IN} for tag, text in known.items()]
