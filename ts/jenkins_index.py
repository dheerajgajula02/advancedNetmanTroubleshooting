"""Every Jenkins build of this network's jobs, kept searchable for the AI.

What ran, when, how it ended, who or what started it, and what its console printed: the test and
deployment jobs, the housekeeping jobs, and the troubleshooting-pipelines builds of the rules
themselves. Read from the build records on disk, like ts/sources/jenkins.py, and kept in a database
of its own, so thousands of once-a-minute builds never crowd the memory a run's brief is drawn from.

A console is scrubbed like everything else the debugging side sees, and anything that looks like a
password, token or key is hidden before it is stored or shown. A job that runs dozens of times a day
keeps only a line for each build that passed; read_build still reads any build whole from disk.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from ts import history, scrub
from ts.sources import jenkins

DB = Path(os.environ.get("TS_JENKINS_INDEX", Path(__file__).resolve().parent.parent / "data" / "jenkins.sqlite"))
FIRST_LOOK_BACK = 14 * 86400
FIRST_KEEP = 5              # builds of each job indexed the first time, however old
MAX_PER_PASS = 600         # builds of one job read in one pass, newest first
FREQUENT = 48               # a job that runs more often than this a day is housekeeping
KEEP_FREQUENT_DAYS = 14     # how long a housekeeping build that passed stays listed
KEEP_DAYS = 180
HEAD, TAIL = 1500, 3500     # how much of a console is kept for searching
PAGE = 6000                 # how much of a console one read_build answer holds
READ_AT_MOST = 5_000_000

_HIDDEN = "[hidden]"
_SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[a private key, hidden]"),
    (re.compile(r"(?i)\b(authorization:\s*(?:basic|bearer|token)?)\s*\S+"), rf"\1 {_HIDDEN}"),
    (re.compile(r"(?i)(\w+://[^/\s:@]+):[^@\s/]+@"), rf"\1:{_HIDDEN}@"),
    (re.compile(r"(?i)\b(sshpass\s+-p)\s*\S+"), rf"\1 {_HIDDEN}"),
    (re.compile(r"(?i)(\s-u\s*['\"]?[^\s:'\"]+):[^\s'\"]+"), rf"\1:{_HIDDEN}"),
    (re.compile(r"(?i)(--?(?:password|passwd|pass|pwd|token|secret|api-?key|community)(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)"), rf"\1{_HIDDEN}"),
    (re.compile(r"(?i)\b((?:password|passwd|secret|token|community)\s+)(\"[^\"]*\"|'[^']*')"), rf"\1{_HIDDEN}"),
    (re.compile(r"(?i)\b([\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|community)[\w.-]*[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}]+)"), rf"\1{_HIDDEN}"),
    (re.compile(r"(?i)\b([\w.-]*[_-]key[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}]+)"), rf"\1{_HIDDEN}"),
    (re.compile(r"(?i)\b(login[^:\n]{0,40}:\s*[\w.@-]+\s*/\s*)\S+"), rf"\1{_HIDDEN}"),
    (re.compile(r"(?i)\b(snmp-server\s+community\s+)\S+"), rf"\1{_HIDDEN}"),
    (re.compile(r"(?i)\b(snmp(?:walk|get|getnext|bulkwalk|bulkget|set)\b[^\n]*?\s-c\s*)\S+"), rf"\1{_HIDDEN}"),
    (re.compile(r"(?i)\b(secret\s+\d\s+)\S+"), rf"\1{_HIDDEN}"),              # "username admin secret 0 ..." in device config
    (re.compile(r"\{AQAAAB[A-Za-z0-9+/=]+\}"), _HIDDEN),                         # a Jenkins-encrypted value
    (re.compile(r"\b[0-9a-fA-F]{32,39}\b|\b[0-9a-fA-F]{41,}\b"), _HIDDEN),       # API tokens; a 40-character git commit stays
    (re.compile(r"(?<![\w/.-])(?=[A-Za-z0-9+_-]*\d)(?=[A-Za-z0-9+_-]*[A-Z])(?=[A-Za-z0-9+_-]*[a-z])[A-Za-z0-9+_-]{40,}={0,2}(?![\w/.])"), _HIDDEN),  # not paths or addresses
]
_STAGE = re.compile(r"^\[Pipeline\] \{ \((.+)\)$")
_CAUSES = [
    (re.compile(r"TimerTriggerCause"), "started by its timer"),
    (re.compile(r"SCMTriggerCause|BranchEventCause|GitHubPushCause|BranchIndexingCause"), "started by a change in git"),
    (re.compile(r"RemoteCause|GenericCause"), "started by another system"),
]
_UPSTREAM = re.compile(r"<upstreamProject>([^<]+)</upstreamProject>\s*<upstreamUrl>[^<]*</upstreamUrl>\s*<upstreamBuild>(\d+)</upstreamBuild>")
_USER = re.compile(r"<userId>([^<]+)</userId>")
_PARAM = re.compile(r"<hudson\.model\.(String|Boolean|Choice|Text)ParameterValue>\s*<name>([^<]+)</name>\s*<value>([^<]*)</value>", re.S)
_SECRET_NAME = re.compile(r"(?i)pass|secret|token|key|cred|community")


def hide_secrets(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def clean(raw: str) -> str:
    """A console as the AI may see it: no Jenkins markup, scrubbed, secrets hidden."""
    out = []
    for line in raw.splitlines():
        line = jenkins._NOTE.sub("", line).rstrip()
        if line.startswith("[Pipeline] "):
            stage = _STAGE.match(line)
            if stage:
                out.append(f"== {stage.group(1)} ==")
            continue
        if line:
            out.append(line)
    return hide_secrets(scrub.text("\n".join(out)))


def _started_by(xml: str) -> str:
    up = _UPSTREAM.search(xml)
    if up:
        return f"started by {up.group(1)} #{up.group(2)}"
    for pattern, words in _CAUSES:
        if pattern.search(xml):
            return words
    user = _USER.search(xml)
    return f"started by {user.group(1)}" if user else ""


def _params(xml: str) -> str:
    return ", ".join(f"{name}={_HIDDEN if _SECRET_NAME.search(name) else hide_secrets(scrub.text(value))[:120]}" for _kind, name, value in _PARAM.findall(xml))


def _console(folder: Path) -> str:
    try:
        with (folder / "log").open("rb") as handle:
            raw = handle.read(READ_AT_MOST).decode(errors="replace")
    except OSError:
        return ""
    return clean(raw)


def _url(job: str, number: int) -> str:
    return f"{jenkins_link()}/job/{'/job/'.join(job.split('/'))}/{number}/"


def jenkins_link() -> str:
    from ts.jenkins_api import LINK_BASE
    return LINK_BASE


@contextmanager
def _open(path: Path | None = None):
    db = sqlite3.connect(path or DB, timeout=30)
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS builds (key TEXT PRIMARY KEY, job TEXT, number INTEGER, start REAL, seconds REAL, result TEXT,
                                               started_by TEXT, params TEXT, text TEXT, url TEXT, frequent INTEGER);
            CREATE INDEX IF NOT EXISTS builds_job ON builds (job, number);
            CREATE VIRTUAL TABLE IF NOT EXISTS builds_fts USING fts5(key UNINDEXED, job, text);
            CREATE TABLE IF NOT EXISTS jobs (job TEXT PRIMARY KEY, indexed_to INTEGER, frequent INTEGER);
        """)
        yield db
        db.commit()
    finally:
        db.close()


