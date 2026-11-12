"""Reading the training data back: for the page, as fine-tuning files, and as one complete snapshot.

The files follow the hackathon's finetune/data.py: each example is one decision, with the task and the calls so far as
the query and the call the AI made next as the answer. Solved runs are used whole.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

from ts import agent, analyst, cards, capture, history, llm, store, tickets
from ts.ruleset import DEFAULT_RULES_DIR

ROOT = capture.ROOT
SNAPSHOTS = ROOT / "data" / "snapshots"
CONTAINER = "ts-training-db"
SPLIT_SEED = "ts-ft-v1"
RETRIEVAL_TOOLS = ("search_memory", "search_runs", "search_rejected", "list_rules", "list_tags", "search_jenkins", "read_build")
SKIPPED_OUTCOMES = ("invalid", "refused", "error")
UNDONE = ("undone", "reverted", "rolled_back")
SETTINGS = {"task_chars": 3500, "detail_steps": 6, "history_steps": 14, "output_chars": 350, "rationale_chars": 90, "decision_weight": 5, "test_fraction": 0.2}
BACKFILL_NOTE = "Rebuilt from an older run record, before this was being captured. Thinking was shortened when it was saved and is not per step, and commands have no stated reason."
NAMESPACE = uuid.UUID("6f1c2c9e-3a51-4f0e-9d3b-0a1f2b7c5e11")
ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

LIST = """SELECT e.episode_id::text AS id, e.run_id, e.kind, e.capture, e.model, e.status, e.outcome, extract(epoch FROM e.started_at) AS started, extract(epoch FROM e.ended_at) AS ended,
       r.device, r.record->'event'->>'title' AS title,
       (SELECT count(*) FROM steps s WHERE s.episode_id = e.episode_id) AS steps,
       (SELECT count(*) FROM steps s WHERE s.episode_id = e.episode_id AND s.reason <> '') AS reasons,
       (SELECT count(*) FROM steps s WHERE s.episode_id = e.episode_id AND s.action->>'tool' = ANY(%(retrieval)s)) AS retrievals,
       (SELECT coalesce(sum(length(c.thinking)), 0) FROM llm_calls c WHERE c.episode_id = e.episode_id) AS thinking_chars,
       (SELECT coalesce(sum((c.usage->>'prompt_tokens')::bigint), 0) FROM llm_calls c WHERE c.episode_id = e.episode_id) AS tokens_in,
       (SELECT coalesce(sum((c.usage->>'completion_tokens')::bigint), 0) FROM llm_calls c WHERE c.episode_id = e.episode_id) AS tokens_out,
       (SELECT coalesce(sum(i.chars), 0) FROM inputs i WHERE i.episode_id = e.episode_id AND i.sent) AS input_chars
