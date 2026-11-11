"""Everything that goes into and comes out of the model, kept for fine-tuning.

Nothing here may get in the way of the work. If the database is away, rows wait in a file and go in
later, and any other failure is written to a log and forgotten. Secrets are taken out before anything is stored.
The AI has no way to read any of this, so the ground truth from drills stays hidden from it.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ts import store

ROOT = Path(__file__).resolve().parent.parent
SECRETS = Path(os.environ.get("TS_TRAINING_ENV", ROOT / "secrets" / "trainingdb.env"))
WAITING = Path(os.environ.get("TS_TRAINING_WAITING", ROOT / "data" / "capture-waiting.jsonl"))
PROBLEMS = WAITING.with_name("capture-problems.log")
HOST, PORT = os.environ.get("TS_TRAINING_HOST", "127.0.0.1"), int(os.environ.get("TS_TRAINING_PORT", "5444"))
ENABLED = os.environ.get("TS_CAPTURE", "on") != "off"
TRY_AGAIN_AFTER = 60  # seconds before knocking on a database that did not answer

JSON_COLUMNS = {"settings", "outcome", "items", "tool_calls", "usage", "request", "raw_response", "action", "validation", "state", "label", "spec", "record"}
ON_CONFLICT = {
    "runs": " ON CONFLICT (run_id) DO UPDATE SET happened_at = EXCLUDED.happened_at, device = EXCLUDED.device, event = EXCLUDED.event, state = EXCLUDED.state, "
            "handled_by = EXCLUDED.handled_by, label = EXCLUDED.label, tags = EXCLUDED.tags, record = EXCLUDED.record, updated_at = now()",
    "episodes": " ON CONFLICT (episode_id) DO UPDATE SET status = EXCLUDED.status, outcome = EXCLUDED.outcome, ended_at = EXCLUDED.ended_at, settings = EXCLUDED.settings",
    "fault_truth": " ON CONFLICT (fault_id) DO UPDATE SET cleared_at = EXCLUDED.cleared_at",
}

_SECRET = re.compile(r"(?i)\b(password|passwd|secret|community|message-digest-key|authentication-key|auth-key|api[_-]?key)(\s+|[\"']?\s*[=:]\s*[\"']?)((?:\d{1,3}|md5|sha\d*)\s+)*[^\s,;\"']+")
_HASH = re.compile(r"\$\d\$[./A-Za-z0-9$]{8,}")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{10,}")
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,})\b")

# The headings the prompt is built from: where a section starts, what to call it, and what kind of data it is.
SECTIONS = (("EVERY PAST RUN", "Every past run, grouped by cause", "retrieved"), ("CLOSEST PAST RUNS", "The closest past cases", "retrieved"), ("WHAT PEOPLE ACCEPTED", "What people accepted", "retrieved"),
            ("INCIDENT at", "The alarm", "incident"), ("CHECKS", "Checks the rules ran", "rules"), ("CONCLUSION", "What the rules concluded", "rules"), ("REPAIR:", "Repair attempted", "rules"),
            ("HISTORY:", "How often this has happened", "retrieved"), ("SIMILAR PAST RUNS", "Similar past runs", "retrieved"), ("FROM MEMORY", "From memory", "retrieved"),
            ("AROUND THE SAME TIME", "Around the same time", "live"), ("THIS INCIDENT IS TAGGED", "Tags", "incident"), ("OTHER RULES THAT ALSO RAN", "Other rules that ran", "rules"),
            ("ANOTHER AI RUN", "What an earlier AI run found", "retrieved"), ("SUGGESTIONS A PERSON ALREADY REJECTED", "Rejected by a person", "retrieved"))
ALONE_KINDS = {"desk": "desk", "notes": "notes", "support": "agent"}  # which agent a single call with no conversation around it belongs to
BRIEF_LIMIT = 120000  # analyst.brief cuts what it writes to this many characters

_lock = threading.RLock()
_local = threading.local()
_away_until = 0.0


@dataclass
class Episode:
    id: str
    row: dict
    seen: int = 0
    calls: int = 0
    sections: int = 0
    tools: str = ""
    changes: list = field(default_factory=list)


def _note(problem: str) -> None:
    try:
        PROBLEMS.parent.mkdir(parents=True, exist_ok=True)
        with PROBLEMS.open("a") as out:
            out.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {problem}\n")
    except OSError:
        pass


def _quietly(work):
    """The decorator on everything the rest of the system calls."""
    @functools.wraps(work)
    def run(*args, **kwargs):
        if not ENABLED:
            return None
        try:
            return work(*args, **kwargs)
        except Exception as exc:  # whatever went wrong here, the incident comes first
            _note(f"{work.__name__}: {type(exc).__name__}: {exc}")
            return None
    return run


def _when(moment: float | None = None) -> str:
    return datetime.fromtimestamp(moment if moment is not None else time.time(), tz=timezone.utc).isoformat()


def clean(value):
    """The value with secrets taken out and the one character the database cannot hold."""
    if isinstance(value, str):
        text = _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}<removed>", value.replace("\x00", ""))
        return _TOKEN.sub("<removed>", _BEARER.sub("Bearer <removed>", _HASH.sub("<removed>", text)))
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


def _dsn() -> str:
    values = dict(line.strip().split("=", 1) for line in SECRETS.read_text().splitlines() if "=" in line and not line.startswith("#"))
    return f"host={HOST} port={PORT} dbname={values['POSTGRES_DB']} user={values['POSTGRES_USER']} password={values['POSTGRES_PASSWORD']} connect_timeout=2"


def _connection():
    import psycopg

    connection = getattr(_local, "connection", None)
    if connection is None or connection.closed:
        connection = psycopg.connect(_dsn(), autocommit=True)
        _local.connection = connection
    return connection


def open_for_reading():
    """A connection of its own, for one web request or one export. The caller closes it."""
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row)


def _insert(table: str, row: dict) -> None:
    from psycopg.types.json import Jsonb

    columns = list(row)
    values = [Jsonb(row[c]) if c in JSON_COLUMNS and row[c] is not None else row[c] for c in columns]
    placeholders = ", ".join(["%s"] * len(columns))
    _connection().execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})" + ON_CONFLICT.get(table, " ON CONFLICT DO NOTHING"), values)


def _is_away(exc: Exception) -> bool:
    import psycopg

    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError, OSError))


def _keep_for_later(table: str, row: dict) -> None:
    WAITING.parent.mkdir(parents=True, exist_ok=True)
    with WAITING.open("a") as out:
        out.write(json.dumps({"table": table, "row": row}, default=str) + "\n")


def _catch_up() -> None:
    """Rows that waited go in first, so they stay in order. Taking the file away first keeps two processes from sending the same rows."""
    if not WAITING.exists():
        return
    taken = WAITING.with_suffix(f".{os.getpid()}.sending")
    try:
        WAITING.replace(taken)
    except FileNotFoundError:
        return
    lines = taken.read_text().splitlines()
    for n, line in enumerate(lines):
        item = json.loads(line)
        try:
            _insert(item["table"], item["row"])
        except Exception as exc:
            if _is_away(exc):
                with WAITING.open("a") as out:
                    out.write("".join(rest + "\n" for rest in lines[n:]))
                taken.unlink()
                raise
            _note(f"dropped a waiting {item['table']} row: {type(exc).__name__}: {exc}")
    taken.unlink()


def _write(table: str, row: dict) -> None:
    global _away_until
    row = clean(row)
    with _lock:
        if time.time() < _away_until:
            _keep_for_later(table, row)
            return
        try:
            _catch_up()
            _insert(table, row)
        except Exception as exc:
            if _is_away(exc):
                _local.connection = None
                _away_until = time.time() + TRY_AGAIN_AFTER
                _note(f"database away: {type(exc).__name__}")
                _keep_for_later(table, row)
            else:
                _note(f"could not store a {table} row: {type(exc).__name__}: {exc}")


def current() -> Episode | None:
    return getattr(_local, "episode", None)


def _begin(kind: str, run_id: str, model: str, settings: dict | None, how: str, started: float | None, episode_id: str | None) -> Episode:
    row = {"episode_id": episode_id or str(uuid.uuid4()), "run_id": run_id, "kind": kind, "capture": how, "model": model, "settings": settings or {}, "status": "running", "started_at": _when(started)}
    _write("episodes", row)
    return Episode(row["episode_id"], row)


@_quietly
def start(kind: str, run_id: str = "", model: str = "", settings: dict | None = None) -> Episode:
    """From here until close, every model call on this thread belongs to this episode."""
    if current():
        close(current(), "abandoned")
    episode = _begin(kind, run_id, model, settings, "live", None, None)
    _local.episode = episode
    return episode


def split_prompt(prompt: str) -> list[dict]:
    """The prompt cut at its headings, so each part can be seen and sized."""
    parts: list[dict] = []
    for line in prompt.splitlines():
        head = next((s for s in SECTIONS if line.startswith(s[0])), None)
        if head or not parts:
            parts.append({"section": head[1] if head else "Prompt", "source": head[2] if head else "incident", "lines": []})
        parts[-1]["lines"].append(line)
    return [{"section": p["section"], "source": p["source"], "body": "\n".join(p["lines"]).strip()} for p in parts]


def was_cut(prompt: str) -> bool:
    return len(prompt.split("\n\nTHIS INCIDENT IS TAGGED")[0]) >= BRIEF_LIMIT


def sections(system: str, prompt: str, tool_list: list[dict], record: dict) -> list[dict]:
    """What the model is about to be given, and what stands behind it."""
    behind = {"Similar past runs": record.get("similar"), "From memory": record.get("retrieved"), "Around the same time": record.get("context")}
    out = [{"section": "Instructions", "source": "instructions", "body": system}]
    if tool_list:
        out.append({"section": "Tools it can call", "source": "tools", "body": json.dumps(tool_list), "items": tool_list})
    out += [{**part, "items": behind.get(part["section"])} for part in split_prompt(prompt)]
    out.append({"section": "Everything retrieval returned", "source": "snapshot", "sent": False,
                "items": {"retrieval": record.get("retrieval"), "memory": record.get("retrieved"), "similar": record.get("similar"), "context": record.get("context"), "sources": record.get("sources"), "pattern": record.get("pattern")}})
    out.append({"section": "The whole run so far", "source": "snapshot", "sent": False, "items": {k: v for k, v in record.items() if k != "agent"}})
    return out


@_quietly
def inputs(parts: list[dict]) -> None:
    episode = current()
    if not episode:
        return
    for part in parts:
        body = part.get("body")
        _write("inputs", {"episode_id": episode.id, "section_idx": episode.sections, "section": part["section"], "source": part["source"], "sent": part.get("sent", True),
                          "chars": len(body) if body else len(json.dumps(part.get("items") or "", default=str)), "body": body, "items": part.get("items")})
        episode.sections += 1


def _thinking(message: dict) -> str:
    return message.get("reasoning_content") or message.get("reasoning") or ""


def _message(episode: Episode, idx: int, message: dict, moment: str) -> None:
    _write("messages", {"episode_id": episode.id, "idx": idx, "ts": moment, "role": message.get("role", ""), "content": message.get("content") or "", "thinking": _thinking(message),
                        "tool_calls": message.get("tool_calls"), "tool_call_id": message.get("tool_call_id")})


def _tell(episode: Episode, messages: list[dict]) -> None:
    moment = _when()
    for idx in range(episode.seen, len(messages)):
        _message(episode, idx, messages[idx], moment)
    episode.seen = max(episode.seen, len(messages))


@_quietly
def llm_call(body: dict, reply: dict, seconds: float, role: str = "") -> None:
    """One request to the model and everything it sent back. `role` says which agent asked."""
    episode, alone = current(), False
    if not episode:  # nothing may reach the model without being recorded
        episode, alone = _begin(ALONE_KINDS.get(role) or ("person" if re.fullmatch(r"h\d+", role) else "other"), "", body.get("model", ""), {"role": role} if role else {}, "live", None, None), True
    episode.row["model"] = episode.row.get("model") or body.get("model", "")
    messages, tool_list = body["messages"], body.get("tools")
    _tell(episode, messages)
    message = reply["choices"][0]["message"]
    _message(episode, len(messages), {"role": "assistant", **message}, _when())
    episode.seen = len(messages) + 1
    tools_hash = hashlib.sha256(json.dumps(tool_list, sort_keys=True).encode()).hexdigest()[:16] if tool_list else ""
    _write("llm_calls", {"episode_id": episode.id, "call_idx": episode.calls, "messages_sent": len(messages), "thinking": _thinking(message), "content": message.get("content") or "",
                         "finish": reply["choices"][0].get("finish_reason"), "usage": reply.get("usage") or {}, "latency_ms": int(seconds * 1000),
                         "request": {k: body.get(k) for k in ("model", "temperature", "max_tokens")} | {"tools_sha": tools_hash}, "raw_response": reply})
    episode.calls += 1
    if alone:
        _write("episodes", {**episode.row, "status": "done", "ended_at": _when()})


@_quietly
def step(entry: dict, given_back: str, change: dict | None = None) -> None:
    """One tool call. `entry` is what the tool layer recorded, `given_back` the exact text returned to the model."""
    episode = current()
    if not episode:
        return
    _write("steps", {"episode_id": episode.id, "step_idx": entry["n"], "ts": _when(entry.get("started")), "llm_call": episode.calls - 1, "reason": entry.get("reason") or "",
                     "action": {"tool": entry["tool"], "args": entry.get("args") or {}}, "kind": entry.get("kind"), "outcome": entry.get("outcome"), "saw": entry.get("saw") or "",
                     "tool_output": entry.get("output") or "", "given_back": given_back, "validation": {"ran": entry.get("ran") or [], "change": change}, "latency_ms": entry.get("ms")})
    if change and "problems_before" in change:
        before = change["problems_before"]
        after = sorted((set(before) - set(change.get("healed") or [])) | set(change.get("new") or []))
        run_id = episode.row["run_id"]
        for phase, problems in (("pre_change", before), ("post_change", after)):
            _write("state_snapshots", {"snapshot_id": str(uuid.uuid4()), "run_id": run_id, "episode_id": episode.id, "step_idx": entry["n"], "phase": phase, "state": {"problems": problems, "kept": change.get("status")}})


@_quietly
def state(phase: str, value: dict) -> None:
    episode = current()
    if episode:
        _write("state_snapshots", {"snapshot_id": str(uuid.uuid4()), "run_id": episode.row["run_id"], "episode_id": episode.id, "phase": phase, "state": value})


@_quietly
def close(episode: Episode | None, status: str, outcome: dict | None = None, messages: list[dict] | None = None) -> None:
    """The end of an episode. `messages` is the conversation, because the last tool results were never sent back to the model."""
    if not episode:
        return
    if messages is not None:
        _tell(episode, messages)
    _write("episodes", {**episode.row, "status": status, "outcome": outcome, "ended_at": _when()})
    if outcome:
        _write("labels", {"label_id": str(uuid.uuid4()), "run_id": episode.row["run_id"], "episode_id": episode.id, "labeler": "system", "label": outcome, "dedupe": f"outcome:{episode.id}"})
    if current() is episode:
        _local.episode = None


def run_row(record: dict) -> dict:
    event, result = record["event"], record["result"]
    position, handled = store.verdict(record)
    return {"run_id": record["id"], "happened_at": _when(event["time"]), "device": event.get("device"), "event": event.get("event", "interface.down"), "state": position, "handled_by": handled,
            "label": result.get("label") or result.get("kind"), "tags": list(record.get("tags") or []), "record": record}


@_quietly
def run(record: dict) -> None:
    """The whole record of an investigation, AI or not."""
    _write("runs", run_row(record))


@_quietly
def label(run_id: str, labeler: str, value: dict, dedupe: str | None = None) -> None:
    _write("labels", {"label_id": str(uuid.uuid4()), "run_id": run_id, "labeler": labeler, "label": value, "dedupe": dedupe})


@_quietly
def fault(drill: str, spec: dict, injected_at: float, cleared_at: float | None = None, fault_id: str | None = None) -> str:
    """Ground truth from a drill. The same id again marks it cleared."""
    fault_id = fault_id or str(uuid.uuid4())
    _write("fault_truth", {"fault_id": fault_id, "drill": drill, "spec": spec, "injected_at": _when(injected_at), "cleared_at": _when(cleared_at) if cleared_at else None})
    return fault_id
