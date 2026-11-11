"""The working day at each computer.

Everyone gets on with their own work from their own machine. The requests really cross
the network and what happens to them is what the person remembers, which is how they can
answer "did this work before?" the way anyone would. No model is involved, so a day of
it costs nothing.
"""

from __future__ import annotations

import random
import re
import subprocess
import threading
import time
from pathlib import Path

from drills.run import _why_not_now, sh
from ts import brain, tickets
from ts.sweep import INTERNET_V4, INTERNET_V6

BRAINS = Path(__file__).resolve().parent.parent / "data" / "drills" / "people-brains.sqlite"  # each person's memory, apart from anything the support side can read
EVERY = 480         # seconds between one person's goes, on average
PAUSE = 60          # while the network is being worked on, how soon to ask again
TRIES = 3           # requests in one go
PAGE_TIMEOUT = "6"  # seconds
SLOW = 2.0          # seconds, past which a page feels slow
OUTSIDE, INSIDE = "outside", "inside"
FINE, IS_SLOW, DROPPING, NOT_WORKING = "fine", "slow", "dropping out", "not working"

USES = {
    "priya": (("the payroll system", OUTSIDE), ("the bank's website", OUTSIDE), ("the finance share", INSIDE)),
    "marcus": (("the customer records site", OUTSIDE), ("video calls", OUTSIDE), ("the sales share", INSIDE)),
    "aiko": (("the code host", OUTSIDE), ("package downloads", OUTSIDE), ("the build server", INSIDE)),
    "diego": (("the support chat", OUTSIDE), ("the knowledge base", OUTSIDE), ("the support share", INSIDE)),
    "lena": (("the design library", OUTSIDE), ("the stock photo site", OUTSIDE), ("the design share", INSIDE)),
    "sam": (("the carrier booking site", OUTSIDE), ("the shipping label portal", OUTSIDE), ("the warehouse system", INSIDE)),
    "ravi": (("the shipping system", OUTSIDE), ("the carrier booking site", OUTSIDE), ("the warehouse system", INSIDE)),
}
# The company machine each person's inside work is on.
_SERVER = {"h1": "10.10.10.169", "h2": "10.10.10.176", "h3": "10.10.10.176", "h4": "2001:db8:10::176", "h5": "10.10.10.176", "h6": "10.10.10.227", "h7": "10.10.10.226"}
_next: dict[str, float] = {}
_busy: set[str] = set()
_paused_until = 0.0


def uses_of(person_id: str) -> list[str]:
    return [name for name, _where in USES.get(person_id, ())]


def server_for(host: str) -> str | None:
    return _SERVER.get(host)


def _outside_page(host: str) -> str:
    return f"http://[{INTERNET_V6}]/" if host == "h4" else f"http://{INTERNET_V4}/"


def _page(host: str, url: str) -> float | None:
    """How long one page took, or None if it never came."""
    try:
        out = sh(host, "curl", "-s", "-m", PAGE_TIMEOUT, "-o", "/dev/null", "-w", "%{http_code} %{time_total}", url, check=False)
    except subprocess.TimeoutExpired:
        return None
    found = re.match(r"(\d{3}) ([\d.]+)", out.strip())
    return float(found.group(2)) if found and found.group(1) != "000" else None


def _reach(host: str, address: str) -> float | None:
    try:
        out = sh(host, "ping", *(["-6"] if ":" in address else []), "-c", "1", "-W", PAGE_TIMEOUT, address, check=False)
    except subprocess.TimeoutExpired:
        return None
    found = re.search(r"time=([\d.]+) ms", out)
    return float(found.group(1)) / 1000 if found else None


def _one_try(host: str, where: str) -> float | None:
    if where == OUTSIDE:
        return _page(host, _outside_page(host))
    server = _SERVER[host]
    return _reach(host, server) if ":" in server else _page(host, f"http://{server}/")


def how_it_went(times: list[float | None]) -> str:
    worked = [t for t in times if t is not None]
    if not worked:
        return NOT_WORKING
    if len(worked) < len(times):
        return DROPPING
    return IS_SLOW if max(worked) > SLOW else FINE


def one_go(person_id: str, host: str, moment: float | None = None) -> str:
    """Do one of their usual things and remember how it went."""
    name, where = random.choice(USES[person_id])
    how = how_it_went([_one_try(host, where) for _ in range(TRIES)])
    ended = brain.used(person_id, name, how, moment, path=BRAINS)
    if ended:
        brain.remember(person_id, "use", f"{name.capitalize()} was {ended['how']} from {brain.when(ended['first'])} to {brain.when(ended['last'])}, then it was {how}.", moment, path=BRAINS)
    return how


def _go(person_id: str, host: str) -> None:
    try:
        one_go(person_id, host)
    except Exception as exc:  # one person's morning must not stop anyone else's, or the desk
        print(f"office, {person_id}: {type(exc).__name__}: {exc}", flush=True)
    finally:
        _busy.discard(person_id)


def tick() -> None:
    """Whoever is due gets on with something, unless the network is being worked on."""
    global _paused_until
    now = time.time()
    if now < _paused_until:
        return
    due = [person_id for person_id in USES if _next.setdefault(person_id, now + random.uniform(10, EVERY)) <= now and person_id not in _busy]
    if not due:
        return
    if _why_not_now():
        _paused_until = now + PAUSE
        return
    for person_id in due:
        person = tickets.person(person_id)
        _busy.add(person_id)
        _next[person_id] = now + random.uniform(0.75 * EVERY, 1.25 * EVERY)
        threading.Thread(target=_go, args=(person_id, person["host"]), daemon=True).start()


def _story(what: str, stretches: list[dict]) -> str:
    parts = [f"{s['how']} from {brain.when(s['first'])} to {brain.when(s['last'])}" if s["times"] > 1 else f"{s['how']} at {brain.when(s['first'])}" for s in stretches]
    return f"- {what.capitalize()}: {', then '.join(parts)}."


def remembered(person_id: str, about: str) -> str:
    """What is in this person's head as they sit down: how their usual work has gone, and whatever this brings to mind."""
    day = [_story(what, stretches[-3:]) for what, stretches in brain.stretches(person_id, BRAINS).items()]
    came = brain.recall(person_id, about, 4, path=BRAINS)
    return "\n\n".join(filter(None, ["How your usual work has gone lately, as far as you remember:\n" + "\n".join(day) if day else "You have not done much on your computer lately, so you do not remember how things have been going.",
                                     "This comes to mind from before:\n" + "\n".join(brain.lines(came)) if came else ""]))