FROM episodes e LEFT JOIN runs r USING (run_id) """


def plain(value):
    """For json.dumps: what the database hands back that JSON has no word for."""
    return float(value) if isinstance(value, decimal.Decimal) else str(value)


def _away_errors() -> tuple:
    try:
        import psycopg
    except ImportError:
        return (ImportError, OSError)
    return (psycopg.OperationalError, OSError, KeyError)


AWAY = _away_errors()


def _filters(want: dict) -> tuple[str, dict]:
    where, values = [], {"retrieval": list(RETRIEVAL_TOOLS)}
    for name in ("kind", "capture", "status"):
        if want.get(name):
            where.append(f"e.{name} = %({name})s")
            values[name] = want[name]
    if want.get("q"):
        where.append("(e.run_id ILIKE %(q)s OR r.device ILIKE %(q)s OR r.record->'event'->>'title' ILIKE %(q)s)")
        values["q"] = f"%{want['q']}%"
    return (" WHERE " + " AND ".join(where) if where else ""), values


def totals(db) -> dict:
    row = db.execute("""SELECT (SELECT count(*) FROM runs) AS runs, (SELECT count(*) FROM episodes) AS episodes, (SELECT count(*) FROM episodes WHERE capture = 'live') AS live,
        (SELECT count(*) FROM steps) AS steps, (SELECT count(*) FROM steps WHERE reason <> '') AS reasons, (SELECT count(*) FROM llm_calls) AS llm_calls,
        (SELECT count(*) FROM llm_calls WHERE coalesce(thinking, '') <> '') AS with_thinking, (SELECT coalesce(sum(length(thinking)), 0) FROM llm_calls) AS thinking_chars,
        (SELECT coalesce(sum((usage->>'prompt_tokens')::bigint), 0) FROM llm_calls) AS tokens_in, (SELECT coalesce(sum((usage->>'completion_tokens')::bigint), 0) FROM llm_calls) AS tokens_out,
        (SELECT count(*) FROM labels WHERE labeler = 'person') AS person_labels, (SELECT count(*) FROM fault_truth) AS faults, pg_database_size(current_database()) AS bytes""").fetchone()
    return {**row, "waiting": sum(1 for _ in capture.WAITING.open()) if capture.WAITING.exists() else 0}


def page(want: dict) -> dict:
    size, number = min(max(int(want.get("size") or 25), 5), 100), max(int(want.get("page") or 1), 1)
    where, values = _filters(want)
    with capture.open_for_reading() as db:
        total = db.execute(f"SELECT count(*) AS n FROM episodes e LEFT JOIN runs r USING (run_id){where}", values).fetchone()["n"]
        pages = max(1, (total + size - 1) // size)
        number = min(number, pages)
        rows = db.execute(LIST + where + " ORDER BY e.started_at DESC LIMIT %(limit)s OFFSET %(offset)s", {**values, "limit": size, "offset": (number - 1) * size}).fetchall()
        return {"totals": totals(db), "items": rows, "total": total, "page": number, "pages": pages, "size": size, "snapshots": snapshots()}


def detail(episode_id: str) -> dict | None:
    if not ID.match(episode_id):
        return None
    with capture.open_for_reading() as db:
        found = db.execute(LIST + "WHERE e.episode_id = %(id)s", {"id": episode_id, "retrieval": list(RETRIEVAL_TOOLS)}).fetchone()
        if not found:
            return None
        settings = db.execute("SELECT settings FROM episodes WHERE episode_id = %s", (episode_id,)).fetchone()["settings"]
        sections = db.execute("SELECT section_idx AS n, section, source, sent, chars FROM inputs WHERE episode_id = %s ORDER BY section_idx", (episode_id,)).fetchall()
        steps = db.execute("""SELECT s.step_idx AS n, extract(epoch FROM s.ts) AS ts, s.llm_call, s.reason, s.action, s.kind, s.outcome, s.saw, left(s.tool_output, 400) AS output, length(s.tool_output) AS output_len,
                                     s.validation->'change'->>'status' AS change_status, s.latency_ms, c.thinking
                              FROM steps s LEFT JOIN llm_calls c ON c.episode_id = s.episode_id AND c.call_idx = s.llm_call WHERE s.episode_id = %s ORDER BY s.step_idx""", (episode_id,)).fetchall()
        calls = db.execute("SELECT call_idx AS n, thinking, content, finish, usage, latency_ms, messages_sent FROM llm_calls WHERE episode_id = %s ORDER BY call_idx", (episode_id,)).fetchall()
        labels = db.execute("SELECT labeler, label, extract(epoch FROM created_at) AS at FROM labels WHERE episode_id = %(id)s OR (run_id = %(run)s AND labeler = 'person' AND %(run)s <> '') ORDER BY created_at", {"id": episode_id, "run": found["run_id"] or ""}).fetchall()
        truth = db.execute("""SELECT f.drill, f.spec, extract(epoch FROM f.injected_at) AS injected, extract(epoch FROM f.cleared_at) AS cleared FROM fault_truth f JOIN runs r ON r.run_id = %s
                              WHERE f.injected_at <= r.happened_at AND coalesce(f.cleared_at, now()) >= r.happened_at - interval '60 seconds' ORDER BY f.injected_at DESC LIMIT 3""", (found["run_id"],)).fetchall()
        states = db.execute("SELECT phase, step_idx AS n, state, extract(epoch FROM ts) AS ts FROM state_snapshots WHERE episode_id = %s ORDER BY ts", (episode_id,)).fetchall()
    return {**found, "settings": settings, "sections": sections, "steps": steps, "calls": calls, "labels": labels, "truth": truth, "states": states}


def input_section(episode_id: str, n: int) -> dict | None:
    if not ID.match(episode_id):
        return None
    with capture.open_for_reading() as db:
        return db.execute("SELECT section_idx AS n, section, source, sent, chars, body, items FROM inputs WHERE episode_id = %s AND section_idx = %s", (episode_id, n)).fetchone()


def step(episode_id: str, n: int) -> dict | None:
    if not ID.match(episode_id):
        return None
    with capture.open_for_reading() as db:
        return db.execute("""SELECT s.step_idx AS n, s.reason, s.action, s.kind, s.outcome, s.saw, s.tool_output, s.given_back, s.validation, s.latency_ms, c.thinking, c.content, c.usage
                             FROM steps s LEFT JOIN llm_calls c ON c.episode_id = s.episode_id AND c.call_idx = s.llm_call WHERE s.episode_id = %s AND s.step_idx = %s""", (episode_id, n)).fetchone()


# ---------------------------------------------------------------- older runs
def _backfill_episode(record: dict, kind: str, model: str, prompt: str, system: str, thinking: str, started: float, ended: float, status: str, outcome: dict, calls: list[dict]) -> None:
    episode_id = str(uuid.uuid5(NAMESPACE, f"{record['id']}:{kind}"))
    capture._write("episodes", {"episode_id": episode_id, "run_id": record["id"], "kind": kind, "capture": "backfill", "model": model, "settings": {"note": BACKFILL_NOTE}, "status": status, "outcome": outcome, "started_at": capture._when(started), "ended_at": capture._when(ended)})
    parts = capture.sections(system, prompt, [], record)
    for n, part in enumerate(parts):
        body = part.get("body")
        capture._write("inputs", {"episode_id": episode_id, "section_idx": n, "section": part["section"], "source": part["source"], "sent": part.get("sent", True), "chars": len(body) if body else len(json.dumps(part.get("items") or "", default=str)), "body": body, "items": part.get("items")})
    for n, (role, text) in enumerate((("system", system), ("user", prompt))):
        capture._write("messages", {"episode_id": episode_id, "idx": n, "ts": capture._when(started), "role": role, "content": text, "thinking": ""})
    capture._write("llm_calls", {"episode_id": episode_id, "call_idx": 0, "ts": capture._when(started), "messages_sent": 2, "thinking": thinking or "", "content": "", "usage": {}, "request": {"model": model}})
    for call in calls:
        capture._write("steps", {"episode_id": episode_id, "step_idx": call["n"], "ts": capture._when(call.get("started", started)), "llm_call": 0, "reason": "", "action": {"tool": call["tool"], "args": call.get("args") or {}}, "kind": call.get("kind"), "outcome": call.get("outcome"), "saw": call.get("saw") or "", "tool_output": call.get("output") or "", "given_back": "", "validation": {"ran": call.get("ran") or []}, "latency_ms": call.get("ms")})


def backfill() -> dict:
    """Every run already on disk goes into the database. Running it twice changes nothing."""
    counts = {"runs": 0, "agent": 0, "analyst": 0, "labels": 0}
    for path in sorted(store.DATA_DIR.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        capture._write("runs", capture.run_row(record))
        counts["runs"] += 1
        worked, read = record.get("agent") or {}, record.get("analysis") or {}
        if worked.get("calls") and worked.get("prompt"):
            _backfill_episode(record, "agent", worked.get("model", ""), worked["prompt"], agent.SYSTEM, worked.get("reasoning", ""), worked.get("started", record["event"]["time"]), worked.get("finished", record["event"]["time"]), worked.get("status", "done"),
                              {"outcome": worked.get("outcome"), "root_cause": worked.get("root_cause"), "changes": [c.get("status") for c in worked.get("changes") or []], "proposals": worked.get("proposals")}, worked["calls"])
            counts["agent"] += 1
        elif read.get("prompt"):
            from ts import analyst
            _backfill_episode(record, "analyst", read.get("model", ""), read["prompt"], analyst.SYSTEM, read.get("reasoning", ""), read.get("started", record["event"]["time"]), read.get("finished", record["event"]["time"]), "done",
                              {k: read.get(k) for k in ("summary", "assessment", "why", "next", "used", "related")}, [])
            counts["analyst"] += 1
    with history._open(None) as db:
        for row in db.execute("SELECT id, kind, status, run_id, title, reason, decided_by FROM proposals WHERE status != 'pending'").fetchall():
            capture._write("labels", {"label_id": str(uuid.uuid5(NAMESPACE, f"proposal:{row['id']}:{row['status']}")), "run_id": row["run_id"], "labeler": "person", "label": {"proposal": row["id"], "kind": row["kind"], "title": row["title"], "decision": row["status"], "reason": row["reason"], "by": row["decided_by"]}, "dedupe": f"proposal:{row['id']}:{row['status']}"})
            counts["labels"] += 1
    return counts


# ---------------------------------------------------------------- fine-tuning files
def _fraction(key: str) -> float:
    return int(hashlib.sha256(f"{SPLIT_SEED}:{key}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " ...[cut]"


def _call_text(step_row: dict) -> str:
    args = ", ".join(f"{k}={json.dumps(v)}" for k, v in (step_row["action"].get("args") or {}).items())
    return f"{step_row['action']['tool']}({args})"


PAST_HEADS = ("SIMILAR PAST RUNS", "FROM MEMORY", "AROUND THE SAME TIME", "THIS INCIDENT IS TAGGED", "OTHER RULES THAT ALSO RAN", "ANOTHER AI RUN", "SUGGESTIONS A PERSON", "EVERY PAST RUN", "CLOSEST PAST RUNS", "WHAT PEOPLE ACCEPTED")


def short_task(task: str) -> str:
    """The incident and what the rules found. What is known from the past stays out of a training example: it would be longer than a small model can read."""
    cut = min((i for i in (task.find("\n" + head) for head in PAST_HEADS) if i >= 0), default=len(task))
    return _cut(task[:cut].rstrip(), SETTINGS["task_chars"])


def render_query(task: str, history_steps: list[dict]) -> str:
    """The task and the calls so far, in the hackathon's layout."""
    lines = [f"Task: {short_task(task)}"]
    if not history_steps:
        return "\n".join(lines + ["No calls made yet."])
    numbered = list(enumerate(history_steps, start=1))
    k = SETTINGS["detail_steps"]
    earlier, latest = numbered[:-k], numbered[-k:]
    if earlier:
        shown = earlier[-SETTINGS["history_steps"]:]
        lines.append(f"Earlier calls ({len(earlier)} total, last {len(shown)} shown):")
        lines += [f"{n}. {_call_text(s)} - {_cut(s['reason'] or '', SETTINGS['rationale_chars'])}" for n, s in shown]
    lines.append("Latest calls and results:")
    for n, s in latest:
        lines += [f"{n}. {_call_text(s)}", "=> " + _cut(s["tool_output"] or s["saw"] or "", SETTINGS["output_chars"])]
    return "\n".join(lines)


