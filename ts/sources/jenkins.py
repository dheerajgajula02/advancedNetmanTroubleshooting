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
# Jobs that change the network or test it by changing it.
CHANGES_NETWORK = re.compile(r"network-health-monitor|advtests-orchestrator|lab1-vrrp|lab2-traps|lab3-dashboards|deploy|reset|change|rotation|onboard|promote|config", re.I)
_NOTE = re.compile(r"\x1b\[8mha:.*?\x1b\[0m")
_FIELD = {k: re.compile(rf"<{k}>([^<]*)</{k}>") for k in ("startTime", "timestamp", "result", "duration", "completed")}
SCAN_BACK = 40


def _jobs(folder: Path = ROOT, prefix: str = ""):
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name in OTHERS or not entry.is_dir():
            continue
        name = f"{prefix}{entry.name}"
        if (entry / "jobs").is_dir():
            yield from _jobs(entry / "jobs", name + "/")
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
