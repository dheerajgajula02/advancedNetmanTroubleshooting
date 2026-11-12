"""The two questions a network engineer asks before touching anything.

Was it working before? Has anything changed? Both are answered from the network and its
records before any change is made, by a rule or by the AI, and the answers stay on the run.
"""

from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from ts import configs, scrub, store, sweep, tickets
from ts.sources import jenkins

RECENT = 86400        # how far back "recently" reaches
NEVER_AFTER = 86400   # how long something must have been checked before "it never worked" means anything
_WORKERS = 6
_MOST = 8


def _clock(moment: float) -> str:
    return time.strftime("%H:%M", time.localtime(moment))


def _check_line(key: str, known: dict, beat: float | None) -> str:
    name = key.replace(":", " ")
    if known.get("ok"):
        at = known.get("last_ok") or beat
        return f"{name}: working at the last check" + (f", {_clock(at)}" if at else "")
    return f"{name}: failing" + (f" since {_clock(known['since'])}" if known.get("since") else "") + (f", last seen working {_clock(known['last_ok'])}" if known.get("last_ok") else "")


def _earlier_trouble(device: str, skip: str, now: float) -> list[str]:
    rows = [r for r in store.summaries(store.DATA_DIR) if r["device"] == device and r["id"] != skip and now - r["time"] < RECENT]
    if not rows:
        return [f"No other problems recorded on {device} in the last 24 hours."]
    return [f"{len(rows)} other problem{'s' if len(rows) > 1 else ''} on {device} in the last 24 hours. Latest, {_clock(rows[0]['time'])}: {rows[0]['title']} ({rows[0]['state'].replace('_', ' ')})."]


def _was_it_working(event: dict, skip: str) -> dict:
    now, state, device = time.time(), sweep.load_state(), event.get("device") or ""
    beat = (state.get("_tick") or {}).get("at")
    mine = [(key, known) for key, known in state.items() if not key.startswith(("_", "drift:")) and (known.get("event") or {}).get("device") == device]
    exact = [pair for pair in mine if (pair[1].get("event") or {}).get("subject") == event.get("subject")]
    checks = exact or mine
    lines = [_check_line(key, known, beat) for key, known in checks[:_MOST]] + _earlier_trouble(device, skip, now)
    if not checks:
        return {"verdict": "unknown", "summary": f"Not known. Nothing regularly checks {device}.", "lines": lines}
    good = [known for _key, known in checks if known.get("ok")]
    if good:
        at = max((known.get("last_ok") or beat or 0) for known in good)
        return {"verdict": "yes", "summary": f"Yes. The last check{f' at {_clock(at)}' if at else ''} saw it working.", "lines": lines}
    worked = [known["last_ok"] for _key, known in checks if known.get("last_ok")]
    if worked:
        return {"verdict": "yes", "summary": f"Yes, until {_clock(max(worked))}. It has been failing since.", "lines": lines}
    if all(known.get("first_seen") and now - known["first_seen"] > NEVER_AFTER for _key, known in checks):
        return {"verdict": "no", "summary": "No. In more than a day of checks this has never been seen working.", "lines": lines}
    return {"verdict": "unknown", "summary": "Not known. It is failing and the records do not go back far enough to say.", "lines": lines}


def _one_device(device: str) -> tuple[str, dict | None, str]:
    try:
        good = configs.known_good(device)
        if good is None:
            return device, None, "has no known-good copy to compare with"
        _shown, now = configs.running(device)
        if now is None:
            return device, None, "did not return its config"
        return device, configs.managed_changes(configs.without_held(device, good[1]), configs.without_held(device, now)), good[0]
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return device, None, f"could not be read: {type(exc).__name__}"


_RECENT_CONFIGS: dict = {}  # the last comparison, for runs that come in a burst (see take's fresh_within)


def _config_differences_within(seconds: float) -> tuple[list[dict], list[str]]:
    at, value = _RECENT_CONFIGS.get("at", 0.0), _RECENT_CONFIGS.get("value")
    if value is not None and time.time() - at < seconds:
        return value
    value = _config_differences()
    _RECENT_CONFIGS.update(at=time.time(), value=value)
    return value


def forget_recent() -> None:
    """After the network was changed, the next look reads every device again."""
    _RECENT_CONFIGS.clear()


