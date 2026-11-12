"""Checks that need the vocabulary or the whole rule set, beyond what one file's schema can see.

The installer refuses a rule that adds any error. Errors are things that would
make a rule unsafe or unable to run; warnings are worth a look but legal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Iterable, Literal

from ts import safety, tags as tag_book
from ts.schema import PLACEHOLDER, CheckCall, Condition, DiagnoseRule, FilterRule, FixRule, Outcome, _Call, placeholders
from ts.vocab import (COMMON_FIELDS, DEVICES, EVENT_TYPES, FIX_MATCH_KEYS, FORBIDDEN_DEVICES, PLATFORMS,
                      PROTECTED_INTERFACES, ROLES, SEVERITIES, SOURCES)


@dataclass(frozen=True, order=True)
class Issue:
    level: Literal["error", "warning"]
    rule_id: str
    message: str

    def __str__(self) -> str:
        return f"{self.level:<7} {self.rule_id}: {self.message}"


EVENT_FIELDS = frozenset().union(*EVENT_TYPES.values())
MATCH_KEYS = {
    "filter": (COMMON_FIELDS - {"time"}) | EVENT_FIELDS | {"tags"},
    "diagnose": (COMMON_FIELDS - {"time"}) | EVENT_FIELDS | {"tags"},
    "fix": FIX_MATCH_KEYS,
}
VOCABULARY = {"event": EVENT_TYPES, "role": ROLES, "platform": PLATFORMS, "source": SOURCES,
              "severity": SEVERITIES, "device": DEVICES}
CAUSE_PATTERN = re.compile(r"^(\*|[a-z][a-z0-9_*?]*(\.[a-z0-9_*?]+)+)$")
DEVICE_ARGS = ("device", "peer_device")
# Lines a rule may never send, whatever it is fixing.
FORBIDDEN_LINE = re.compile(
    r"^\s*(reload|write\s+erase|erase\b|delete\b|format\b|copy\b|boot\b|username\b|"
    r"enable\s+(password|secret)|aaa\b|(no\s+)?management\b|zerotouch\b|(no\s+)?ip\s+routing\b)", re.I)
INTERFACE_LINE = re.compile(r"^\s*(?:no\s+|default\s+)?interface\s+(.+?)\s*$", re.I)


def _values(value) -> list[str]:
    return value if isinstance(value, list) else [value]


def _expand(key: str, patterns: Iterable[str]) -> set[str]:
    return {v for v in VOCABULARY[key] if any(fnmatchcase(v, p) for p in patterns)}


def _globs_overlap(a: str, b: str) -> bool:
    return a == b or fnmatchcase(a, b) or fnmatchcase(b, a)


def reachable_devices(match: dict) -> set[str]:
    """Devices whose events a match can see. Never the forbidden ones, and hosts
    only when the match names them (a role, platform or device)."""
    devices = {d for d in DEVICES if d not in FORBIDDEN_DEVICES}
    names_host = False
    for key, index in (("role", 0), ("platform", 1)):
        if key in match:
            pats = _values(match[key])
            devices = {d for d in devices if any(fnmatchcase(DEVICES[d][index], p) for p in pats)}
            names_host |= any(p in ("host", "linux") for p in pats)
    if "device" in match:
        pats = _values(match["device"])
        devices = {d for d in devices if any(fnmatchcase(d, p) for p in pats)}
        names_host |= any(DEVICES.get(p, ("", ""))[0] == "host" for p in pats)
    if not names_host:
        devices = {d for d in devices if DEVICES[d][0] != "host"}
    return devices


def event_types(match: dict) -> set[str]:
    types = _expand("event", _values(match["event"])) if "event" in match else set(EVENT_TYPES)
    return types & tag_book.events_for(_values(match["tags"])) if "tags" in match else types


def base_vars(match: dict) -> set[str]:
    """Placeholders every event this match can see is guaranteed to fill."""
    types = event_types(match)
    common = set(COMMON_FIELDS)
    return common | set.intersection(*(set(EVENT_TYPES[t]) for t in types)) if types else common


def cause_overlaps(label: str, pattern: str) -> bool:
    """Can a produced cause label (which may hold {placeholders}) match a fix rule's cause pattern?"""
    as_glob = PLACEHOLDER.sub("*", label)
    example = PLACEHOLDER.sub(lambda m: m.group(1), label)
    return fnmatchcase(example, pattern) or fnmatchcase(pattern, as_glob) or fnmatchcase(label, pattern)


def matches_overlap(a: dict, b: dict) -> bool:
    """Could one event (or cause) satisfy both matches?"""
    if not reachable_devices(a) & reachable_devices(b):
        return False
    for key in set(a) & set(b):
        if key in ("device", "role", "platform"):
            continue  # already decided by the device sets
        pa, pb = _values(a[key]), _values(b[key])
        if key in VOCABULARY:
            if not _expand(key, pa) & _expand(key, pb):
                return False
        elif not any(_globs_overlap(x, y) for x in pa for y in pb):
            return False
    return True


