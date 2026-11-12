"""The rule format: one schema for the filter, diagnose and fix tables.

A rule file holds one rule. Calls to checks and actions can be written
compactly (``interface_status {device} {interface}``) or as a mapping
(``{name: config_lines, device: "{device}", lines: [...]}``). Placeholders
in braces are filled from the event, from values earlier checks produced,
or, in fix rules, from the cause.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

from ts.registry import ACTIONS, CHECKS, Block, load_blocks

load_blocks()

PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
RULE_ID = r"^[a-z0-9][a-z0-9-]{2,63}$"
PIPELINE = r"^[a-z0-9]+(-[a-z0-9]+)*$"  # a Jenkins pipeline's name in troubleshooting-pipelines
STEP_ID = r"^[a-z0-9][a-z0-9_-]{0,40}$"
CAUSE_LABEL = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_{}]+)+$")
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
TABLES = ("filter", "diagnose", "fix")
MODES = ("proposed", "shadow", "active", "retired")


def placeholders(value: Any) -> set[str]:
    """Every {name} used anywhere inside a value."""
    if isinstance(value, str):
        return set(PLACEHOLDER.findall(value))
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        return set().union(*(placeholders(v) for v in value)) if value else set()
    return set()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- calls to building blocks ---------------------------------------------

def _parse_call(value: Any, registry: dict[str, Block], kind: str) -> dict:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, str):
        try:
            tokens = shlex.split(value)
        except ValueError as exc:
            raise ValueError(f"cannot parse {kind} call {value!r}: {exc}") from None
        if not tokens:
            raise ValueError(f"empty {kind} call")
        name, rest = tokens[0], tokens[1:]
        block = registry.get(name)
        if block is None:
            raise ValueError(f"unknown {kind} {name!r}")
        args: dict[str, Any] = {}
        positional = []
        for token in rest:
            key, sep, val = token.partition("=")
            if sep and block.param(key):
                if key in args:
                    raise ValueError(f"{name}: argument {key} given twice")
                args[key] = val
            else:
                positional.append(token)
        free = [p for p in block.params if p.name not in args]
        if len(positional) > len(free):
            raise ValueError(f"{name} takes ({', '.join(block.param_names) or 'no arguments'}); "
                             f"too many values: {positional[len(free):]}")
        for param, token in zip(free, positional):
            if param.many:
                raise ValueError(f"{name}: {param.name} takes a list; write the call as a mapping")
            args[param.name] = token
        return {"name": name, "args": args}
    if isinstance(value, dict):
        if "name" not in value:
            raise ValueError(f"a {kind} call written as a mapping needs 'name'")
        if value["name"] not in registry:
            raise ValueError(f"unknown {kind} {value['name']!r}")
        if "args" in value:
            extra = set(value) - {"name", "args"}
            if extra:
                raise ValueError(f"unexpected keys next to 'args': {sorted(extra)}")
            return {"name": value["name"], "args": dict(value["args"] or {})}
        return {"name": value["name"], "args": {k: v for k, v in value.items() if k != "name"}}
    raise ValueError(f"a {kind} call must be a string or a mapping")


class _Call(Strict):
    kind: ClassVar[str] = ""
    name: str
    args: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def registry(cls) -> dict[str, Block]:
        return CHECKS if cls.kind == "check" else ACTIONS

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, value: Any) -> Any:
        return _parse_call(value, cls.registry(), cls.kind)

    @model_validator(mode="after")
    def _check_args(self):
        block = self.block
        clean: dict[str, Any] = {}
        for key, val in self.args.items():
            param = block.param(key)
            if param is None:
                raise ValueError(f"{self.name} has no argument {key!r} (takes {', '.join(block.param_names) or 'none'})")
            if param.many:
                if not isinstance(val, list) or not val or not all(isinstance(x, (str, int, float)) for x in val):
                    raise ValueError(f"{self.name}: {key} must be a non-empty list of strings")
                clean[key] = [str(x) for x in val]
            else:
                if not isinstance(val, (str, int, float)) or isinstance(val, bool):
                    raise ValueError(f"{self.name}: {key} must be a single value")
                clean[key] = str(val)
        missing = [n for n in block.required if n not in clean]
        if missing:
            raise ValueError(f"{self.name} is missing argument(s): {', '.join(missing)}")
        self.args = clean
        return self

    @property
    def block(self) -> Block:
        return self.registry()[self.name]

    def render(self) -> str:
        parts = [self.name]
        for param in self.block.params:
            if param.name not in self.args:
                continue
            val = self.args[param.name]
            if param.many:
                parts.append(f"{param.name}=[{', '.join(val)}]")
            else:
                parts.append(shlex.quote(val) if (" " in val or not val) else val)
        return " ".join(parts)


class CheckCall(_Call):
    kind: ClassVar[str] = "check"


class ActionCall(_Call):
    kind: ClassVar[str] = "action"


class Condition(Strict):
    """``<check call> == outcome`` or ``<check call> in [a, b]``."""

    check: CheckCall
    expect: list[str] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if " == " in value:
            call, _, expected = value.rpartition(" == ")
            return {"check": call.strip(), "expect": [expected.strip()]}
        if " in " in value:
            call, _, expected = value.rpartition(" in ")
            expected = expected.strip()
            if not (expected.startswith("[") and expected.endswith("]")):
                raise ValueError(f"expected a list after 'in': {value!r}")
            return {"check": call.strip(), "expect": [x.strip() for x in expected[1:-1].split(",") if x.strip()]}
        raise ValueError(f"a condition looks like '<check ...> == outcome' or '<check ...> in [a, b]': {value!r}")

    @model_validator(mode="after")
    def _check_outcomes(self):
        unknown = [e for e in self.expect if e not in self.check.block.all_outcomes]
        if unknown:
            raise ValueError(f"{self.check.name} never returns {unknown}; it returns {list(self.check.block.outcomes)}")
        return self

    def render(self) -> str:
        if len(self.expect) == 1:
            return f"{self.check.render()} == {self.expect[0]}"
        return f"{self.check.render()} in [{', '.join(self.expect)}]"


# --- outcomes of a diagnose step ------------------------------------------

OutcomeKind = Literal["next", "cause", "goto", "jump", "stop", "escalate"]


class Outcome(Strict):
    """next | cause <label> [key=value ...] | goto <rule-id> | jump <step-id> |
    stop <message> | escalate <message>"""

    kind: OutcomeKind
    target: str = ""
    params: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        word, _, rest = value.strip().partition(" ")
        rest = rest.strip()
        if word == "next":
            if rest:
                raise ValueError("'next' takes nothing after it")
            return {"kind": "next"}
        if word == "cause":
            try:
                tokens = shlex.split(rest)
            except ValueError as exc:
                raise ValueError(f"cannot parse {value!r}: {exc}") from None
            if not tokens:
                raise ValueError("'cause' needs a label, e.g. 'cause interface.admin_shut'")
            params = {}
            for token in tokens[1:]:
                key, sep, val = token.partition("=")
                if not sep:
                    raise ValueError(f"cause parameters are key=value, got {token!r}")
                params[key] = val
            return {"kind": "cause", "target": tokens[0], "params": params}
        if word in ("goto", "jump"):
            if not rest or " " in rest:
                raise ValueError(f"'{word}' takes exactly one id")
            return {"kind": word, "target": rest}
        if word in ("stop", "escalate"):
            if not rest:
                raise ValueError(f"'{word}' needs a message saying why")
            return {"kind": word, "target": rest}
        raise ValueError(f"unknown outcome {word!r}; use next, cause, goto, jump, stop or escalate")

    @model_validator(mode="after")
    def _check_target(self):
        if self.kind == "cause":
            if not CAUSE_LABEL.match(self.target):
                raise ValueError(f"cause label {self.target!r} must look like area.what, e.g. ospf.mtu_mismatch")
            bad = [k for k in self.params if not IDENTIFIER.match(k)]
            if bad:
                raise ValueError(f"bad cause parameter name(s): {bad}")
        elif self.kind == "goto" and not re.match(RULE_ID, self.target):
            raise ValueError(f"goto needs a rule id, got {self.target!r}")
        elif self.kind == "jump" and not re.match(STEP_ID, self.target):
            raise ValueError(f"jump needs a step id, got {self.target!r}")
        return self

    def render(self) -> str:
        if self.kind == "next":
            return "next"
        extra = "".join(f" {k}={v}" for k, v in self.params.items())
        return f"{self.kind} {self.target}{extra}"


class Step(Strict):
    id: str | None = Field(default=None, pattern=STEP_ID)
    # The question this step answers, in plain words; shown in the UI.
    ask: str | None = Field(default=None, min_length=5)
    # confirm = does the monitoring agree there is a problem; investigate = why.
    phase: Literal["confirm", "investigate"] = "investigate"
    check: CheckCall
    # Outcome name -> what happens. Outcomes not listed continue with the next step.
    then: dict[str, Outcome] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_outcome_names(self):
        unknown = [k for k in self.then if k not in self.check.block.all_outcomes]
        if unknown:
            raise ValueError(f"{self.check.name} never returns {unknown}; it returns "
                             f"{list(self.check.block.all_outcomes)}")
        return self


# --- rules ----------------------------------------------------------------

class Source(Strict):
    by: str = Field(min_length=1)
    incident: str | int | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    note: str | None = None


class Retire(Strict):
    max_failures: int = Field(3, ge=1)
    unused_days: int = Field(90, ge=1)


MatchValue = Union[str, list[str]]


class RuleBase(Strict):
    id: str = Field(pattern=RULE_ID)
    version: int = Field(1, ge=1)
    priority: int = Field(ge=0, le=65535)
    description: str = Field(min_length=10)
    match: dict[str, MatchValue] = Field(default_factory=dict)
    mode: Literal["proposed", "shadow", "active", "retired"] = "proposed"
    # Where it runs for an event: before the main rule, as the main rule, or once the main rule has run out.
    position: Literal["beginning", "main", "end"] = "main"
    source: Source
    tags: list[str] = Field(default_factory=list)
    # The Jenkins pipeline that runs it, in the troubleshooting-pipelines folder. The rule's id when left out.
    pipeline: str | None = Field(default=None, pattern=PIPELINE, min_length=3, max_length=60)

    @field_validator("match")
    @classmethod
    def _check_match(cls, match: dict[str, MatchValue]) -> dict[str, MatchValue]:
        for key, value in match.items():
            values = value if isinstance(value, list) else [value]
            if not values or not all(isinstance(v, str) and v.strip() for v in values):
                raise ValueError(f"match.{key} must be a non-empty string or list of strings")
        return match


class FilterRule(RuleBase):
    table: Literal["filter"]
    when: list[Condition] = Field(default_factory=list)
    verdict: Literal["drop", "expected", "pass"]


class DiagnoseRule(RuleBase):
    table: Literal["diagnose"]
    # What a person would call this scenario, and what starts it. Shown in the UI.
    title: str | None = None
    trigger: str | None = None
    # A run that ends in "stop" is written to the watcher's log but not kept as a run.
    # For scenarios where most events are harmless and arrive by the hundred.
    quiet_stops: bool = False
    steps: list[Step] = Field(min_length=1)
    # Cause label -> one plain sentence explaining it (placeholders allowed).
    causes: dict[str, str] = Field(default_factory=dict)
    end: Outcome = Field(default_factory=lambda: Outcome(kind="stop", target="no conclusive cause found"))

    @model_validator(mode="after")
    def _check_steps(self):
        if self.end.kind in ("next", "jump"):
            raise ValueError("'end' cannot be next or jump; there is nothing after it")
        phases = [s.phase for s in self.steps]
        if "confirm" in phases[phases.index("investigate"):] if "investigate" in phases else False:
            raise ValueError("confirm steps must all come before investigate steps")
        ids = [s.id for s in self.steps if s.id]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"step ids must be unique: {dupes}")
        return self


class FixAction(Strict):
    do: ActionCall
    undo: ActionCall | None = None


class FixRule(RuleBase):
    table: Literal["fix"]
    title: str = ""
    require: list[Condition] = Field(default_factory=list)
    action: FixAction
    verify: list[Condition] = Field(min_length=1)
    risk: Literal["low", "medium", "high"]
    retire: Retire = Field(default_factory=Retire)


Rule = Annotated[Union[FilterRule, DiagnoseRule, FixRule], Field(discriminator="table")]
RULE_ADAPTER: TypeAdapter = TypeAdapter(Rule)


class RuleLoadError(Exception):
    pass


def _format(exc: ValidationError, origin: str) -> str:
    lines = [f"{origin}: not a valid rule"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"] if p not in TABLES or len(err["loc"]) == 1)
        msg = err["msg"].removeprefix("Value error, ")
        lines.append(f"  {loc or '(rule)'}: {msg}")
    return "\n".join(lines)


def parse_rule(data: Any, origin: str = "<rule>"):
    if not isinstance(data, dict):
        raise RuleLoadError(f"{origin}: a rule file holds exactly one mapping")
    try:
        return RULE_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise RuleLoadError(_format(exc, origin)) from None


def read_rule_file(path: Path) -> tuple[dict, Any]:
    """(raw mapping, parsed rule). The raw mapping keeps the compact call forms for rewriting."""
    try:
        data = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError as exc:
        raise RuleLoadError(f"{path}: not valid YAML: {exc}") from None
    return data, parse_rule(data, str(path))
