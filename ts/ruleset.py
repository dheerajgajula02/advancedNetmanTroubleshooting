"""Load the installed rules: one YAML file per rule under rules/<table>/<id>.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ts.schema import RuleLoadError, read_rule_file

DEFAULT_RULES_DIR = Path(os.environ.get("TS_RULES", Path(__file__).resolve().parent.parent / "rules"))


@dataclass
class LoadedRule:
    path: Path
    raw: dict
    rule: Any  # FilterRule | DiagnoseRule | FixRule

    @property
    def id(self) -> str:
        return self.rule.id


@dataclass
class RuleSet:
    root: Path
    rules: list[LoadedRule]
    errors: list[str]  # files that could not be loaded, or sit in the wrong place

    def get(self, rule_id: str) -> LoadedRule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    @property
    def parsed(self) -> list:
        return [r.rule for r in self.rules]


def load_ruleset(root: Path | str = DEFAULT_RULES_DIR) -> RuleSet:
    root = Path(root)
    rules: list[LoadedRule] = []
    errors: list[str] = []
    seen: dict[str, Path] = {}
    for path in sorted(root.glob("**/*.y*ml")):
        if path.suffix not in (".yaml", ".yml"):
            continue
        try:
            raw, rule = read_rule_file(path)
        except RuleLoadError as exc:
            errors.append(str(exc))
            continue
        rel = path.relative_to(root)
        if path.stem != rule.id:
            errors.append(f"{rel}: file name must be the rule id ({rule.id}.yaml)")
        if path.parent.name != rule.table:
            errors.append(f"{rel}: a {rule.table} rule belongs in {rule.table}/")
        if rule.id in seen:
            errors.append(f"{rel}: rule id {rule.id} is already used by {seen[rule.id].relative_to(root)}")
            continue
        seen[rule.id] = path
        rules.append(LoadedRule(path, raw, rule))
    return RuleSet(root, rules, errors)