@dataclass
class _Producer:
    rule_id: str
    label: str
    vars: set[str]


@dataclass
class _Goto:
    rule_id: str
    target: str
    vars: set[str]
    where: str


@dataclass
class _Linter:
    rules: list
    carried: frozenset = frozenset()  # tags the monitors put on the events they raise
    issues: list[Issue] = field(default_factory=list)
    producers: list[_Producer] = field(default_factory=list)
    gotos: list[_Goto] = field(default_factory=list)

    def error(self, rule, msg):
        self.issues.append(Issue("error", rule.id, msg))

    def warn(self, rule, msg):
        self.issues.append(Issue("warning", rule.id, msg))

    # --- pieces shared by every table --------------------------------------

    def lint_match(self, rule):
        allowed = MATCH_KEYS[rule.table]
        for key, value in rule.match.items():
            pats = _values(value)
            if key not in allowed:
                self.error(rule, f"match.{key}: {rule.table} rules cannot match on {key} "
                                 f"(allowed: {', '.join(sorted(allowed))})")
                continue
            if key in VOCABULARY:
                for p in pats:
                    if not _expand(key, [p]):
                        self.error(rule, f"match.{key}: {p!r} matches no known {key}")
            if key == "device" and any(p in FORBIDDEN_DEVICES for p in pats):
                self.error(rule, f"match.device: {', '.join(sorted(FORBIDDEN_DEVICES))} is the ISP router; rules never touch it")
            if key == "role" and "isp" in pats:
                self.error(rule, "match.role: isp is the ISP router; rules never touch it")
            if key == "tags":
                for p in pats:
                    if not tag_book.is_known(p):
                        self.error(rule, f"match.tags: {p!r} is not a tag")
                known = [p for p in pats if tag_book.is_known(p)]
                if not tag_book.events_for(known) and not tag_book.covers(known, self.carried):
                    self.warn(rule, "no event or monitor carries these tags, so the rule can never run")
            if key == "cause":
                for p in pats:
                    if not CAUSE_PATTERN.match(p):
                        self.error(rule, f"match.cause: {p!r} is not a cause label or pattern")
        for tag in rule.tags:
            if not tag_book.is_known(tag):
                self.error(rule, f"tags: {tag!r} is not a tag")
        if rule.table == "diagnose" and "event" not in rule.match and "tags" not in rule.match:
            self.error(rule, "a diagnose rule must match on event or tags")
        if rule.table == "fix" and "cause" not in rule.match:
            self.error(rule, "a fix rule must match on cause")
        if rule.match and not reachable_devices(rule.match):
            self.warn(rule, "the match selects no device the system watches, so the rule can never run")

    def lint_call(self, rule, call: _Call, where: str, known: set[str]):
        missing = placeholders(call.args) - known
        if missing:
            self.error(rule, f"{where}: {call.name} uses {{{'}, {'.join(sorted(missing))}}}, "
                             f"which nothing has set at this point")
        for name in DEVICE_ARGS:
            value = call.args.get(name)
            if value is None or placeholders(value):
                continue
            if value in FORBIDDEN_DEVICES:
                self.error(rule, f"{where}: {value} is the ISP router; rules never touch it")
            elif value not in DEVICES:
                self.error(rule, f"{where}: unknown device {value!r}")
        command = call.args.get("command") if call.name == "output_has" else None
        if command and not placeholders(command):
            problem = safety.read_problem(command)
            if problem:
                self.error(rule, f"{where}: {problem}")
        block = call.block
        device = call.args.get("device")
        if block.device_bound and device is not None:
            if device == "{device}":
                devices = reachable_devices(rule.match)
            elif not placeholders(device) and device in DEVICES:
                devices = {device}
            else:
                devices = set()  # only known at run time
            unsupported = sorted(d for d in devices if DEVICES[d][1] not in block.platforms)
            if unsupported:
                plats = sorted({DEVICES[d][1] for d in unsupported})
                self.warn(rule, f"{where}: {call.name} does not support {'/'.join(plats)} "
                                f"({', '.join(unsupported)}) and returns unsupported there")

    def lint_action_safety(self, rule, call: _Call, where: str):
        iface = call.args.get("interface")
        if iface and not placeholders(iface) and _protected(iface):
            self.error(rule, f"{where}: {iface} is a management interface; rules never change it")
        for line in call.args.get("lines", []):
            if FORBIDDEN_LINE.match(line):
                self.error(rule, f"{where}: {line!r} is not allowed in a rule")
            m = INTERFACE_LINE.match(line)
            if m and not placeholders(m.group(1)) and _protected(m.group(1)):
                self.error(rule, f"{where}: {line!r} changes a management interface; rules never do that")
            if rule.source.by == "ai" and safety.line_problem(line):
                self.error(rule, f"{where}: {safety.line_problem(line)}")
        for key in ("argv", "undo_argv"):
            argv = call.args.get(key)
            if argv and not placeholders(argv) and safety.argv_problem(argv):
                self.error(rule, f"{where}: {safety.argv_problem(argv)}")

    def lint_conditions(self, rule, conditions: list[Condition], what: str, known: set[str]):
        for n, cond in enumerate(conditions, 1):
            self.lint_call(rule, cond.check, f"{what} {n}", known)

    def lint_outcome(self, rule, outcome: Outcome, where: str, known: set[str]):
        used = placeholders(outcome.target) | placeholders(outcome.params)
        missing = used - known
        if missing:
            self.error(rule, f"{where}: '{outcome.render()}' uses {{{'}, {'.join(sorted(missing))}}}, "
                             f"which nothing has set at this point")
        device = outcome.params.get("device")
        if device and not placeholders(device):
            if device in FORBIDDEN_DEVICES:
                self.error(rule, f"{where}: {device} is the ISP router; rules never touch it")
            elif device not in DEVICES:
                self.error(rule, f"{where}: unknown device {device!r}")
        if outcome.kind == "cause" and rule.mode != "retired":
            self.producers.append(_Producer(rule.id, outcome.target, known | set(outcome.params) | {"cause"}))
        if outcome.kind == "goto":
            self.gotos.append(_Goto(rule.id, outcome.target, set(known), where))

    # --- tables ------------------------------------------------------------

    def lint_filter(self, rule: FilterRule):
        self.lint_conditions(rule, rule.when, "when", base_vars(rule.match))
        if rule.verdict == "drop" and "event" not in rule.match:
            self.error(rule, "a drop filter must name the event types it drops; dropping everything hides real faults")

    def lint_diagnose(self, rule: DiagnoseRule):
        base = base_vars(rule.match)
        steps = rule.steps
        index = {s.id: i for i, s in enumerate(steps) if s.id}
        incoming: list[list[set[str]]] = [[] for _ in range(len(steps) + 1)]  # last slot = end
        incoming[0].append(base)
        for i, step in enumerate(steps):
            where = f"step {i + 1}" + (f" ({step.id})" if step.id else "")
            if not incoming[i]:
                self.warn(rule, f"{where} can never be reached")
            known = set.intersection(*incoming[i]) if incoming[i] else set(base)
            self.lint_call(rule, step.check, where, known)
            after = known | set(step.check.block.produces)
            outcomes = step.check.block.all_outcomes
            if any(o not in step.then or step.then[o].kind == "next" for o in outcomes):
                incoming[i + 1].append(after)
            for name, outcome in step.then.items():
                self.lint_outcome(rule, outcome, f"{where} on {name}", after)
                if outcome.kind == "jump":
                    j = index.get(outcome.target)
                    if j is None:
                        self.error(rule, f"{where} on {name}: no step with id {outcome.target!r}")
                    elif j <= i:
                        self.error(rule, f"{where} on {name}: jumps back to {outcome.target}; "
                                         f"jumps only go forward so every diagnosis ends")
                    else:
                        incoming[j].append(after)
        end_known = set.intersection(*incoming[-1]) if incoming[-1] else set(base)
        self.lint_outcome(rule, rule.end, "end", end_known)
        produced = {o.target for s in steps for o in s.then.values() if o.kind == "cause"}
        if rule.end.kind == "cause":
            produced.add(rule.end.target)
        for label in sorted(set(rule.causes) - produced):
            self.warn(rule, f"causes: {label} is explained but no step produces it")
        for label in sorted(produced - set(rule.causes)):
            self.warn(rule, f"cause {label} has no plain-language sentence under causes")

    def lint_fix(self, rule: FixRule):
        patterns = _values(rule.match.get("cause", []))
        sites = [p for p in self.producers if any(cause_overlaps(p.label, pat) for pat in patterns)]
        if sites:
            known = set.intersection(*(s.vars for s in sites))
        else:
            known = set(COMMON_FIELDS) | {"cause"}
            if patterns:
                self.warn(rule, f"no diagnose rule produces cause {', '.join(patterns)}, so this fix can never run")
        known = set(known) | {"alarm_device"}  # the fixer always sets this
        self.lint_conditions(rule, rule.require, "require", known)
        self.lint_call(rule, rule.action.do, "action", known)
        self.lint_action_safety(rule, rule.action.do, "action")
        undo_policy = rule.action.do.block.undo
        if rule.action.undo is not None:
            self.lint_call(rule, rule.action.undo, "undo", known)
            self.lint_action_safety(rule, rule.action.undo, "undo")
            if undo_policy == "not_needed":
                self.warn(rule, f"{rule.action.do.name} changes no configuration; the undo is never used")
        elif undo_policy == "explicit":
            self.error(rule, f"{rule.action.do.name} needs an explicit undo in every rule that uses it")
        self.lint_conditions(rule, rule.verify, "verify", known)
        if rule.risk == "high" and rule.mode == "active":
            self.error(rule, "a high-risk fix cannot be active; a person runs it from proposed or shadow")
        if not any(c.check.name == "change_window_open" and c.expect == ["closed"] for c in rule.require):
            self.warn(rule, "require has no 'change_window_open == closed', so this fix could fight a Jenkins change")

    # --- whole set ---------------------------------------------------------

    def lint_overlaps(self):
        live = [r for r in self.rules if r.mode != "retired"]
        for i, a in enumerate(live):
            for b in live[i + 1:]:
                if a.table == b.table and a.priority == b.priority and a.position == b.position == "main" and matches_overlap(a.match, b.match):
                    for x, y in ((a, b), (b, a)):
                        self.error(x, f"overlaps {y.id} at the same priority {x.priority} in the {x.table} "
                                      f"table; change one priority or narrow a match")

    def lint_gotos(self):
        by_id = {r.id: r for r in self.rules}
        graph: dict[str, set[str]] = {}
        for g in self.gotos:
            rule = by_id[g.rule_id]
            target = by_id.get(g.target)
            if target is None:
                self.error(rule, f"{g.where}: goto {g.target}, but no such rule is installed")
                continue
            if target.table != "diagnose":
                self.error(rule, f"{g.where}: goto {g.target}, which is a {target.table} rule, not a diagnose rule")
                continue
            if target.mode == "retired":
                self.error(rule, f"{g.where}: goto {g.target}, which is retired")
            elif target.mode == "proposed" and rule.mode in ("shadow", "active"):
                self.warn(rule, f"{g.where}: goto {g.target}, which is only proposed; the diagnosis stops there until it is promoted")
            needs = _rule_placeholders(target) & base_vars(target.match)
            missing = needs - g.vars
            if missing:
                self.error(rule, f"{g.where}: goto {g.target} needs {{{'}, {'.join(sorted(missing))}}}, "
                                 f"which are not set here")
            graph.setdefault(rule.id, set()).add(target.id)
        for cycle in _cycles(graph):
            for rule_id in sorted(set(cycle)):
                self.error(by_id[rule_id], f"goto loop: {' -> '.join(cycle)}")

    def run(self) -> list[Issue]:
        ids = [r.id for r in self.rules]
        for dup in sorted({i for i in ids if ids.count(i) > 1}):
            self.issues.append(Issue("error", dup, "rule id used more than once"))
        piped: dict[str, str] = {}  # each diagnose and fix rule runs as its own Jenkins pipeline
        for rule in sorted(self.rules, key=lambda r: r.id):
            if isinstance(rule, (DiagnoseRule, FixRule)):
                name = getattr(rule, "pipeline", None) or rule.id
                if piped.get(name, rule.id) != rule.id:
                    self.error(rule, f"its Jenkins pipeline {name} already belongs to the rule {piped[name]}")
                piped.setdefault(name, rule.id)
        for rule in self.rules:
            self.lint_match(rule)
        for rule in self.rules:
            if isinstance(rule, FilterRule):
                self.lint_filter(rule)
            elif isinstance(rule, DiagnoseRule):
                self.lint_diagnose(rule)
        for rule in self.rules:  # after every diagnose rule has listed its causes
            if isinstance(rule, FixRule):
                self.lint_fix(rule)
        self.lint_overlaps()
        self.lint_gotos()
        return sorted(set(self.issues))


def _protected(name: str) -> bool:
    name = name.replace(" ", "").lower()
    return name in PROTECTED_INTERFACES or name.startswith("management")


def _rule_placeholders(rule: DiagnoseRule) -> set[str]:
    used = set()
    for step in rule.steps:
        used |= placeholders(step.check.args)
        for outcome in step.then.values():
            used |= placeholders(outcome.target) | placeholders(outcome.params)
    return used | placeholders(rule.end.target) | placeholders(rule.end.params)


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    cycles, state, stack = [], {}, []

    def visit(node):
        state[node] = "open"
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if state.get(nxt) == "open":
                cycles.append(stack[stack.index(nxt):] + [nxt])
            elif nxt not in state:
                visit(nxt)
        stack.pop()
        state[node] = "done"

    for node in sorted(graph):
        if node not in state:
            visit(node)
    return cycles


def lint(rules: list, carried: Iterable[str] = ()) -> list[Issue]:
    """carried: the tags the monitors put on their events (monitors.carried_tags), so a rule only a monitor runs is not called dead."""
    return _Linter(list(rules), frozenset(carried)).run()


def errors(issues: Iterable[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == "error"]
