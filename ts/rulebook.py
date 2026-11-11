"""Putting rules into the rule set, and moving them between modes.

The CLI, the review page and the AI's proposals all come through here, so a rule
that gets in has passed the same checks whichever way it arrived.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path

import yaml

from ts.lint import Issue, errors, lint
from ts.ruleset import load_ruleset
from ts.schema import MODES, parse_rule


class RuleRefused(ValueError):
    pass


def check(root: Path, raws: list[dict]) -> tuple[list, list[Issue]]:
    """Parse rules and lint them against what is installed. Returns (parsed rules, the issues they would add)."""
    rs = load_ruleset(root)
    parsed = [parse_rule(copy.deepcopy(raw), raw.get("id", "<rule>")) for raw in raws]
    replacing = {rule.id for rule in parsed}
    before = set(lint(rs.parsed))
    after = lint([r.rule for r in rs.rules if r.id not in replacing] + parsed)
    return parsed, [issue for issue in after if issue not in before]


def _approved(raw: dict, mode: str, by: str) -> dict:
    source = {**(raw.get("source") or {"by": by}), "approved_by": by, "approved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    return {**raw, "mode": mode, "source": source}


def install(root: Path, raws: list[dict], mode: str, by: str) -> list[str]:
    """Put these rules in, all or none. Returns their ids."""
    if mode not in MODES:
        raise RuleRefused(f"{mode!r} is not a mode")
    root = Path(root)
    rs = load_ruleset(root)
    taken = [raw["id"] for raw in raws if rs.get(raw["id"])]
    if taken:
        raise RuleRefused(f"Already installed: {', '.join(taken)}.")
    ready = [_approved(raw, mode, by) for raw in raws]
    parsed, issues = check(root, ready)
    if errors(issues):
        raise RuleRefused("; ".join(i.message for i in errors(issues)))
    written = []
    for raw, rule in zip(ready, parsed):
        dest = root / rule.table / f"{rule.id}.yaml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(raw, sort_keys=False))
        tmp.replace(dest)
        written.append(rule.id)
    return written


def set_mode(root: Path, rule_id: str, mode: str, by: str) -> None:
    """Move an installed rule to another mode. Only the mode line changes, so the rest of the file stays as written."""
    if mode not in MODES:
        raise RuleRefused(f"{mode!r} is not a mode")
    rs = load_ruleset(root)
    loaded = rs.get(rule_id)
    if loaded is None:
        raise RuleRefused(f"There is no rule {rule_id!r}.")
    raw = {**loaded.raw, "mode": mode}
    if loaded.raw.get("source", {}).get("by") == "ai":
        raw["source"] = {**loaded.raw["source"], "approved_by": by, "approved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _parsed, issues = check(root, [raw])
    if errors(issues):
        raise RuleRefused("; ".join(i.message for i in errors(issues)))
    tmp = loaded.path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(raw, sort_keys=False))
    tmp.replace(loaded.path)
