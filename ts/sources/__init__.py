"""What else was going on: places to look for context around an event.

Each source answers one question: what happened between these two moments,
for this device if it knows about devices. A source that fails or is not set
up is reported and skipped, so an investigation never depends on one.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

from ts import scrub

BEFORE, AFTER = 1800, 300  # seconds of context around an event
SOURCES: dict = {}


class NotConfigured(Exception):
    """This source needs something that has not been provided."""


@dataclass
class Doc:
    source: str
    time: float
    title: str
    text: str = ""
    url: str = ""
    device: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def source(name: str):
    def register(func):
        SOURCES[name] = func
        return func
    return register


def load() -> dict:
    from ts.sources import datalake, github, grafana, jenkins, prometheus  # noqa: F401
    return SOURCES


def query(name: str, start: float, end: float, device: str | None = None) -> list[Doc]:
    load()
    docs = SOURCES[name](start, end, device)
    for doc in docs:
        doc.title, doc.text = scrub.text(doc.title), scrub.text(doc.text)
    return sorted(docs, key=lambda d: d.time)


def gather(event: dict, before: int = BEFORE, after: int = AFTER) -> tuple[list[dict], dict]:
    """(everything found around this event, what happened with each source)."""
    load()
    start, end, device = event["time"] - before, event["time"] + after, event.get("device")
    docs, status = [], {}

    def one(name):
        try:
            return name, query(name, start, end, device), "ok"
        except NotConfigured as exc:
            return name, [], f"not set up: {exc}"
        except Exception as exc:  # a broken source must not stop the investigation
            return name, [], f"failed: {type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        for name, found, state in pool.map(one, SOURCES):
            status[name] = {"state": state, "found": len(found)}
            docs += [d.as_dict() for d in found]
    return sorted(docs, key=lambda d: d["time"]), status