def _config_differences() -> tuple[list[dict], list[str]]:
    """Every device against its known-good config, leaving out what is on hold. (differences, what could not be compared)"""
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        results = list(pool.map(_one_device, sweep.CONFIG_DEVICES))
    differs = [{"device": device, "against": note, "sections": [{"section": section, **change} for section, change in list(changes.items())[:_MOST]]} for device, changes, note in results if changes]
    return differs, [f"{device} {note}" for device, changes, note in results if changes is None]


def _changes_we_made(skip: str, now: float) -> tuple[list[dict], list[str]]:
    """What was kept here lately: newest first, as records to explain differences with and as lines to read."""
    kept, out = [], []
    for row in store.summaries(store.DATA_DIR):
        if now - row["time"] > RECENT:
            break
        if row["id"] == skip or not (row.get("agent_changes") or row.get("repair") == "fixed"):
            continue
        rec = store.load(row["id"], store.DATA_DIR) or {}
        mine = [{**c, "run": row["id"], "ticket": rec["event"].get("ticket")} for c in (rec.get("agent") or {}).get("changes") or [] if c.get("status") == "kept"]
        kept += mine
        out += [f"{_clock(c.get('started') or row['time'])} on {c.get('device')}: {'; '.join(c.get('what') or [])}" + (" (temporary)" if c.get("temporary") else "") for c in mine]
        if (rec.get("repair") or {}).get("status") == "fixed":
            out.append(f"{_clock(row['time'])} on {row['device']}: rule repair, {rec['repair'].get('title')}")
    return kept, out[:_MOST]


def _line_said(line: str) -> str:
    return line.split(" > ")[-1].strip().lower()


def _explain(differs: list[dict], kept: list[dict]) -> None:
    """Mark the differences an earlier change of ours fully accounts for. Whatever is left is drift."""
    for item in differs:
        mine = [c for c in kept if c.get("device") == item["device"]]
        said = {line.strip().lower() for c in mine for line in c.get("what") or []}
        for section in item["sections"]:
            lines = ({_line_said(x) for x in section.get("added") or []} | {f"no {_line_said(x)}" for x in section.get("removed") or []}) - {section["section"].lower(), f"no {section['section'].lower()}"}
            if lines and lines <= said:
                by = next(c for c in mine if lines & {line.strip().lower() for line in c.get("what") or []})
                section["because"] = {"run": by["run"], "ticket": by.get("ticket"), "why": by.get("why", ""), "at": by.get("started"), "temporary": bool(by.get("temporary"))}


def _ticket_words(number) -> str:
    if not number:
        return ""
    ticket = tickets.get(int(number))
    return f"T-{number}, {ticket['status'].replace('_', ' ')}" if ticket else f"T-{number}"


def _earlier_changes() -> list[dict]:
    """What was changed on purpose and is being kept, one entry for each piece of work, with the reasons."""
    groups: dict[str, dict] = {}
    for hold in configs.holds():
        group = groups.setdefault(hold["run"], {"run": hold["run"], "ticket": hold.get("ticket"), "ticket_words": _ticket_words(hold.get("ticket")), "since": hold.get("since"), "changes": []})
        group["changes"].append({"device": hold["device"], "lines": hold["lines"], "why": hold.get("why", "")})
    for group in groups.values():
        rec = store.load(group["run"], store.DATA_DIR) or {}
        group["root_cause"], group["next"] = (rec.get("agent") or {}).get("root_cause", ""), (rec.get("analysis") or {}).get("next", "")
    return list(groups.values())


def _deploys() -> list[str]:
    try:
        return jenkins.recent_deploys(RECENT)[:_MOST]
    except Exception as exc:  # Jenkins being away must not stop anyone looking at the network
        return [f"Jenkins could not be asked: {type(exc).__name__}"]


