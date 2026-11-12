"""Talking to Jenkins over its API: the troubleshooting folder, its pipelines, and their builds.

The login is the API token the ZTP webhook already uses, read from /etc/netman/jenkins-api.env
each time it is needed. It is never copied, logged or sent anywhere but to Jenkins. Requests
signed with an API token need no CSRF crumb.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_FILE = Path(os.environ.get("TS_JENKINS_ENV", "/etc/netman/jenkins-api.env"))
LINK_BASE = os.environ.get("TS_JENKINS_LINK", "https://jenkins.dheerajgajula.com").rstrip("/")
FOLDER = "troubleshooting-pipelines"
TIMEOUT = 20


class Unavailable(RuntimeError):
    """Jenkins could not be reached, refused the login, or answered with an error."""


def _settings() -> tuple[str, str]:
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError as exc:
        raise Unavailable(f"cannot read {ENV_FILE}: {exc.strerror}") from None
    env = {}
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            env[key.strip()] = value.strip().strip('"').strip("'")
    url, user, token = env.get("JENKINS_URL", "http://localhost:8080"), env.get("JENKINS_USER"), env.get("JENKINS_TOKEN")
    if not (user and token):
        raise Unavailable(f"{ENV_FILE} has no JENKINS_USER and JENKINS_TOKEN")
    return url.rstrip("/"), "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


def _request(method: str, path: str, data: bytes | None = None, content_type: str = "", want: str = "json", timeout: float = TIMEOUT):
    base, auth = _settings()
    headers = {"Authorization": auth}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if want == "location":
                return response.headers.get("Location", "")
            body = response.read()
            return json.loads(body) if want == "json" else body.decode(errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and want in ("json", "text", "exists"):
            return None
        raise Unavailable(f"Jenkins answered {exc.code} to {method} {path.split('?')[0]}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Unavailable(f"Jenkins did not answer: {getattr(exc, 'reason', exc)}") from None


def job_path(name: str, folder: str = FOLDER) -> str:
    """The API path of a job inside the folder."""
    return f"/job/{urllib.parse.quote(folder)}/job/{urllib.parse.quote(name)}"


def link(name: str | None = None, number: int | None = None, folder: str = FOLDER) -> str:
    """The address a person opens: the folder, a pipeline, or one build."""
    url = f"{LINK_BASE}/job/{urllib.parse.quote(folder)}/"
    if name:
        url += f"job/{urllib.parse.quote(name)}/"
    if name and number:
        url += f"{number}/"
    return url


def free_executors() -> int:
    """How many executors are idle right now, across every node."""
    found = _request("GET", "/computer/api/json?tree=busyExecutors,totalExecutors", timeout=5) or {}
    return int(found.get("totalExecutors", 0)) - int(found.get("busyExecutors", 0))


def alive() -> bool:
    try:
        return _request("GET", "/api/json?tree=mode", timeout=5) is not None
    except Unavailable:
        return False


# --- the folder and its pipelines -------------------------------------------------

FOLDER_XML = """<?xml version='1.1' encoding='UTF-8'?>
<com.cloudbees.hudson.plugins.folder.Folder plugin="cloudbees-folder">
  <description>{description}</description>
  <properties/>
  <folderViews class="com.cloudbees.hudson.plugins.folder.views.DefaultFolderViewHolder">
    <views><hudson.model.AllView><owner class="com.cloudbees.hudson.plugins.folder.Folder" reference="../../../.."/><name>All</name><filterExecutors>false</filterExecutors><filterQueue>false</filterQueue><properties class="hudson.model.View$PropertyList"/></hudson.model.AllView></views>
    <tabBar class="hudson.views.DefaultViewsTabBar"/>
  </folderViews>
  <healthMetrics/>
</com.cloudbees.hudson.plugins.folder.Folder>
"""


def ensure_folder(description: str) -> bool:
    """Create the folder if it is missing. True if it was created."""
    if _request("GET", f"/job/{urllib.parse.quote(FOLDER)}/api/json?tree=name") is not None:
        return False
    xml = FOLDER_XML.format(description=_xml(description)).encode()
    _request("POST", f"/createItem?name={urllib.parse.quote(FOLDER)}", xml, "application/xml; charset=utf-8", want="text")
    return True


def jobs() -> dict[str, dict]:
    """The pipelines in the folder: name -> {"disabled", "description"}."""
    found = _request("GET", f"/job/{urllib.parse.quote(FOLDER)}/api/json?tree=jobs[name,color,description]")
    return {j["name"]: {"disabled": j.get("color") == "disabled", "description": j.get("description") or ""} for j in (found or {}).get("jobs", [])}


def config(name: str) -> str | None:
    return _request("GET", f"{job_path(name)}/config.xml", want="text")


def put_job(name: str, xml: str, exists: bool) -> None:
    if exists:
        _request("POST", f"{job_path(name)}/config.xml", xml.encode(), "application/xml; charset=utf-8", want="text")
    else:
        _request("POST", f"/job/{urllib.parse.quote(FOLDER)}/createItem?name={urllib.parse.quote(name)}", xml.encode(), "application/xml; charset=utf-8", want="text")


def set_enabled(name: str, enabled: bool) -> None:
    _request("POST", f"{job_path(name)}/{'enable' if enabled else 'disable'}", b"", want="text")


def delete_job(name: str) -> None:
    _request("POST", f"{job_path(name)}/doDelete", b"", want="text")


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# --- builds ------------------------------------------------------------------------

def start(name: str, params: dict[str, str]) -> str:
    """Queue a build of a pipeline. Returns the queue item's address."""
    body = urllib.parse.urlencode(params).encode()
    where = _request("POST", f"{job_path(name)}/buildWithParameters", body, "application/x-www-form-urlencoded", want="location")
    if not where:
        raise Unavailable("Jenkins queued the build but did not say where")
    return where


def started_number(queue_url: str) -> tuple[int | None, str]:
    """(build number once it has started, why it never will). Both empty while it waits."""
    base, _auth = _settings()
    path = queue_url.replace(base, "", 1) if queue_url.startswith(base) else urllib.parse.urlsplit(queue_url).path
    item = _request("GET", path.rstrip("/") + "/api/json")
    if item is None:
        return None, "the queue item is gone"
    if item.get("cancelled"):
        return None, "the build was cancelled in the queue"
    executable = item.get("executable") or {}
    return executable.get("number"), ""


def cancel(queue_url: str) -> None:
    try:
        item_id = queue_url.rstrip("/").rsplit("/", 1)[-1]
        _request("POST", f"/queue/cancelItem?id={urllib.parse.quote(item_id)}", b"", want="text")
    except Unavailable:
        pass


def build(name: str, number: int) -> dict | None:
    """{"building", "result", "duration", "description"} of one build."""
    return _request("GET", f"{job_path(name)}/{number}/api/json?tree=building,result,duration,description,displayName,timestamp")


def stop(name: str, number: int) -> None:
    try:
        _request("POST", f"{job_path(name)}/{number}/stop", b"", want="text")
    except Unavailable:
        pass


def wait_started(queue_url: str, within: float) -> tuple[int | None, str]:
    """Wait for a queued build to start. (number, "") or (None, why not)."""
    deadline = time.time() + within
    while True:
        number, why = started_number(queue_url)
        if number or why:
            return number, why
        if time.time() >= deadline:
            return None, f"Jenkins had not started it after {within:.0f} s"
        time.sleep(1)
