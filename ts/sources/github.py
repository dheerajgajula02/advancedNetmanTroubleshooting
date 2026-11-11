"""Pushes, commits, pull requests and workflow runs on the network's repository, and GitHub's own status."""

from __future__ import annotations

import calendar
import json
import os
import subprocess
import time
import urllib.request

from ts.sources import Doc, NotConfigured, source

REPO = os.environ.get("TS_GITHUB_REPO", "dheerajgajula02/advancedNetworkManagement")
STATUS_URL = "https://www.githubstatus.com/api/v2/incidents.json"


def _gh(path: str):
    try:
        proc = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=25)
    except FileNotFoundError as exc:
        raise NotConfigured("the gh command is not installed") from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200])
    return json.loads(proc.stdout)


def _epoch(stamp: str) -> float:
    return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


@source("github")
def activity(start: float, end: float, device: str | None = None) -> list[Doc]:
    docs = []
    for c in _gh(f"repos/{REPO}/commits?since={_iso(start)}&until={_iso(end)}&per_page=30"):
        info = c["commit"]
        docs.append(Doc("github", _epoch(info["committer"]["date"]), f"Commit {c['sha'][:7]} by {info['author']['name']}: {info['message'].splitlines()[0]}",
                        info["message"][:800], c["html_url"]))
    for e in _gh(f"repos/{REPO}/events?per_page=60"):
        t = _epoch(e["created_at"])
        if not start <= t <= end:
            continue
        who, payload = e["actor"]["login"], e["payload"]
        if e["type"] == "PushEvent":
            docs.append(Doc("github", t, f"{who} pushed to {payload['ref'].removeprefix('refs/heads/')}", f"Head {payload.get('head', '')[:7]}."))
        elif e["type"] == "PullRequestEvent":
            pr = payload["pull_request"]
            docs.append(Doc("github", t, f"{who} {payload['action']} pull request #{pr.get('number', '')}: {pr.get('title', '')}", url=pr.get("html_url", "")))
    runs = _gh(f"repos/{REPO}/actions/runs?created=>={_iso(start)}&per_page=30").get("workflow_runs", [])
    for r in runs:
        t = _epoch(r["created_at"])
        if t <= end:
            docs.append(Doc("github", t, f"Workflow {r['name']} on {r['head_branch']}: {r.get('conclusion') or r['status']}", url=r["html_url"]))
    return docs + _platform_incidents(start, end)


def _platform_incidents(start: float, end: float) -> list[Doc]:
    request = urllib.request.Request(STATUS_URL, headers={"User-Agent": "network-troubleshooting/1.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        incidents = json.load(response).get("incidents", [])
    docs = []
    for i in incidents:
        began, resolved = _epoch(i["created_at"]), _epoch(i["resolved_at"]) if i.get("resolved_at") else time.time()
        if began <= end and resolved >= start:
            docs.append(Doc("github", began, f"GitHub incident ({i.get('impact')}): {i['name']}", i.get("status", ""), i.get("shortlink", "")))
    return docs