def _first_mistake(steps: list[dict]) -> int:
    for n, s in enumerate(steps):
        if s["outcome"] in UNDONE or (s["validation"] or {}).get("change", {}) and ((s["validation"]["change"] or {}).get("status") in UNDONE):
            return n
    return len(steps)


# Agents that answer in one go. Each of their jobs is written as one call, so the same trainer and the same scoring work for them all.
SINGLE_JOBS = {
    "triage": ("sort_ticket", ("kind", "title", "priority")), "ask": ("send_closing_message", ("message",)), "reply": ("write_to_person", ("message",)), "judge": ("judge_reply", ("resolved",)),
    "note": ("write_note", ("symptom", "cause", "cause_key", "evidence", "worked", "failed", "lesson")), "read": ("write_read", ("summary", "assessment", "why", "next", "related", "used")),
}
COMPUTERS = "computers"


def agent_names() -> dict[str, str]:
    return {"support": "Support agent", "desk": "Service desk", "notes": "Memory notes", COMPUTERS: "All the computers", **{p["id"]: p["name"] for p in tickets.PEOPLE}}


def _job(kind: str, settings: dict | None, system: str | None) -> tuple[str, str]:
    """Which agent a conversation belongs to, and which of its jobs it was."""
    if kind == "agent":
        return "support", "tools"
    if kind == "person":
        return COMPUTERS, (settings or {}).get("person") or "someone"
    for owner, track, prompt in (("desk", "triage", tickets._TRIAGE), ("desk", "ask", tickets._ASK_THEM), ("desk", "reply", tickets._REPLY), ("desk", "judge", tickets._DID_IT_WORK), ("notes", "note", cards._NOTE_SYSTEM), ("notes", "read", analyst.SYSTEM)):
        if (system or "").startswith(prompt[:40]):  # older calls were recorded without saying whose they were
            return owner, track
    return "", ""