def _runs_a_day(folder: Path, last: int) -> float:
    newest, older = jenkins._build(folder, last), jenkins._build(folder, last - FREQUENT)
    if not newest or not older:
        return 0.0
    return FREQUENT / max((newest["start"] - older["start"]) / 86400, 1e-6)


def _summary(job: str, b: dict, started_by: str, params: str) -> str:
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(b["start"]))
    changes = " It changes the network or tests it by changing it." if jenkins.CHANGES_NETWORK.search(job) and not job.startswith(tuple(jenkins.OWN)) else ""
    own = " A troubleshooting rule run as a pipeline." if job.startswith(tuple(jenkins.OWN)) else ""
    return (f"Job {job} #{b['number']}: {b['result'] or 'RUNNING'}. Started {when}, ran {b['seconds']:.0f} s"
            + (f", {started_by}" if started_by else "") + "." + (f" Parameters: {params}." if params else "") + changes + own)


def _entry(job: str, b: dict, frequent: bool) -> dict:
    try:
        xml = (b["folder"] / "build.xml").read_text(errors="replace")
    except OSError:
        xml = ""
    started_by, params = _started_by(xml), _params(xml)
    text = _summary(job, b, started_by, params)
    if not (frequent and b["result"] == "SUCCESS"):
        console = _console(b["folder"])
        if console:
            text += "\n" + (console if len(console) <= HEAD + TAIL else f"{console[:HEAD]}\n[... {len(console) - HEAD - TAIL} characters left out; read_build reads it all ...]\n{console[-TAIL:]}")
    return {"key": f"{job}#{b['number']}", "job": job, "number": b["number"], "start": b["start"], "seconds": b["seconds"], "result": b["result"],
            "started_by": started_by, "params": params, "text": text, "url": _url(job, b["number"]), "frequent": int(frequent)}


