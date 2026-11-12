"""Putting rules into the rule set, and moving them between modes.

The CLI, the review page and the AI's proposals all come through here, so a rule
that gets in has passed the same checks whichever way it arrived.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path

import yaml

from ts.lint import Issue, errors, lint, reachable_devices
from ts.ruleset import load_ruleset
from ts.schema import MODES, parse_rule
from ts.vocab import DEVICES


class RuleRefused(ValueError):
    pass


def check(root: Path, raws: list[dict]) -> tuple[list, list[Issue]]:
    """Parse rules and lint them against what is installed. Returns (parsed rules, the issues they would add)."""
    rs = load_ruleset(root)
    parsed = [parse_rule(copy.deepcopy(raw), raw.get("id", "<rule>")) for raw in raws]
    replacing = {rule.id for rule in parsed}
    from ts import monitors
    carried = monitors.carried_tags()
    before = set(lint(rs.parsed, carried))
    after = lint([r.rule for r in rs.rules if r.id not in replacing] + parsed, carried)
    return parsed, [issue for issue in after if issue not in before]


def _approved(raw: dict, mode: str, by: str) -> dict:
    source = {**(raw.get("source") or {"by": by}), "approved_by": by, "approved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    return {**raw, "mode": mode, "source": source}


def _for_its_platforms(raw: dict) -> dict:
    """A fix whose change or whose verify works on some kinds of device only (eos, frr, linux) is matched to
    those kinds, so a cause on any other kind of device looks for the fix written for it instead of picking
    this one and answering unsupported. A fix that names its platform, role or device already is left alone."""
    if raw.get("table") != "fix" or any(k in (raw.get("match") or {}) for k in ("platform", "role", "device")):
        return raw
    try:
        rule = parse_rule(copy.deepcopy(raw), raw.get("id", "<rule>"))
    except Exception:  # noqa: BLE001 - check() reports why it does not parse
        return raw
    do = rule.action.do
    acts_on = do.args.get("device")
    calls = [do, *(c.check for c in rule.verify if acts_on is not None and c.check.args.get("device") == acts_on)]
    limited = [set(c.block.platforms) for c in calls if c.block.device_bound]
    works = set.intersection(*limited) if limited else set()
    seen = {DEVICES[d][1] for d in reachable_devices(rule.match)}
    if not works or seen <= works:
        return raw
    return {**raw, "match": {**raw["match"], "platform": sorted(works)}}


def install(root: Path, raws: list[dict], mode: str, by: str) -> list[str]:
    """Put these rules in, all or none. Returns their ids."""
    if mode not in MODES:
        raise RuleRefused(f"{mode!r} is not a mode")
    root = Path(root)
    rs = load_ruleset(root)
    taken = [raw["id"] for raw in raws if rs.get(raw["id"])]
    if taken:
        raise RuleRefused(f"Already installed: {', '.join(taken)}.")
    ready = [_for_its_platforms(_approved(raw, mode, by)) for raw in raws]
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
    _pipelines_follow(root)
    return written


def _pipelines_follow(root: Path) -> None:
    from ts import pipelines
    pipelines.sync_soon(root)


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
    _pipelines_follow(root)