def _wanted(who: str, owner: str, track: str) -> bool:
    return who == owner or (owner == COMPUTERS and who == track)


def _single_answer(track: str, text: str) -> dict | None:
    name, fields = SINGLE_JOBS[track]
    if fields == ("message",):
        return {"name": name, "arguments": {"message": text.strip()}} if text.strip() else None
    parsed = llm.parse_json(text) or {}
    found = {field: parsed[field] for field in fields if field in parsed}
    return {"name": name, "arguments": found} if found else None


def _what_it_said(message: dict) -> str:
    """A closing message arrives as a tool call. Older ones were plain text."""
    for call in message.get("tool_calls") or []:
        try:
            found = json.loads(call["function"]["arguments"]).get("message")
        except (KeyError, TypeError, AttributeError, json.JSONDecodeError):
            continue
        if found:
            return found
    return message["content"]


def _single_example(ep: dict, track: str, messages: list[dict]) -> dict | None:
    asked, said = next((m for m in messages if m["role"] == "user"), None), next((m for m in reversed(messages) if m["role"] == "assistant"), None)
    answer = _single_answer(track, _what_it_said(said)) if asked and said else None
    if not answer:
        return None
    return {"query": _cut(asked["content"], SETTINGS["task_chars"]), "answers": [answer], "reasoning": "", "thinking": said.get("thinking") or "",
            "meta": {"episode_id": ep["id"], "run_id": ep["run_id"], "step": 1, "model": ep["model"], "capture": ep["capture"], "run_correct": True, "decision": "", "track": track, "whole": True}, "weight": 1}