def _has_anything_changed(docs: list[dict], skip: str, fresh_within: float = 0) -> dict:
    now = time.time()
    differs, unread = _config_differences_within(fresh_within) if fresh_within else _config_differences()
    earlier = _earlier_changes()
    kept, ours = _changes_we_made(skip, now)
    deploys = _deploys()
    _explain(differs, kept)
    happened = [d["title"] for d in docs if d.get("source") in ("prometheus", "github")][:_MOST]
    drifted = [d["device"] for d in differs if any("because" not in s for s in d["sections"])]
    explained = [d["device"] for d in differs if d["device"] not in drifted]
    config = " ".join(filter(None, [f"{', '.join(drifted)} differ{'s' if len(drifted) == 1 else ''} from known-good and nothing on record explains it." if drifted else "",
                                    f"{', '.join(explained)} differ{'s' if len(explained) == 1 else ''} from known-good because of an earlier change here." if explained else "",
                                    "" if differs else "Every device that could be read matches known-good."]))
    deliberate = "; ".join(f"{', '.join(sorted({c['device'] for c in group['changes']}))}" + (f" for {group['ticket_words']}" if group["ticket_words"] else "") for group in earlier)
    others = [words for words, found in (("changes made here in the last 24 hours", ours), ("recent deployments", deploys), ("other things changing around then", happened)) if found]
    return {"verdict": "yes" if differs or others or earlier else "no", "summary": " ".join(filter(None, [config, f"Kept on purpose from earlier work: {deliberate}." if earlier else "", f"Also: {', '.join(others)}." if others else "Nothing else has changed recently."])),
            "config": differs, "drift": drifted, "unread": unread, "earlier": earlier, "ours": ours, "deploys": deploys, "events": happened}


def _safely(work, *args) -> dict:
    try:
        return work(*args)
    except Exception as exc:  # the look must never be why an investigation stops
        return {"verdict": "unknown", "summary": f"This could not be checked ({type(exc).__name__}).", "lines": []}


def take(event: dict, docs: list[dict] | None = None, run_id: str = "", fresh_within: float = 0) -> dict:
    """fresh_within: reuse a comparison of every device's config made this many seconds ago, for runs that come in a burst."""
    return scrub.deep({"time": time.time(), "working": _safely(_was_it_working, event, run_id), "changed": _safely(_has_anything_changed, docs or [], run_id, fresh_within)})


def _config_lines(differs: list[dict]) -> tuple[list[str], list[str]]:
    """The differences nothing explains, and the ones an earlier change explains."""
    drift, explained = [], []
    for item in differs:
        for section in item["sections"]:
            line = f"- {item['device']} ({item['against']}), {section['section']}: removed {section.get('removed') or 'nothing'}, added {section.get('added') or 'nothing'}"
            by = section.get("because")
            if by:
                explained.append(f"{line}. Made at {_clock(by['at'] or time.time())} by run {by['run']}" + (f" for ticket T-{by['ticket']}" if by.get("ticket") else "") + f": {by['why'] or 'no reason recorded'}")
            else:
                drift.append(line)
    return drift, explained


def _earlier_lines(earlier: list[dict]) -> list[str]:
    out = []
    for group in earlier:
        out.append(f"- Run {group['run']}" + (f", ticket {group['ticket_words']}" if group["ticket_words"] else "") + (f", since {_clock(group['since'])}" if group.get("since") else ""))
        out += [f"  On {c['device']}: {'; '.join(c['lines'])}. Why: {c['why'] or 'not recorded'}" for c in group["changes"]]
        out += [f"  {label}: {value}" for label, value in (("Cause found then", group.get("root_cause")), ("Left for a person to do", group.get("next"))) if value]
    return out


def text(look: dict) -> str:
    """How the AI reads it."""
    working, changed = look["working"], look["changed"]
    out = ["FIRST LOOK, taken before anything else", f"WAS IT WORKING BEFORE? {working['summary']}", *[f"- {line}" for line in working.get("lines") or []], f"HAS ANYTHING CHANGED? {changed['summary']}"]
    drift, explained = _config_lines(changed.get("config") or [])
    if drift:
        out += ["DRIFT: differs from known-good and nothing on record explains it:", *drift]
    if explained:
        out += ["Differs from known-good because of an earlier change here. It is not being kept, so it goes back to known-good at the next heartbeat:", *explained]
    if changed.get("earlier"):
        out += ["CHANGED HERE EARLIER, ON PURPOSE, AND STILL IN PLACE. These are left out of the comparison above. Do not put them back just because they differ:", *_earlier_lines(changed["earlier"])]
    for label, key in (("Could not be compared", "unread"), ("Changes made here in the last 24 hours", "ours"), ("Recent deployments", "deploys"), ("Also changing around then", "events")):
        if changed.get(key):
            out += [f"{label}:", *[f"- {line}" for line in changed[key]]]
    return "\n".join(out)
