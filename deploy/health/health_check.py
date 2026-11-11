#!/usr/bin/env python3
"""Every five minutes: is everything that should be up, up? Emails when that changes.

    health_check.py           run every check, save the result, email what broke or came back
    health_check.py --print   run every check and print the result; no email, nothing saved

A check that fails twice in a row is emailed; one that comes back is emailed too; anything still
failing is repeated every 12 hours. Results go to data/health.json in this repo.

The signed-in walk through nmas runs once an hour, and only when a monitor account's file exists
at ~/.config/nmas/health-check.json ({"username": ..., "password": ...}, mode 600).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TS = Path(__file__).resolve().parent.parent.parent
STATE = TS / "data" / "health.json"
BACKUP_STATUS = Path("/var/lib/boulder-backup/status.json")
MONITOR = Path.home() / ".config" / "nmas" / "health-check.json"
JENKINS = Path("/var/lib/jenkins/jobs")
MAILER_URL = os.environ.get("MAILER_URL", "http://127.0.0.1:8025/send")
MAILER_TOKEN = Path(os.environ.get("MAILER_TOKEN_FILE", "/opt/lab3/mailer.token"))
NMAS = "https://nmas.shiplabs.net"
HOST = "10.9.8.182"
REMIND = 12 * 3600
WALK_EVERY = 3600

UNITS = ["netman-gui", "netman-troubleshoot-watch", "netman-troubleshoot-ui", "netman-troubleshoot-desk",
         "netman-onboard-webhook", "jenkins", "lab2-gnmic-collector", "lab2-snmp-exporter", "cloudflared", "docker"]
CONTAINERS = ["netbox-ipam", "netbox-postgres", "netbox-valkey", "netbox-valkey-cache", "netbox-public",
              "guacamole-web", "guacamole-postgres", "ts-training-db", "lab1-tacacs-mfa"]
LAB_PREFIX = "clab-lab1-"
PAGES = {  # name -> (url, extra headers, statuses that mean it works)
    "Netman GUI": (f"http://{HOST}:8050/healthz", {}, {200}),
    "troubleshooter pages": (f"http://{HOST}:8770/", {}, {200}),
    "service desk": (f"http://{HOST}:8771/", {}, {200}),
    "NetBox": (f"http://{HOST}:8000/", {}, {200, 302}),
    "NetBox for nmas": (f"http://{HOST}:8001/ipam/", {"Host": "nmas.shiplabs.net"}, {200, 302}),
    "Guacamole": (f"http://{HOST}:8088/guacamole/", {}, {200}),
    "Grafana": ("http://10.0.0.58:3000/api/health", {}, {200}),
    "Prometheus": ("http://10.9.8.99:9090/-/ready", {}, {200}),
    "topology API": ("http://10.9.8.238:8090/", {}, {200}),
    "Jenkins": ("http://localhost:8080/login", {}, {200}),
    "nmas is up": (f"{NMAS}/healthz", {}, {200}),
}
JOBS = {  # Jenkins job -> (what it is, longest gap between finished runs, how many failures in a row is a problem)
    "advNetmanTests/jobs/advtests-orchestrator": ("nightly test run", 26 * 3600, 1),
    "advNetmanTests/jobs/network-health-monitor": ("hourly network health monitor", 2 * 3600, 2),
    "ci-cd/jobs/password-rotation": ("daily password rotation", 26 * 3600, 1),
    "Lab3-Monitoring/jobs/lab3-sync-events": ("syslog and trap sync to the data lake", 15 * 60, 3),
    "lab6-sdn-telemetry": ("SDN telemetry into the Mongo on pve2", 15 * 60, 3),
}


class Result:
    def __init__(self, state: str, detail: str):
        self.state, self.detail = state, detail


def ok(detail: str = "") -> Result:
    return Result("ok", detail)


def fail(detail: str) -> Result:
    return Result("fail", detail)


def warn(detail: str) -> Result:
    return Result("warn", detail)


def ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def http(url: str, headers: dict | None = None, timeout: int = 10, opener=None, data: bytes | None = None):
    """(status, final url, body) without following redirects unless an opener says so."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "boulder-health", **(headers or {})})
    op = opener or urllib.request.build_opener(_NoRedirect)
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read() if e.fp else b""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# ---------- the checks ----------

