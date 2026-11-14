"""Which automation jobs ran. Read from the build records on disk, so it needs no login."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from ts.sources import Doc, source

ROOT = Path(os.environ.get("TS_JENKINS_JOBS", "/var/lib/jenkins/jobs"))
# Jobs that have nothing to do with this network.
OTHERS = {"metaGlobalHack", "lab6-sdn-telemetry", "personal_pipelines", "others"}
# The troubleshooter's own rule pipelines. They are how it looks and repairs, not jobs that change
# the network on their own, so they never count as tests running or as context for a run.
OWN = {"troubleshooting-pipelines"}
# Jobs that change the network or test it by changing it.
CHANGES_NETWORK = re.compile(r"network-health-monitor|advtests-orchestrator|lab1-vrrp|lab2-traps|lab3-dashboards|deploy|reset|change|rotation|onboard|promote|config", re.I)
_NOTE = re.compile(r"\x1b\[8mha:.*?\x1b\[0m")
_FIELD = {k: re.compile(rf"<{k}>([^<]*)</{k}>") for k in ("startTime", "timestamp", "result", "duration", "completed")}
SCAN_BACK = 40
# What the network does just after one of those jobs is often still its doing (a failover settling, a port coming back).
AFTER_A_JOB = 300
# The hourly health monitor holds the change lock whenever it changes anything. Counting its runs as well would leave
# traps and the sweep deaf for the minutes after each one, every hour.
HOLDS_THE_LOCK = re.compile(r"network-health-monitor", re.I)


def _jobs(folder: Path | None = None, prefix: str = "", own: bool = False):
    """Every job of this network, folders opened. With own, the troubleshooter's pipelines too."""
    try:
        entries = sorted((folder or ROOT).iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name in OTHERS or (not own and not prefix and entry.name in OWN) or not entry.is_dir():
            continue
        name = f"{prefix}{entry.name}"
        if (entry / "jobs").is_dir():
            yield from _jobs(entry / "jobs", name + "/", own)
        elif (entry / "nextBuildNumber").is_file():
            yield name, entry


def _field(xml: str, key: str) -> str | None:
    m = _FIELD[key].search(xml)
    return m.group(1) if m else None


def _build(job_dir: Path, number: int) -> dict | None:
    folder = job_dir / "builds" / str(number)
    try:
        xml = (folder / "build.xml").read_text(errors="replace")
    except FileNotFoundError:
        if not folder.is_dir():
            return None
        return {"number": number, "start": folder.stat().st_mtime, "result": None, "seconds": 0.0, "folder": folder}  # still starting
    except OSError:
        return None
    started = _field(xml, "startTime") or _field(xml, "timestamp")
    if not started:
        return None
    done = _field(xml, "completed") != "false" and _field(xml, "result") is not None
    return {"number": number, "start": int(started) / 1000, "result": _field(xml, "result") if done else None,
            "seconds": int(_field(xml, "duration") or 0) / 1000, "folder": folder}


def _recent(job_dir: Path, stop_before: float):
    try:
        last = int((job_dir / "nextBuildNumber").read_text().strip()) - 1
    except (OSError, ValueError):
        return
    for number in range(last, max(0, last - SCAN_BACK), -1):
        build = _build(job_dir, number)
        if build is None:
            continue
        end = build["start"] + build["seconds"] if build["result"] else time.time()
        if end < stop_before:
            return
        yield build


def _log_tail(folder: Path, lines: int = 25) -> str:
    try:
        raw = (folder / "log").read_text(errors="replace")
    except OSError:
        return ""
    clean = [_NOTE.sub("", line).rstrip() for line in raw.splitlines()]
    return "\n".join(line for line in clean if line)[-1800:] if lines else ""


DEPLOYS = re.compile(r"deploy|change|promote|onboard|reset|config-deployment", re.I)


def recent_deploys(within: float = 600) -> list[str]:
    """Deployment jobs that finished in the last few minutes: what they changed is meant to be there."""
    since = time.time() - within
    return [f"{name} #{b['number']}" for name, folder in _jobs() if DEPLOYS.search(name)
            for b in _recent(folder, since) if b["result"] and b["start"] + b["seconds"] >= since]


def running_change_jobs() -> list[str]:
    """Jobs running right now that change the network or test it by changing it."""
    return [f"{name} #{b['number']}" for name, folder in _jobs() if CHANGES_NETWORK.search(name)
            for b in _recent(folder, time.time() - 21600) if b["result"] is None and time.time() - b["start"] < 14400]


def changing_network_now(now: float | None = None, after: float = AFTER_A_JOB) -> list[str]:
    """Jobs changing the network now, or that finished less than `after` seconds ago. The nightly tests break things
    on purpose without taking the change lock, so a trap or a sweep finding from that time is most likely theirs."""
    now = time.time() if now is None else now
    return [w["job"] for w in change_windows(now - after)
            if w["start"] <= now <= w["end"] + after and not HOLDS_THE_LOCK.search(w["job"])]


def change_windows(since: float, scan: int = 400) -> list[dict]:
    """When jobs that change the network, or test it by changing it, were running, back to `since`.

    Each is {"job", "start", "end"}; one still running ends now. Monitors use this to tell a fault a
    test caused on purpose from a real one, both live and when replaying the past week.
    """
    now, out = time.time(), []
    for name, folder in _jobs():
        if not CHANGES_NETWORK.search(name):
            continue
        try:
            last = int((folder / "nextBuildNumber").read_text().strip()) - 1
        except (OSError, ValueError):
            continue
        for number in range(last, max(0, last - scan), -1):
            b = _build(folder, number)
            if b is None:
                continue
            end = b["start"] + b["seconds"] if b["result"] else min(now, b["start"] + 14400)  # a build left without a result stops counting after 4 h
            if end < since:
                break
            out.append({"job": f"{name} #{number}", "start": b["start"], "end": end})
    return sorted(out, key=lambda w: w["start"])


@source("jenkins")
def builds(start: float, end: float, device: str | None = None) -> list[Doc]:
    docs = []
    for name, folder in _jobs():
        for b in _recent(folder, start):
            finished = b["start"] + b["seconds"]
            if b["start"] > end or (b["result"] and finished < start):
                continue
            state = b["result"] or "RUNNING"
            if state == "SUCCESS" and not CHANGES_NETWORK.search(name):  # the once-a-minute housekeeping jobs
                continue
            changes = "It changes the network or tests it by changing it. " if CHANGES_NETWORK.search(name) else ""
            docs.append(Doc("jenkins", b["start"], f"Job {name} #{b['number']}: {state}",
                            f"{changes}Started {time.strftime('%H:%M:%S', time.localtime(b['start']))}, ran {b['seconds']:.0f} s."
                            + (f"\n{_log_tail(b['folder'])}" if state in ("FAILURE", "UNSTABLE") else ""),
                            f"https://jenkins.dheerajgajula.com/job/{'/job/'.join(name.split('/'))}/{b['number']}/"))
    return docs