def _merged_tools(have: list | None, more: list | None) -> list:
    known = {(tool.get("function") or tool)["name"]: tool for tool in [*(have or []), *(more or [])]}
    return list(known.values())


def by_agent() -> list[dict]:
    """How much has been gathered for each agent."""
    with capture.open_for_reading() as db:
        rows = db.execute("""SELECT e.kind, e.settings, (SELECT content FROM messages m WHERE m.episode_id = e.episode_id AND m.idx = 0) AS system,
                                    (SELECT count(*) FROM llm_calls c WHERE c.episode_id = e.episode_id) AS calls, (SELECT count(*) FROM llm_calls c WHERE c.episode_id = e.episode_id AND c.thinking <> '') AS thought
                             FROM episodes e WHERE e.status <> 'running'""").fetchall()
    out = {who: {"id": who, "name": name, "conversations": 0, "calls": 0, "thought": 0} for who, name in agent_names().items()}
    for row in rows:
        owner, track = _job(row["kind"], row["settings"], row["system"])
        for key in {owner, track if owner == COMPUTERS else owner} & set(out):
            out[key]["conversations"] += 1
            out[key]["calls"] += row["calls"]
            out[key]["thought"] += row["thought"]
    return list(out.values())


def export(out: Path, include: str = "solved", who: str = "support") -> dict:
    """conversations.jsonl holds every episode whole. train.jsonl and test.jsonl hold one example per decision of the agent asked for. tools.json holds each job's instructions and tools."""
    out.mkdir(parents=True, exist_ok=True)
    stats = {"episodes": 0, "examples_train": 0, "examples_test": 0, "rows_train": 0, "runs_solved": 0, "runs_skipped_unsolved": 0, "with_thinking": 0, "with_reason": 0, "who": who, "who_name": agent_names().get(who, who), "include": include, "settings": SETTINGS}
    tracks: dict[str, dict] = {}
    with capture.open_for_reading() as db, (out / "conversations.jsonl").open("w") as conversations, (out / "train.jsonl").open("w") as train, (out / "test.jsonl").open("w") as test:
        episodes = db.execute("SELECT e.*, e.episode_id::text AS id, extract(epoch FROM e.started_at) AS started FROM episodes e WHERE e.status <> 'running' ORDER BY e.started_at").fetchall()
        for ep in episodes:
            messages = db.execute("SELECT role, content, thinking, tool_calls, tool_call_id FROM messages WHERE episode_id = %s ORDER BY idx", (ep["id"],)).fetchall()
            steps = db.execute("SELECT s.*, c.thinking FROM steps s LEFT JOIN llm_calls c ON c.episode_id = s.episode_id AND c.call_idx = s.llm_call WHERE s.episode_id = %s ORDER BY step_idx", (ep["id"],)).fetchall()
            labels = db.execute("SELECT labeler, label FROM labels WHERE episode_id = %(id)s OR (run_id = %(run)s AND labeler = 'person' AND %(run)s <> '')", {"id": ep["id"], "run": ep["run_id"] or ""}).fetchall()
            tools_row = db.execute("SELECT items FROM inputs WHERE episode_id = %s AND source = 'tools' LIMIT 1", (ep["id"],)).fetchone()
            conversations.write(json.dumps({"episode_id": ep["id"], "run_id": ep["run_id"], "kind": ep["kind"], "capture": ep["capture"], "model": ep["model"], "outcome": ep["outcome"], "labels": labels,
                                            "messages": [{k: v for k, v in m.items() if v not in (None, "")} for m in messages], "steps": [{k: s[k] for k in ("step_idx", "reason", "action", "outcome", "saw", "tool_output", "given_back")} | {"thinking": s["thinking"]} for s in steps]}, default=str, ensure_ascii=False) + "\n")
            stats["episodes"] += 1
            system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
            owner, track = _job(ep["kind"], ep["settings"], system)
            if not _wanted(who, owner, track):
                continue
            split = "test" if _fraction(ep["run_id"] or ep["id"]) < SETTINGS["test_fraction"] else "train"
            if track in SINGLE_JOBS:
                single = _single_example(ep, track, messages)
                if single:
                    name, fields = SINGLE_JOBS[track]
                    tracks[track] = {"system": system, "tools": [{"name": name, "parameters": {"properties": {field: {} for field in fields}}}]}
                    (train if split == "train" else test).write(json.dumps(single, ensure_ascii=False) + "\n")
                    stats[f"examples_{split}"] += 1
                    stats["rows_train"] += 1 if split == "train" else 0
                    stats["with_thinking"] += bool(single["thinking"])
                continue
            if not steps:
                continue
            tracks[track] = {"system": system, "tools": _merged_tools((tracks.get(track) or {}).get("tools"), tools_row and tools_row["items"])}
            solved = (ep["outcome"] or {}).get("outcome") == "fixed" if owner == "support" else ep["status"] == "done"
            if include == "solved" and not solved:
                stats["runs_skipped_unsolved"] += 1
                continue
            stats["runs_solved"] += solved
            usable = [s for s in steps if s["outcome"] not in SKIPPED_OUTCOMES]
            usable = usable if solved else usable[:_first_mistake(usable)]
            task = next((m["content"] for m in messages if m["role"] == "user"), "")
            acted = False
            for i, s in enumerate(usable):
                decision = "report" if s["kind"] == "finish" else "change" if s["kind"] == "change" and not acted else ""
                acted = acted or s["kind"] == "change"
                weight = SETTINGS["decision_weight"] if decision and split == "train" else 1
                example = {"query": render_query(task, usable[:i]), "answers": [{"name": s["action"]["tool"], "arguments": s["action"].get("args") or {}}], "reasoning": s["reason"] or "", "thinking": (s["thinking"] or "") if ep["capture"] == "live" else "",  # an older run has one shortened summary, not thinking for this step
                           
                           "meta": {"episode_id": ep["id"], "run_id": ep["run_id"], "step": i + 1, "model": ep["model"], "capture": ep["capture"], "run_correct": solved, "decision": decision, "track": track}, "weight": weight}
                (train if split == "train" else test).write(json.dumps(example, ensure_ascii=False) + "\n")
                stats[f"examples_{split}"] += 1
                stats["rows_train"] += weight if split == "train" else 0
                stats["with_thinking"] += bool(example["thinking"])
                stats["with_reason"] += bool(s["reason"])
    (out / "tools.json").write_text(json.dumps({"tracks": tracks}, indent=1))
    (out / "stats.json").write_text(json.dumps(stats, indent=1))
    return stats