def check_units() -> Result:
    down = [u for u in UNITS if subprocess.run(["systemctl", "is-active", "--quiet", u]).returncode]
    failed = subprocess.run(["systemctl", "--failed", "--no-legend", "--plain"], capture_output=True, text=True).stdout
    failed = [line.split()[0] for line in failed.splitlines() if line.strip()]
    if down or failed:
        return fail("; ".join(x for x in [f"not running: {', '.join(down)}" if down else "",
                                           f"failed units: {', '.join(failed)}" if failed else ""] if x))
    return ok(f"{len(UNITS)} services running")


def check_page(name: str) -> Result:
    url, headers, good = PAGES[name]
    try:
        status, _, _ = http(url, headers)
    except Exception as e:
        return fail(f"{url} does not answer ({type(e).__name__})")
    return ok() if status in good else fail(f"{url} answered {status}")


def check_gate() -> Result:
    """Signed out, the public address must send people to the sign-in page, tools included."""
    problems = []
    for path, want in [("/", 303), ("/ipam/", 302), ("/guacamole/", 302), ("/prometheus/", 302)]:
        try:
            status, headers, _ = http(NMAS + path, {"Accept": "text/html"})
        except Exception as e:
            problems.append(f"{path} does not answer ({type(e).__name__})")
            continue
        where = headers.get("Location", "") if headers else ""
        if status != want or "/login" not in where:
            problems.append(f"{path} answered {status} {where or ''}".strip())
    return fail("sign-in gate open or broken: " + "; ".join(problems)) if problems else ok("signed-out visitors go to sign-in")


def check_containers() -> Result:
    out = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"],
                         capture_output=True, text=True, timeout=30).stdout
    rows = [line.split("\t") for line in out.splitlines() if line.count("\t") == 2]
    state = {n: (s, st) for n, s, st in rows}
    missing = [c for c in CONTAINERS if state.get(c, ("", ""))[0] != "running"]
    lab = {n: v for n, v in state.items() if n.startswith(LAB_PREFIX)}
    lab_down = [n for n, (s, _) in lab.items() if s != "running"]
    sick = [n for n, (s, st) in state.items() if "unhealthy" in st or s == "restarting"]
    ours = [n for n in sick if n in CONTAINERS or n.startswith(LAB_PREFIX)]
    problems = []
    if missing:
        problems.append(f"not running: {', '.join(missing)}")
    if lab_down:
        problems.append(f"lab devices down: {', '.join(lab_down)}")
    if ours:
        problems.append(f"unhealthy: {', '.join(ours)}")
    if problems:
        return fail("; ".join(problems))
    others = [n for n in sick if n not in ours]
    if others:
        return warn(f"other projects' containers unhealthy: {', '.join(others)}")
    return ok(f"{len(CONTAINERS)} services and {len(lab)} lab devices running")


def check_lake() -> Result:
    import pymongo
    try:
        db = pymongo.MongoClient("10.0.0.148", 27017, serverSelectionTimeoutMS=5000, socketTimeoutMS=10000)["datalake"]
        now = dt.datetime.now(dt.timezone.utc)
        newest = None
        for day in (now, now - dt.timedelta(days=1)):
            doc = db[day.strftime("%Y_%m_%d")].find_one({}, {"_id": 1}, sort=[("_id", -1)])
            if doc:
                newest = doc["_id"].generation_time
                break
    except Exception as e:
        return fail(f"the data lake does not answer ({type(e).__name__})")
    if newest is None:
        return fail("the data lake has nothing for today or yesterday")
    age = (now - newest).total_seconds()
    return ok(f"newest event {ago(age)} old") if age < 15 * 60 else fail(f"no new events for {ago(age)}")


def check_troubleshooter() -> Result:
    try:
        status = json.loads((TS / "data" / "status.json").read_text())
    except (OSError, ValueError):
        return fail("the watcher has written no status")
    age = time.time() - status.get("alive", 0)
    return ok(f"watcher alive {ago(age)} ago") if age < 10 * 60 else fail(f"the watcher last checked in {ago(age)} ago")


def check_embeddings() -> Result:
    try:
        env = dict(line.split("=", 1) for line in (TS / "secrets" / "embed.env").read_text().splitlines() if "=" in line)
    except OSError:
        return warn("no embedding settings")
    bases = [b for b in env.get("EMBED_BASES", "").split(",") if b]
    up = []
    for base in bases:
        try:
            http(base.rstrip("/") + "/", timeout=5)
            up.append(base)
        except Exception:
            pass
    if up:
        return ok(f"reachable: {', '.join(up)}")
    return fail("no embedding server answers, so past runs are found by keywords only "
                "(run deploy/embed/install-on-gpu.sh on the GPU machine)")


