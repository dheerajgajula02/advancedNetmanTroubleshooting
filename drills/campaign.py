"""A collection campaign: work through staged situations one at a time so the AI gets to practise on each.

Only this side knows the faults are staged. What ends up in the training data is the ordinary ticket and the
ordinary run, the same as anything else. The desk's upkeep loop calls tick() every few seconds.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

from drills import scenarios
from drills.run import _why_not_now
from ts import agent, history, tickets

STATE = Path(__file__).resolve().parent.parent / "data" / "drills" / "campaigns.json"
TARGET_SOLVED = 0.7
MAX_ROUNDS = 20
SAME_SCENARIO_GAP = agent.SAME_AGAIN_WITHIN + 60  # the AI will not take the same problem on the same device again sooner than this
SETTLED = ("solved", "no_fault", "needs_agent", "closed", "workaround")


def _load(path: Path | None = None) -> list[dict]:
    try:
        return json.loads((path or STATE).read_text())
    except (OSError, ValueError):
        return []


def _save(items: list[dict], path: Path | None = None) -> None:
    target = path or STATE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(items, indent=1))
    temporary.replace(target)


def listing(path: Path | None = None) -> list[dict]:
    return sorted(_load(path), key=lambda c: -c["created"])


def start(name: str, scenario_ids: list[str], rounds: int, path: Path | None = None) -> dict:
    """Plan a campaign. The rounds are spread out, so each situation comes round again only after the others."""
    name = (name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name):
        raise ValueError("Name the campaign with lowercase letters, numbers and dashes.")
    known = {s["id"] for s in scenarios.SCENARIOS}
    chosen = [s for s in dict.fromkeys(scenario_ids or []) if s in known]
    if not chosen:
        raise ValueError("Choose at least one situation.")
    if not 1 <= int(rounds) <= MAX_ROUNDS:
        raise ValueError(f"Rounds must be between 1 and {MAX_ROUNDS}.")
    items = _load(path)
    if any(c["state"] == "running" for c in items):
        raise ValueError("A campaign is already running. Stop it first.")
    if any(c["name"] == name for c in items):
        raise ValueError("A campaign already has that name.")
    plan = [{"scenario": s, "round": r + 1, "status": "waiting", "ticket": None, "run_id": "", "result": "", "at": 0.0} for r in range(int(rounds)) for s in chosen]
    made = {"id": f"cmp-{secrets.token_hex(3)}", "name": name, "created": time.time(), "state": "running", "rounds": int(rounds), "scenarios": chosen, "target": TARGET_SOLVED, "plan": plan}
    _save([*items, made], path)
    return made


def stop(campaign_id: str, path: Path | None = None) -> None:
    items = _load(path)
    for c in items:
        if c["id"] == campaign_id and c["state"] == "running":
            c["state"] = "stopped"
            for item in c["plan"]:
                if item["status"] == "waiting":
                    item["status"] = "skipped"
    _save(items, path)


def _ready(campaign: dict) -> dict | None:
    """The next situation that may go now."""
    if scenarios.current() or _why_not_now() or history.ai_sessions_lately() >= agent.MAX_SESSIONS_PER_HOUR:
        return None
    last = {}
    for item in campaign["plan"]:
        last[item["scenario"]] = max(last.get(item["scenario"], 0.0), item["at"])
    return next((i for i in campaign["plan"] if i["status"] == "waiting" and time.time() - last[i["scenario"]] >= SAME_SCENARIO_GAP), None)


def tick(path: Path | None = None) -> None:
    items = _load(path)
    campaign = next((c for c in items if c["state"] == "running"), None)
    if not campaign:
        return
    busy = next((i for i in campaign["plan"] if i["status"] == "staged"), None)
    if busy:
        ticket = tickets.get(busy["ticket"]) if busy["ticket"] else None
        marker = scenarios.current()
        if (ticket and ticket["status"] in SETTLED and not marker) or (not marker and time.time() - busy["at"] > scenarios.LONGEST):
            busy.update(status="done", result=(ticket or {}).get("status", "timed out"), run_id=(ticket or {}).get("run_id") or "")
            _save(items, path)
        return
    if not any(i["status"] == "waiting" for i in campaign["plan"]):
        campaign["state"] = "done"
        _save(items, path)
        return
    item = _ready(campaign)
    if not item:
        return
    chosen = next(s for s in scenarios.SCENARIOS if s["id"] == item["scenario"])
    try:
        scenarios.stage(item["scenario"])
    except ValueError:
        return  # something else got in first; try again on the next tick
    number = tickets.create(chosen["person"], chosen["ticket"])
    scenarios.attach(number)
    item.update(status="staged", ticket=number, at=time.time())
    _save(items, path)
