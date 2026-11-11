"""Building-block registry: the only checks and actions a rule may use.

Rules are data. They can only combine blocks registered here, so a rule
(written by a person or, later, by the agent) can never run arbitrary code.
Adding a block is a code change that goes through review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

# Every check can also end in these, whatever it declares.
ALWAYS_OUTCOMES = ("error", "unsupported")
# Platform marker for checks that run on the host, not on the device itself.
ANY_PLATFORM = "any"


@dataclass(frozen=True)
class Param:
    name: str
    many: bool = False  # takes a list
    optional: bool = False

    @classmethod
    def parse(cls, spec: str) -> "Param":
        optional = spec.endswith("?")
        spec = spec.rstrip("?")
        many = spec.endswith("[]")
        return cls(spec[:-2] if many else spec, many, optional)


@dataclass(frozen=True)
class Block:
    kind: Literal["check", "action"]
    name: str
    params: tuple[Param, ...]
    description: str
    platforms: frozenset[str]
    outcomes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    undo: Literal["auto", "explicit", "not_needed"] | None = None
    func: Callable | None = field(default=None, compare=False, repr=False)

    @property
    def param_names(self) -> list[str]:
        return [p.name for p in self.params]

    @property
    def required(self) -> list[str]:
        return [p.name for p in self.params if not p.optional]

    def param(self, name: str) -> Param | None:
        return next((p for p in self.params if p.name == name), None)

    @property
    def all_outcomes(self) -> tuple[str, ...]:
        return self.outcomes + ALWAYS_OUTCOMES

    @property
    def device_bound(self) -> bool:
        return ANY_PLATFORM not in self.platforms


CHECKS: dict[str, Block] = {}
ACTIONS: dict[str, Block] = {}


def _describe(func: Callable, description: str) -> str:
    return description or (func.__doc__ or "").strip().split("\n")[0]


def check(name: str, params=(), outcomes=(), produces=(), platforms=("eos", "frr"), description: str = ""):
    """Register a read-only check."""
    if not outcomes:
        raise ValueError(f"check {name} must declare its outcomes")

    def register(func: Callable) -> Callable:
        if name in CHECKS:
            raise ValueError(f"check {name} registered twice")
        CHECKS[name] = Block("check", name, tuple(Param.parse(p) for p in params), _describe(func, description),
                             frozenset(platforms), tuple(outcomes), tuple(produces), None, func)
        return func

    return register


def action(name: str, params=(), undo: Literal["auto", "explicit", "not_needed"] = "auto",
           platforms=("eos", "frr"), description: str = ""):
    """Register an action. ``undo`` says how it is reversed:
    auto = the engine snapshots what it changes and restores it,
    explicit = every rule using it must give its own undo,
    not_needed = it changes no configuration."""

    def register(func: Callable) -> Callable:
        if name in ACTIONS:
            raise ValueError(f"action {name} registered twice")
        ACTIONS[name] = Block("action", name, tuple(Param.parse(p) for p in params), _describe(func, description),
                              frozenset(platforms), (), (), undo, func)
        return func

    return register


def load_blocks() -> None:
    """Import the block modules so their decorators register everything."""
    from ts.blocks import actions, checks, generic, routing, services  # noqa: F401