def check_backups() -> Result:
    try:
        status = json.loads(BACKUP_STATUS.read_text())
    except (OSError, ValueError):
        return fail("no backup has run yet")
    problems = []
    b, c = status.get("backup") or {}, status.get("check") or {}
    if not b.get("ok"):
        problems.append(f"last backup failed: {b.get('error', 'never ran')}")
    elif time.time() - b["at"] > 26 * 3600:
        problems.append(f"last backup was {ago(time.time() - b['at'])} ago")
    if not c.get("ok"):
        problems.append(f"last restore check failed: {c.get('error', 'never ran')}")
    elif time.time() - c["at"] > 8 * 86400:
        problems.append(f"last restore check was {ago(time.time() - c['at'])} ago")
    if problems:
        return fail("; ".join(problems))
    return ok(f"backup {ago(time.time() - b['at'])} ago ({b.get('bytes', 0) / 1e6:.0f} MB), "
              f"restore checked {ago(time.time() - c['at'])} ago")


def last_builds(job: str, n: int) -> list[tuple[str, float]]:
    """(result, finished time) of the newest n finished builds, newest first."""
    builds = JENKINS / job / "builds"
    numbers = sorted((int(p.name) for p in builds.iterdir() if p.name.isdigit()), reverse=True)
    out = []
    for number in numbers[: n + 5]:
        f = builds / str(number) / "build.xml"
        try:
            m = re.search(r"<result>([A-Z_]+)</result>", f.read_text(errors="replace"))
        except OSError:
            continue
        if m:
            out.append((m.group(1), f.stat().st_mtime))
        if len(out) == n:
            break
    return out


def check_job(job: str) -> Result:
    what, gap, streak = JOBS[job]
    try:
        builds = last_builds(job, streak)
    except OSError:
        return fail(f"cannot read the {what}'s builds")
    if not builds:
        return fail(f"the {what} has never finished")
    result, when = builds[0]
    age = time.time() - when
    if age > gap:
        return fail(f"the {what} last finished {ago(age)} ago")
    if len(builds) >= streak and all(r != "SUCCESS" for r, _ in builds[:streak]):
        return fail(f"the {what} failed {streak} time(s) in a row (last {result}, {ago(age)} ago)")
    return ok(f"last run {result}, {ago(age)} ago")


def check_disk() -> Result:
    usage = shutil.disk_usage("/")
    free = usage.free / usage.total
    if free < 0.10:
        return fail(f"only {free:.0%} of the root disk is free")

    def busy():
        for line in open("/proc/diskstats"):
            p = line.split()
            if p[2] == ROOT_DISK:
                return int(p[12])
        return 0
    a = busy()
    time.sleep(3)
    pct = (busy() - a) / 30
    if pct > 95:
        return warn(f"root disk {pct:.0f}% busy; {free:.0%} free")
    return ok(f"root disk {pct:.0f}% busy; {free:.0%} free")


def _root_disk() -> str:
    dev = subprocess.run(["findmnt", "-no", "SOURCE", "/"], capture_output=True, text=True).stdout.strip()
    parent = subprocess.run(["lsblk", "-no", "PKNAME", dev], capture_output=True, text=True).stdout.strip()
    return parent or os.path.basename(dev)


ROOT_DISK = _root_disk()


def check_walk(state: dict) -> Result | None:
    """Sign in on the public address as the monitor account and open each tool, with its styles."""
    if not MONITOR.exists():
        return warn("no monitor account yet, so the signed-in walk does not run")
    last = state.get("walk") or {}
    if time.time() - last.get("at", 0) < WALK_EVERY:
        return Result(last.get("state", "ok"), last.get("detail", "")) if last else None
    account = json.loads(MONITOR.read_text())
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect)
    headers = {"Origin": NMAS, "Referer": NMAS + "/login", "Accept": "text/html"}
    problems = []
    try:
        form = urllib.parse.urlencode({"username": account["username"], "password": account["password"], "next": "/"}).encode()
        status, _, _ = http(NMAS + "/login", headers, 20, opener, form)
        if status != 303:
            raise RuntimeError(f"sign-in answered {status}")
        for path, good in [("/", {200}), ("/troubleshooting", {200}), ("/dashboards", {200}), ("/ipam/", {200}),
                           ("/guacamole/", {200}), ("/prometheus/", {200, 302}), ("/desk/", {200})]:
            status, _, body = http(NMAS + path, headers, 30, opener)
            if status not in good:
                problems.append(f"{path} answered {status}")
                continue
            for asset in sorted(set(re.findall(rb'(?:href|src)=["\'](/[^"\'?#]+\.(?:css|js))', body)))[:12]:
                a_status, _, _ = http(NMAS + asset.decode(), headers, 20, opener)
                if a_status != 200:
                    problems.append(f"{path} needs {asset.decode()}, which answered {a_status}")
        http(NMAS + "/logout", headers, 20, opener, b"")
    except Exception as e:
        problems.append(f"{type(e).__name__}: {e}")
    result = fail("signed in: " + "; ".join(problems[:6])) if problems else ok("signed in and opened every tool with its styles")
    state["walk"] = {"at": time.time(), "state": result.state, "detail": result.detail}
    return result


