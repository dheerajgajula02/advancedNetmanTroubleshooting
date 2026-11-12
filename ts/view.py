"""Plain-data views of the installed rules, shared by the CLI and the web UI."""

from __future__ import annotations

from ts import jenkins_api
from ts.lint import Issue, base_vars, cause_overlaps, lint, reachable_devices
from ts.registry import ACTIONS, CHECKS, Block
from ts.ruleset import RuleSet
from ts.schema import Condition, DiagnoseRule, FilterRule, FixRule, Outcome, placeholders
from ts.vocab import COMMON_FIELDS, DEVICES

TABLE_ORDER = {"filter": 0, "diagnose": 1, "fix": 2}


def sort_key(rule):
    return (TABLE_ORDER[rule.table], -rule.priority, rule.id)


def match_text(match: dict) -> str:
    if not match:
        return "*"
    parts = []
    for key, value in match.items():
        parts.append(f"{key}={','.join(value) if isinstance(value, list) else value}")
    return " ".join(parts)


def fixes_for(label: str, rules) -> list[str]:
    out = []
    for r in sorted(rules, key=sort_key):
        if isinstance(r, FixRule) and r.mode != "retired":
            pats = r.match.get("cause", [])
            if any(cause_overlaps(label, p) for p in (pats if isinstance(pats, list) else [pats])):
                out.append(r.id)
    return out


def producers_of(rule: FixRule, rules) -> list[str]:
    pats = rule.match.get("cause", [])
    pats = pats if isinstance(pats, list) else [pats]
    out = []
    for r in sorted(rules, key=sort_key):
        if isinstance(r, DiagnoseRule) and r.mode != "retired":
            labels = [o.target for s in r.steps for o in s.then.values() if o.kind == "cause"]
            if r.end.kind == "cause":
                labels.append(r.end.target)
            if any(cause_overlaps(l, p) for l in labels for p in pats):
                out.append(r.id)
    return out


def result_text(rule) -> str:
    if isinstance(rule, FilterRule):
        cond = " and ".join(c.render() for c in rule.when)
        return f"when {cond} -> {rule.verdict}" if cond else rule.verdict
    if isinstance(rule, DiagnoseRule):
        causes = sorted({o.target for s in rule.steps for o in s.then.values() if o.kind == "cause"})
        return f"{len(rule.steps)} steps -> {', '.join(causes) if causes else 'no causes'}"
    return f"{rule.action.do.render()} (risk {rule.risk})"


def _outcome(o: Outcome, rules) -> dict:
    d = {"kind": o.kind, "target": o.target, "params": o.params, "text": o.render()}
    if o.kind == "cause":
        d["fixes"] = fixes_for(o.target, rules)
    return d


def _condition(c: Condition) -> dict:
    return {"text": c.render(), "check": c.check.name, "expect": c.expect}


def block_view(b: Block) -> dict:
    return {
        "kind": b.kind, "name": b.name, "description": b.description,
        "params": [{"name": p.name, "many": p.many, "optional": p.optional} for p in b.params],
        "outcomes": list(b.outcomes), "produces": list(b.produces),
        "platforms": sorted(b.platforms), "undo": b.undo,
    }


def rule_view(rule, rules, issues: list[Issue]) -> dict:
    d = {
        "id": rule.id, "table": rule.table, "priority": rule.priority, "version": rule.version,
        "mode": rule.mode, "description": rule.description, "match": rule.match,
        "match_text": match_text(rule.match), "result_text": result_text(rule),
        "source": rule.source.model_dump(exclude_none=True), "tags": rule.tags, "position": rule.position, "title": getattr(rule, "title", "") or "",
        "issues": [{"level": i.level, "message": i.message} for i in issues if i.rule_id == rule.id],
        "reachable": sorted(reachable_devices(rule.match)),
        "fields": _example_fields(rule),
    }
    if isinstance(rule, (DiagnoseRule, FixRule)):
        d["pipeline"] = getattr(rule, "pipeline", None) or rule.id
        if rule.mode != "proposed":
            d["pipeline_url"] = jenkins_api.link(d["pipeline"])
    if isinstance(rule, FilterRule):
        d.update(when=[_condition(c) for c in rule.when], verdict=rule.verdict)
    elif isinstance(rule, DiagnoseRule):
        steps = []
        for n, s in enumerate(rule.steps, 1):
            block = s.check.block
            steps.append({
                "n": n, "id": s.id, "check": s.check.render(), "block": block.name,
                "description": block.description, "produces": list(block.produces),
                "outcomes": {name: _outcome(o, rules) for name, o in s.then.items()},
                "continue_on": [o for o in block.all_outcomes
                                if o not in s.then or s.then[o].kind == "next"],
            })
        d.update(steps=steps, end=_outcome(rule.end, rules))
    else:
        d.update(
            require=[_condition(c) for c in rule.require],
            action={"do": rule.action.do.render(), "do_args": rule.action.do.args,
                    "undo": rule.action.undo.render() if rule.action.undo else None,
                    "undo_args": rule.action.undo.args if rule.action.undo else None,
                    "undo_policy": rule.action.do.block.undo},
            verify=[_condition(c) for c in rule.verify], risk=rule.risk,
            retire=rule.retire.model_dump(), produced_by=producers_of(rule, rules),
        )
    return d


def state(ruleset: RuleSet) -> dict:
    rules = sorted(ruleset.parsed, key=sort_key)
    issues = lint(rules)
    return {
        "root": str(ruleset.root),
        "load_errors": ruleset.errors,
        "issues": [{"level": i.level, "rule_id": i.rule_id, "message": i.message} for i in issues],
        "rules": [rule_view(r, rules, issues) for r in rules],
        "checks": [block_view(b) for b in sorted(CHECKS.values(), key=lambda b: b.name)],
        "actions": [block_view(b) for b in sorted(ACTIONS.values(), key=lambda b: b.name)],
        "devices": {d: {"role": role, "platform": plat} for d, (role, plat) in DEVICES.items()},
        "jenkins": {"folder": jenkins_api.FOLDER, "url": jenkins_api.link()},
    }


def _example_fields(rule) -> list[str]:
    """Inputs the UI offers for trying a rule: event fields (diagnose) or the values a fix uses."""
    if isinstance(rule, FixRule):
        return sorted(_all_placeholders(rule) - {"device"})
    return sorted((base_vars(rule.match) - COMMON_FIELDS) & _all_placeholders(rule))


def _all_placeholders(rule) -> set[str]:
    used = set()
    if isinstance(rule, FilterRule):
        for c in rule.when:
            used |= placeholders(c.check.args)
    elif isinstance(rule, DiagnoseRule):
        for s in rule.steps:
            used |= placeholders(s.check.args)
            for o in s.then.values():
                used |= placeholders(o.target) | placeholders(o.params)
        used |= placeholders(rule.end.target)
    else:
        for c in rule.require + rule.verify:
            used |= placeholders(c.check.args)
        used |= placeholders(rule.action.do.args)
    return used