# ---------------------------------------------------------------- one complete copy
def _tar(source: Path, target: Path) -> None:
    with tarfile.open(target, "w:gz") as tar:
        tar.add(source, arcname=source.name)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(root: Path = SNAPSHOTS) -> dict:
    """Everything collected, as one folder: the training database, every run, the search index, the rules and the fine-tuning files."""
    backfill()
    name = time.strftime("%Y%m%dT%H%M%S")
    out = root / name
    out.mkdir(parents=True)
    values = dict(line.strip().split("=", 1) for line in capture.SECRETS.read_text().splitlines() if "=" in line)
    try:
        with (out / "training.dump").open("wb") as dump:
            subprocess.run(["docker", "exec", CONTAINER, "pg_dump", "-U", values["POSTGRES_USER"], "-d", values["POSTGRES_DB"], "-Fc"], stdout=dump, stderr=subprocess.PIPE, check=True, timeout=900)
        _tar(store.DATA_DIR, out / "runs.tar.gz")
        _tar(DEFAULT_RULES_DIR, out / "rules.tar.gz")
        source, copy = sqlite3.connect(history.DB), sqlite3.connect(out / "history.sqlite")
        with copy:
            source.backup(copy)
        source.close()
        copy.close()
        stats = export(out / "dataset")
    except Exception:
        shutil.rmtree(out, ignore_errors=True)
        raise
    files = {str(p.relative_to(out)): {"bytes": p.stat().st_size, "sha256": _sha(p)} for p in sorted(out.rglob("*")) if p.is_file()}
    with capture.open_for_reading() as db:
        counts = totals(db)
    manifest = {"name": name, "taken": time.time(), "totals": counts, "dataset": stats, "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str))
    return _summary(manifest, out)


def _summary(manifest: dict, folder: Path) -> dict:
    return {"name": manifest["name"], "taken": manifest["taken"], "path": str(folder), "bytes": sum(f["bytes"] for f in manifest["files"].values()), "episodes": manifest["totals"]["episodes"], "steps": manifest["totals"]["steps"], "runs": manifest["totals"]["runs"], "examples": manifest["dataset"]["examples_train"] + manifest["dataset"]["examples_test"]}


def snapshots(root: Path = SNAPSHOTS) -> list[dict]:
    out = []
    for path in sorted(root.glob("*/manifest.json"), reverse=True)[:10]:
        try:
            out.append(_summary(json.loads(path.read_text()), path.parent))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return out
