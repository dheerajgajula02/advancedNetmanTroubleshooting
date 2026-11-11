"""Long memory: everything the sources have said, kept searchable, and found again when a run needs it.

Each source is read on a timer, not just around incidents, so a run can find something
from days ago that no investigation looked at. Finding it is keyword search with no model involved.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from ts import cards, history, sources
from ts.rag import ADDRESS as _ADDRESS, PORT as _PORT
from ts.vocab import DEVICES, FORBIDDEN_DEVICES

INGEST_EVERY = 600  # seconds between reads of the sources
FIRST_LOOK_BACK = 14 * 86400
# How much of each source to ask for at once. Some cap what one answer holds; Jenkins re-reads its history on every ask.
CHUNK = {"datalake": 1800, "prometheus": 3600, "github": 86400, "grafana": 86400}
CANDIDATES = 120
RECALL = 10
PER_SOURCE = 4
HALF_LIFE_DAYS = 21
KEEP_TEXT = 1500

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*")
_KNOWN = {device.lower() for device in DEVICES} - {device.lower() for device in FORBIDDEN_DEVICES}
_FORBIDDEN = {device.lower() for device in FORBIDDEN_DEVICES}
_ROLLING = ("Also around then:", "Over the last 7 days")  # true of one moment's question, not of any moment in time
_TEST_MESSAGE = re.compile(r"ALERT_TEST_|SYSLOG-PROBE|CLAUDE-PROBE", re.I)  # sent on purpose to prove logging works
_ANY_PORT = re.compile(r"\b(?:eth\d+|ethernet\d+|et\d+|vlan\d+|port-channel\d+|loopback\d+)\b")
OTHER_PORT = 0.4  # same device, but plainly about a different port


def _worth_keeping(doc: dict) -> bool:
    title = doc["title"]
    return (doc.get("device") or "").lower() not in _FORBIDDEN and not title.endswith(": RUNNING") and not title.startswith(_ROLLING) and not _TEST_MESSAGE.search(title)


def ingest(start: float, end: float, log=lambda _message: None) -> dict:
    """Read every source for a stretch of time into the store, and say how many new records each gave."""
    added = {}
    for name in sources.load():
        step, left, total = CHUNK.get(name) or end - start, start, 0
        while left < end:
            right = min(left + step, end)
            try:
                total += history.add_docs([d.as_dict() for d in sources.query(name, left, right, None) if _worth_keeping(d.as_dict())])
            except sources.NotConfigured:
                break
            except Exception as exc:  # one broken source must not stop the others
                log(f"memory: {name} failed: {type(exc).__name__}: {exc}")
                break
            left = right
        added[name] = total
    return added


def keep_up(log=lambda _message: None) -> dict:
    """Read whatever has happened since the last time. The first time ever, that means the last two weeks."""
    now = time.time()
    added = ingest(history.ingested_until() or now - FIRST_LOOK_BACK, now, log)
    history.set_ingested_until(now)
    return added


def involved(rec: dict) -> list[str]:
    """The devices, ports and addresses this run is about, the place that actually broke first."""
    ev, r, values = rec["event"], rec["result"], rec.get("values") or {}
    places = [str(v) for v in (r.get("params") or {}).values()]
    places += [str(values.get(k) or "") for k in ("far_device", "far_interface", "peer_device", "peer_interface", "neighbor_ip", "peer_ip", "prefix")]
    places += [str(ev.get(k) or "") for k in ("device", "interface", "subject")]
    places += [r.get("text") or "", ev.get("title") or ""]
    names: dict[str, None] = {}
    for place in places:
        for word in _NAME.findall(place):
            word = word.lower().rstrip(".:")
            if word in _KNOWN or _PORT.match(word) or _ADDRESS.match(word):
                names.setdefault(word)
                if word.startswith("ethernet"):  # EOS logs write Ethernet1 as Et1
                    names.setdefault("et" + word.removeprefix("ethernet"))
    return list(names)


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"(?<![\w.:]){re.escape(name)}(?![\w:])", text) is not None


def _shape(title: str) -> str:
    """Two records with this in common are the same kind of thing: counts and build numbers vary, names like r3 do not."""
    return re.sub(r"(?<![A-Za-z])\d+", "#", title)


def _rank(found: list[dict], names: list[str], words: list[str], now: float, k: int) -> list[dict]:
    """Keep what is really about this, collapse repeats into one record with a count, and do not let one source fill the list."""
    groups: dict[tuple, list[dict]] = {}
    plain = [w for w in words if w not in names]
    our_ports = {n for n in names if _PORT.match(n)}
    for doc in found:
        if not _worth_keeping(doc):
            continue
        text = f"{doc['title']} {doc.get('text') or ''}".lower()
        named = [n for n in names if _mentions(text, n) or n == (doc.get("device") or "").lower()]
        if not named and not doc.get("near") and sum(_mentions(text, w) for w in plain) < 3:  # sharing a word like "link" is not a connection; being close in meaning is
            continue
        ports = set(_ANY_PORT.findall(text))
        elsewhere = OTHER_PORT if our_ports and ports and not ports & our_ports else 1
        age_days = max(0.0, now - doc["time"]) / 86400
        doc["score"] = -doc["rank"] * (1 + 0.6 * len(named)) * elsewhere * max(0.4, 0.5 ** (age_days / HALF_LIFE_DAYS))
        groups.setdefault((doc["source"], _shape(doc["title"])), []).append(doc)
    best, used = [], {}
    for group in sorted(groups.values(), key=lambda g: max(d["score"] for d in g), reverse=True):
        top = max(group, key=lambda d: d["score"])
        if used.get(top["source"], 0) >= PER_SOURCE:
            continue
        used[top["source"]] = used.get(top["source"], 0) + 1
        best.append({"source": top["source"], "time": top["time"], "device": top.get("device") or "", "title": top["title"], "text": (top.get("text") or "")[:KEEP_TEXT],
                     "url": top.get("url") or "", "score": round(top["score"], 1), "times_seen": len(group), "last_seen": max(d["time"] for d in group)})
        if len(best) == k:
            break
    return best


def recall(rec: dict, k: int = RECALL, path: Path | None = None) -> tuple[list[dict], dict]:
    """Older records that bear on this run, and how they were found. What was happening at the time arrives separately."""
    began = time.time()
    ev, r = rec["event"], rec["result"]
    names = involved(rec)
    label = (r.get("label") or "").replace(".", " ").replace("_", " ")
    words = history.terms(" ".join(names), label, r.get("text") or "", ev.get("title") or "")
    before = ev["time"] - sources.BEFORE
    # First what names this place and shares its topic, then anything else, so common device names cannot crowd those out.
    found = history.search_docs([w for w in words if w not in names], CANDIDATES, before, path, among=names) + history.search_docs(words, CANDIDATES, before, path)
    found += cards.docs_like(" ".join(filter(None, [ev.get("title"), ev.get("detail"), r.get("text")]))[:3000], CANDIDATES, before, path)
    found = list({doc["key"]: doc for doc in reversed(found)}.values())
    items = _rank(found, names, words, ev["time"], k)
    for n, item in enumerate(items, 1):
        item["ref"] = f"M{n}"
    held = history.sizes(path)
    return items, {"at": began, "seconds": round(time.time() - began, 3), "terms": words, "names": names, "before": before,
                   "searched": {"runs": held["runs"], "docs": held["docs"]}, "matched": len(found)}


def search(text: str, k: int = RECALL, path: Path | None = None) -> list[dict]:
    """The same search for a person to try by hand."""
    words = history.terms(text)
    names = [w for w in words if w in _KNOWN or _PORT.match(w) or _ADDRESS.match(w)]
    return _rank([*history.search_docs(words, CANDIDATES, time.time(), path), *cards.docs_like(text, CANDIDATES, None, path)], names, words, time.time(), k)