# ---------- running, remembering, telling ----------

def run_all(state: dict) -> dict[str, Result]:
    jobs = {
        "services": check_units,
        "containers": check_containers,
        "nmas sign-in gate": check_gate,
        "data lake": check_lake,
        "troubleshooter": check_troubleshooter,
        "embedding server": check_embeddings,
        "backups": check_backups,
        "disk": check_disk,
        **{name: (lambda n=name: check_page(n)) for name in PAGES},
        **{JOBS[j][0]: (lambda j=j: check_job(j)) for j in JOBS},
    }
    results = {}
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fn): name for name, fn in jobs.items()}
        done, _ = cf.wait(futures, timeout=120)
        for future, name in futures.items():
            if future not in done:
                results[name] = fail("the check did not finish within 2 minutes")
                continue
            try:
                results[name] = future.result()
            except Exception as e:
                results[name] = fail(f"the check itself broke: {type(e).__name__}: {e}")
    walk = check_walk(state)
    if walk is not None:
        results["signed-in walk"] = walk
    return dict(sorted(results.items()))


def mail(subject: str, body: str) -> None:
    req = urllib.request.Request(MAILER_URL, data=json.dumps({"subject": subject, "body": body}).encode(),
                                 headers={"Content-Type": "application/json", "X-Mailer-Token": MAILER_TOKEN.read_text().strip()})
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status != 200:
            raise RuntimeError(f"the mailer answered {r.status}")


def decide(results: dict[str, Result], state: dict, now: float) -> tuple[str, str] | None:
    """What to email, if anything. A check must fail twice in a row before it is news."""
    streak = state.setdefault("streak", {})
    alerted = state.setdefault("alerted", {})
    for name, r in results.items():
        streak[name] = streak.get(name, 0) + 1 if r.state == "fail" else 0
    for name in list(streak):
        if name not in results:
            del streak[name]
    broke = [n for n, r in results.items() if r.state == "fail" and streak[n] >= 2 and n not in alerted]
    back = [n for n in list(alerted) if n not in results or results[n].state != "fail"]
    still = [n for n in alerted if n not in back]
    remind = still and now - state.get("last_mail", 0) > REMIND
    for n in back:
        del alerted[n]
    for n in broke:
        alerted[n] = now
    if not (broke or back or remind):
        return None
    failing = [n for n, r in results.items() if r.state == "fail" and n in alerted]
    if failing:
        subject = f"[boulder health] {len(failing)} problem{'s' if len(failing) > 1 else ''}: {', '.join(failing)[:120]}"
    else:
        subject = "[boulder health] all clear again"
    lines = []
    if broke:
        lines += ["NEW PROBLEMS"] + [f"- {n}: {results[n].detail}" for n in broke] + [""]
    if [n for n in failing if n not in broke]:
        lines += ["STILL FAILING"] + [f"- {n}: {results[n].detail} (since {time.strftime('%b %d %H:%M', time.localtime(alerted[n]))})"
                                      for n in failing if n not in broke] + [""]
    if back:
        lines += ["WORKING AGAIN"] + [f"- {n}" + (f": {results[n].detail}" if n in results and results[n].detail else "") for n in back] + [""]
    lines += ["Every check: " + str(STATE)]
    state["last_mail"] = now
    return subject, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", help="print the checks; no email, nothing saved")
    args = ap.parse_args()
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    now = time.time()
    results = run_all(state if not args.print else dict(state))
    if args.print:
        for name, r in results.items():
            print(f"{r.state.upper():5} {name}: {r.detail}")
        return 0 if all(r.state != "fail" for r in results.values()) else 1
    message = decide(results, state, now)
    if message:
        try:
            mail(*message)
            state["mail_error"] = ""
        except Exception as e:
            state["mail_error"] = f"{type(e).__name__}: {e}"
    state.update({"at": now, "checks": {n: {"state": r.state, "detail": r.detail} for n, r in results.items()}})
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