def keep_up(log=lambda _m: None, path: Path | None = None, now: float | None = None) -> dict:
    """Index whatever builds finished since the last pass. Returns how many each job added."""
    now = now or time.time()
    added: dict[str, int] = {}
    with _open(path) as db:
        done = {job: to for job, to in db.execute("SELECT job, indexed_to FROM jobs")}
        for job, folder in jenkins._jobs(own=True):
            try:
                last = int((folder / "nextBuildNumber").read_text().strip()) - 1
            except (OSError, ValueError):
                continue
            first_time, since = job not in done, done.get(job) or 0
            if last <= since:
                continue
            frequent = _runs_a_day(folder, last) > FREQUENT
            reached, running, seen = since, None, 0
            for number in range(last, max(since, last - MAX_PER_PASS), -1):
                b = jenkins._build(folder, number)
                if b is None:
                    continue
                seen += 1
                if first_time and b["start"] < now - FIRST_LOOK_BACK and seen > FIRST_KEEP:  # a job idle for weeks still shows its last builds
                    break
                if b["result"] is None:  # still running: comes back next pass
                    running = number
                    continue
                if db.execute("SELECT 1 FROM builds WHERE key = ?", (f"{job}#{number}",)).fetchone():
                    continue
                e = _entry(job, b, frequent)
                db.execute("INSERT INTO builds VALUES (:key,:job,:number,:start,:seconds,:result,:started_by,:params,:text,:url,:frequent)", e)
                db.execute("INSERT INTO builds_fts VALUES (?,?,?)", (e["key"], job, e["text"]))
                added[job] = added.get(job, 0) + 1
            reached = (running - 1) if running else last
            db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (job, max(reached, since), int(frequent)))
        old = db.execute("SELECT key FROM builds WHERE start < ? OR (frequent = 1 AND result = 'SUCCESS' AND start < ?)",
                         (now - KEEP_DAYS * 86400, now - KEEP_FREQUENT_DAYS * 86400)).fetchall()
        for (key,) in old:
            db.execute("DELETE FROM builds WHERE key = ?", (key,))
            db.execute("DELETE FROM builds_fts WHERE key = ?", (key,))
    if added:
        log(f"Jenkins index: {sum(added.values())} new builds from {len(added)} jobs")
    return added


def _row(r) -> dict:
    keys = ("job", "number", "start", "seconds", "result", "started_by", "url", "text")
    return dict(zip(keys, r))


def search(text: str = "", job: str = "", result: str = "", k: int = 12, path: Path | None = None) -> list[dict]:
    """Builds whose job, parameters or console match, newest first among the best matches. No text: the latest builds."""
    where, args = [], []
    if job:
        where.append("b.job LIKE ?")
        args.append(f"%{job}%")
    if result:
        where.append("b.result = ?")
        args.append(result.upper())
    words = history.terms(text) if text else []
    with _open(path) as db:
        if words:
            sql = (f"SELECT b.job, b.number, b.start, b.seconds, b.result, b.started_by, b.url, snippet(builds_fts, 2, '', '', ' … ', 24) "
                   f"FROM builds_fts f JOIN builds b ON b.key = f.key WHERE builds_fts MATCH ? {''.join(' AND ' + w for w in where)} "
                   f"ORDER BY bm25(builds_fts), b.start DESC LIMIT ?")
            rows = db.execute(sql, [history._match(words), *args, k]).fetchall()
        else:
            sql = (f"SELECT job, number, start, seconds, result, started_by, url, substr(text, 1, 300) FROM builds b "
                   f"{'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY start DESC LIMIT ?")
            rows = db.execute(sql, [*args, k]).fetchall()
    return [_row(r) for r in rows]


def jobs(path: Path | None = None) -> list[dict]:
    """Every job: how often it runs, and its latest build."""
    with _open(path) as db:
        rows = db.execute("""SELECT j.job, j.frequent, b.number, b.result, b.start FROM jobs j
                             LEFT JOIN builds b ON b.key = (SELECT key FROM builds WHERE job = j.job ORDER BY number DESC LIMIT 1) ORDER BY j.job""").fetchall()
    return [{"job": r[0], "frequent": bool(r[1]), "last": r[2], "result": r[3], "start": r[4]} for r in rows]


def read(job: str, number: int, part: int = 1) -> dict | None:
    """One build whole, from disk: what it was and a page of its console. None if there is no such build."""
    folder = dict(jenkins._jobs(own=True)).get(job)
    if folder is None:
        return None
    b = jenkins._build(folder, int(number))
    if b is None:
        return None
    try:
        xml = (b["folder"] / "build.xml").read_text(errors="replace")
    except OSError:
        xml = ""
    console = _console(b["folder"])
    pages = max(1, -(-len(console) // PAGE))
    part = min(max(1, int(part)), pages)
    return {"job": job, "number": b["number"], "url": _url(job, b["number"]), "summary": _summary(job, b, _started_by(xml), _params(xml)),
            "part": part, "parts": pages, "console": console[(part - 1) * PAGE: part * PAGE]}


def of_run(record: dict) -> list[str]:
    """The builds this run's rules and repair ran as, one line each, for the AI's brief."""
    found = [(record["rule"].get("title") or record["rule"]["id"], record.get("jenkins"))]
    found += [(t.get("title") or t["rule"], t.get("jenkins")) for t in record.get("tried") or []]
    repair = record.get("repair") or {}
    found.append((f"the repair, {repair.get('title') or repair.get('rule')}", repair.get("jenkins")))
    lines = []
    for what, j in found:
        if not j:
            continue
        if j.get("ran") == "here":
            lines.append(f"- {what}: Jenkins could not run it, so it ran here ({j.get('why')})" + (f"; the build that broke was {j['pipeline']} #{j['number']}" if j.get("number") else ""))
        else:
            lines.append(f"- {what}: {j.get('folder') or 'troubleshooting-pipelines'}/{j['pipeline']} #{j.get('number')}, {j.get('result') or 'finished'}" + (f", broke part-way and was finished here ({j['broke']})" if j.get("broke") else ""))
    return lines
