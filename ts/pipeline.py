"""What happens once the rules have reached the end of an investigation.

Repair with the rules, gather what else was going on, compare with every earlier run.
If the rules could not resolve it, the AI takes over. Then the model's read and the
email. Each part can fail by itself without losing the run or stopping the others.
"""

from __future__ import annotations

from ts import agent, analyst, capture, cards, fixer, history, memory, report, scrub, sources, tags
from ts.ruleset import DEFAULT_RULES_DIR

KEEP_DOCS = 60  # how many context items stay on the run for the UI


def _safely(record: dict, log):
    def safely(name, work, default=None):
        try:
            return work()
        except Exception as exc:  # the investigation itself already succeeded, so keep what we have
            log(f"{record['id']}: {name} failed: {type(exc).__name__}: {exc}")
            return default
    return safely


def tag(record: dict, source: str = "auto") -> list[str]:
    for name in tags.for_record(record):
        history.tag_run(record["id"], name, source)
    record["tags"] = history.tags_of(record["id"])
    return record["tags"]


def finish(record: dict, rules, log=print, inline: bool = True, root=DEFAULT_RULES_DIR) -> None:
    """Everything up to the hand-over. With inline False, a run the AI should take is left marked queued for the caller to start."""
    safely = _safely(record, log)
    repair = safely("repair", lambda: fixer.attempt(record, rules, log))
    if repair:
        record["repair"] = repair
    docs, status = safely("sources", lambda: sources.gather(record["event"]), ([], {}))
    record["sources"] = status
    record["context"] = docs[-KEEP_DOCS:]
    similar = safely("history", lambda: history.similar(record, involved=memory.involved(record)), []) or []
    record["similar"] = [{"ref": f"R{n}", **{k: s[k] for k in ("id", "time", "device", "subject", "result_kind", "label", "text")}} for n, s in enumerate(similar, 1)]
    record["retrieved"], record["retrieval"] = safely("memory", lambda: memory.recall(record), ([], None))
    record["pattern"] = safely("pattern", lambda: history.pattern(record, docs), {"verdict": "none", "note": "", "count_24h": 1, "count_7d": 1})
    safely("index", lambda: history.add_run(record))
    safely("index", lambda: history.add_docs(docs))
    safely("tags", lambda: tag(record))
    safely("past runs", lambda: cards.prepare(record))
    why = safely("handover", lambda: agent.needed(record))
    if why:
        stopped = safely("handover", lambda: agent.blocked(record), "The AI could not be started.")
        if stopped:
            record["agent"] = {"status": "skipped", "why": why, "note": stopped, "outcome": "needs_person", "calls": [], "changes": [], "proposals": []}
        else:
            record["agent"] = {"status": "queued", "why": why, "calls": [], "changes": [], "proposals": []}
            if inline:
                take_over(record, rules, log, root)
            return
    conclude(record, log)


def take_over(record: dict, rules, log=print, root=DEFAULT_RULES_DIR) -> None:
    """The AI's turn, then the report."""
    safely = _safely(record, log)
    done = safely("ai", lambda: agent.take_over(record, rules, log, root=root) or True)
    if not done:
        record["agent"] = {**record.get("agent", {}), "status": "failed", "error": "The AI step crashed."}
    conclude(record, log)


def conclude(record: dict, log=print) -> None:
    safely = _safely(record, log)
    if report.should_send(record):
        if not record.get("analysis"):
            analysis = safely("analysis", lambda: analyst.analyse(record, record.get("context") or [], record.get("similar") or [], record.get("pattern") or {}, record.get("retrieved") or []))
            if analysis:
                record["analysis"] = scrub.deep(analysis)
            else:
                record["ai_note"] = "The AI was asked to read this run, but it gave no usable answer."
        record["email"] = safely("email", lambda: report.send(record), {"sent": False, "why": "the mailer failed"})
    else:
        record["ai_note"] = "Nothing here needed a report, so the AI was not asked. Rules and checks did all of it."
    safely("index", lambda: history.add_run(record))
    safely("past runs", lambda: cards.remember_soon(record))
    capture.run(record)
