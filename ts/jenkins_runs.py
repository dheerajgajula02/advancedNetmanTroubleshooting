"""Running each rule through its Jenkins pipeline, with this process doing the steps.

The troubleshooter still decides what to look at and which rules apply. For each rule it starts
the rule's pipeline (ts/pipelines.py) and serves that build: each stage asks this process, over
a local address and a one-time key, to run one step here, with the lab access, change lock and
scrubbing it already has. The build prints what each step saw and decides where the rule goes.

If Jenkins cannot take the build within a minute, or the build breaks before the rule ends, the
rule runs here instead and the run says so. A repair whose build stops after the change is
undone here, and the change lock let go, whether or not the build says goodbye.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ts import engine, fixer, jenkins_api, lab, pipelines, scrub

START_WITHIN = 60     # seconds Jenkins has to start a build before the rule runs here
RUN_FOR = 1800        # longest a build may take
STALE = 180           # a repair whose build has said nothing this long is undone here
KEEP_SESSIONS = 7200

_SESSIONS: dict[str, "Session"] = {}
_LOCK = threading.Lock()
_SERVER: ThreadingHTTPServer | None = None
_URL = ""
_LOG = print


class Session:
    """One build being served: a diagnose rule's Runner, or a fix rule's Repair."""

    def __init__(self, kind: str, rule, pipeline: str):
        self.id, self.key = secrets.token_hex(8), secrets.token_urlsafe(24)
        self.kind, self.rule, self.pipeline = kind, rule, pipeline
        self.created = self.last = time.time()
        self.calls = threading.Lock()  # one stage at a time
        self.done = threading.Event()
        self.number: int | None = None
        self.runner: engine.Runner | None = None
        self.repair: fixer.Repair | None = None
        self.expect = "step/1" if kind == "diagnose" else "check"
        self.record: dict | None = None
        self.holder: _LockHolder | None = None
        self.change_tried = False
        self.closing = False  # the rule has ended; done is set once the build has been told
        self.result = ""
        self.saw = ""

    def info(self, ran: str, **extra) -> dict:
        out = {"ran": ran, "pipeline": self.pipeline, "folder": jenkins_api.FOLDER, "pipeline_url": jenkins_api.link(self.pipeline)}
        if self.number:
            out.update(number=self.number, url=jenkins_api.link(self.pipeline, self.number), result=self.result)
        return {**out, **extra}


class _LockHolder:
    """Holds the network change lock in a thread of its own, across the stages of one build."""

    def __init__(self):
        self._release, self._ready = threading.Event(), threading.Event()
        self._error: Exception | None = None
        self._thread: threading.Thread | None = None

    def acquire(self) -> None:
        def hold():
            try:
                with lab.change_lock():
                    self._ready.set()
                    self._release.wait()
            except lab.LockBusy as exc:
                self._error = exc
                self._ready.set()
        self._thread = threading.Thread(target=hold, daemon=True)
        self._thread.start()
        self._ready.wait(15)
        if self._error:
            raise self._error

    def release(self) -> None:
        self._release.set()
        if self._thread:
            self._thread.join(10)


# --- what each stage asks for --------------------------------------------------------

def _answer(outcome: str, nxt: str, lines: list[str]) -> str:
    return scrub.text(f"{outcome}\n{nxt}\n" + "\n".join(lines).rstrip() + "\n")


def _words(token: str) -> str:
    return f"step {token[5:]}" if token.startswith("step/") else {"end": "the rule's end", "cause": "a cause", "stop": "stop", "escalate": "escalate to a person", "goto": "another rule"}.get(token, token)


def _evidence(items: list[dict]) -> list[str]:
    out = []
    for e in items:
        out.append(f"  - {e.get('kind', '')}: {e.get('ran', '')}")
        text = str(e.get("output") or e.get("note") or "").strip()
        if text:
            lines = text.splitlines()
            out += [f"      {line}" for line in lines[:40]] + ([f"      … {len(lines) - 40} more lines"] if len(lines) > 40 else [])
    return out


def _step_lines(rec: dict, token: str) -> list[str]:
    args = " ".join(f"{k}={v}" if not isinstance(v, list) else f"{k}={','.join(map(str, v))}" for k, v in (rec.get("ran_with") or {}).items())
    return [f"Question: {rec.get('ask')}", f"Ran: {rec.get('check', '').split(' ')[0]} {args}".rstrip(), f"Answer: {rec.get('outcome')}", f"Saw: {rec.get('saw')}",
            *(["Evidence:", *_evidence(rec.get("evidence") or [])] if rec.get("evidence") else []), f"Goes to: {_words(token)}", f"Took {rec.get('ms', 0)} ms."]


def _result_words(result: dict) -> str:
    kind = result.get("kind")
    if kind == "cause":
        return f"Cause {result.get('label')}"
    return f"{str(kind).capitalize()}: {result.get('text') or ''}"[:200]


def _diagnose(s: Session, action: str) -> tuple[int, str]:
    if action.startswith("step/"):
        if action != s.expect:
            return 409, f"The rule is at {s.expect}, not {action}."
        rec, token = s.runner.step(int(action[5:]) - 1)
        s.expect = token if token.startswith("step/") else f"finish/{token}"
        return 200, _answer(rec["outcome"], token, _step_lines(rec, token))
    if action.startswith("finish/"):
        if action != s.expect:
            return 409, f"The rule is at {s.expect}, not {action}."
        s.record = s.runner.finish()
        s.expect = "done"
        s.closing = True
        r = s.record["result"]
        return 200, _answer(r["kind"], _result_words(r), [f"Result: {r['kind']}", *([f"Cause: {r.get('label')}"] if r.get("label") else []), f"{r.get('text') or ''}"])
    return 404, f"No such stage: {action}"


def _repair_lines(repair: fixer.Repair, start: int) -> list[str]:
    out = []
    for st in repair.steps[start:]:
        out += [f"{st.get('kind')}: {st.get('ask')}", f"  Ran: {st.get('check')}", f"  Answer: {st.get('outcome')}", f"  Saw: {st.get('saw')}", *_evidence(st.get("evidence") or [])]
    return out


def _ended(s: Session, before: int) -> tuple[int, str]:
    done = s.repair.done
    return 200, _answer("done", f"{done['status']}: {done.get('why') or ''}"[:200], [*_repair_lines(s.repair, before), f"Repair: {done['status']}. {done.get('why') or ''}"])


def _fix(s: Session, action: str) -> tuple[int, str]:
    action = action.removeprefix("fix/")
    rp, before = s.repair, len(s.repair.steps)
    if action == "abandon":
        _abandon(s, "The Jenkins build stopped part-way.")
        return 200, _answer("abandoned", "Stopped part-way", ["The repair was stopped part-way and anything it changed was undone."])
    if action != s.expect and not (action == "finish" and rp.done):
        return 409, f"The repair is at {s.expect}, not {action}."
    if action == "check":
        if rp.check():
            s.expect = "finish"
            return _ended(s, before)
        s.expect = "change"
        return 200, _answer("go", "Safe to change", [*_repair_lines(rp, before), "Every guard and requirement holds, and the first look found it working before. Going ahead."])
    if action == "change":
        s.change_tried = True
        try:
            s.holder = _LockHolder()
            s.holder.acquire()
        except lab.LockBusy:
            s.holder = None
            rp.finish("held", "Another job held the network change lock, so I left it alone.", expected=True)
            s.expect = "finish"
            return _ended(s, before)
        if rp.change():
            s.expect = "finish"
            return _ended(s, before)
        s.expect = "verify"
        return 200, _answer("changed", "Changed", [*_repair_lines(rp, before), "Holding the change lock until this build ends."])
    if action == "verify":
        state, s.saw = rp.verify()
        s.expect = {"ok": "settle", "gave_up": "undo"}.get(state, "verify")
        lines = _repair_lines(rp, before) or [f"Not yet: {s.saw}"]
        return 200, _answer(state, {"ok": "It worked", "gave_up": "It did not take effect", "not_yet": "Not yet"}[state], lines)
    if action == "settle":
        rp.settle()
        s.expect = "finish"
        return _ended(s, before)
    if action == "undo":
        rp.undo(f"It did not take effect: {s.saw}")
        s.expect = "finish"
        return _ended(s, before)
    if action == "finish":
        _let_go(s)
        s.expect = "done"
        s.closing = True
        done = rp.done
        return 200, _answer(done["status"], f"{done['status']}: {done.get('why') or ''}"[:200], [f"Repair: {done['status']}. {done.get('why') or ''}"])
    return 404, f"No such stage: {action}"


def _let_go(s: Session) -> None:
    if s.holder:
        s.holder.release()
        s.holder = None


def _abandon(s: Session, why: str) -> None:
    """Leave nothing half-done: undo a change that was not kept, let go of the lock, close the repair."""
    rp = s.repair
    if rp is not None:
        if rp.changed:
            rp.undo(f"{why} The change was undone.")
        elif rp.done is None:
            rp.finish("held" if not s.change_tried else "failed", why)
    _let_go(s)
    s.expect = "done"
    s.done.set()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        parts = self.path.split("?")[0].strip("/").split("/", 2)
        if len(parts) < 3 or parts[0] != "jenkins":
            return self._send(404, "Not here.")
        s = _SESSIONS.get(parts[1])
        if s is None:
            return self._send(404, "That build is not being served.")
        if not hmac.compare_digest(self.headers.get("X-TS-Key", ""), s.key):
            return self._send(403, "Wrong key.")
        with s.calls:
            s.last = time.time()
            if s.expect == "done":
                return self._send(409, "This build's rule has already ended.")
            try:
                code, text = (_diagnose if s.kind == "diagnose" else _fix)(s, parts[2])
            except Exception as exc:  # reported to the build, which stops; the caller falls back
                code, text = 500, f"The troubleshooter failed on this stage: {type(exc).__name__}: {exc}"
            s.last = time.time()
        self._send(code, text)
        if s.closing:
            s.done.set()

    def _send(self, code: int, text: str) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


def _serve() -> str:
    global _SERVER, _URL
    with _LOCK:
        if _SERVER is None:
            _SERVER = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            _SERVER.daemon_threads = True
            threading.Thread(target=_SERVER.serve_forever, daemon=True).start()
            _URL = f"http://127.0.0.1:{_SERVER.server_address[1]}"
            threading.Thread(target=_reap_forever, daemon=True).start()
    return _URL


def _reap_forever() -> None:
    while True:
        time.sleep(30)
        _reap_once()


def _reap_once() -> None:
    """Undo a repair whose build went quiet holding the lock, and forget old builds."""
    now = time.time()
    for sid, s in list(_SESSIONS.items()):
        try:
            if s.kind == "fix" and not s.done.is_set() and s.holder and now - s.last > STALE and s.calls.acquire(blocking=False):
                try:  # a stage still running is not a quiet build, however long it takes
                    if not s.done.is_set() and s.holder and time.time() - s.last > STALE:
                        _LOG(f"Jenkins build {s.pipeline} #{s.number} went quiet holding the change lock; undoing here")
                        _abandon(s, f"The Jenkins build said nothing for {STALE} s.")
                finally:
                    s.calls.release()
            if now - s.created > KEEP_SESSIONS:
                _SESSIONS.pop(sid, None)
        except Exception as exc:  # the reaper must keep going
            _LOG(f"reaping {sid} failed: {type(exc).__name__}: {exc}")


# --- starting a build and waiting for it -----------------------------------------------

def _drive(s: Session, run_id: str) -> tuple[bool, str]:
    """Start the build and wait for its rule to end. (True, "") or (False, why it did not)."""
    _SESSIONS[s.id] = s
    try:
        if jenkins_api.free_executors() < 1:  # long test jobs can hold them all; do not wait a minute for nothing
            return False, "every Jenkins executor was busy"
        queue_url = jenkins_api.start(s.pipeline, {"RUN": run_id, "TS_URL": _serve(), "TS_SESSION": s.id, "TS_KEY": s.key})
        number, why = jenkins_api.wait_started(queue_url, START_WITHIN)
    except jenkins_api.Unavailable as exc:
        return False, str(exc)
    if not number:
        jenkins_api.cancel(queue_url)
        return False, why
    s.number = number
    deadline = time.time() + RUN_FOR
    while not s.done.wait(3):
        try:
            b = jenkins_api.build(s.pipeline, number)
        except jenkins_api.Unavailable:
            b = None
        if b and not b.get("building") and not s.done.wait(3):
            return False, f"build #{number} ended {b.get('result') or 'without a result'} before the rule did"
        if time.time() > deadline:
            jenkins_api.stop(s.pipeline, number)
            return False, f"build #{number} took longer than {RUN_FOR // 60} minutes"
    for _ in range(15):  # the build finishes its last stage after the rule has ended
        try:
            b = jenkins_api.build(s.pipeline, number)
        except jenkins_api.Unavailable:
            break
        if b and not b.get("building"):
            s.result = b.get("result") or ""
            break
        time.sleep(1)
    return True, ""


def _resync_later(why: str) -> None:
    """A build that broke may mean the pipeline is older than its rule."""
    from ts.ruleset import load_ruleset

    def work():
        try:
            pipelines.sync(load_ruleset().parsed, _LOG)
        except Exception as exc:  # the next start tries again
            _LOG(f"Jenkins pipelines could not be brought up to date: {type(exc).__name__}: {exc}")
    if "older than its rule" in why or "404" in why:
        threading.Thread(target=work, daemon=True).start()


def run_rule(rule, event: dict) -> dict:
    """engine.RUN_RULE: run a diagnose rule in its pipeline, or here if Jenkins cannot."""
    s = Session("diagnose", rule, pipelines.name_of(rule))
    s.runner = engine.Runner(rule, event)
    ok, why = _drive(s, engine.incident_id(event))
    if ok:
        record = s.record
        record["jenkins"] = s.info("jenkins")
        return record
    _LOG(f"{engine.incident_id(event)}: {rule.id} ran here, not in Jenkins: {why}")
    _resync_later(why)
    record = engine.run_here(rule, event)
    record["jenkins"] = s.info("here", why=why)
    return record


def apply(record: dict, rule, values: dict, log=print) -> dict:
    """fixer.APPLY: apply a fix rule in its pipeline, or here if Jenkins never got to the change."""
    s = Session("fix", rule, pipelines.name_of(rule))
    s.repair = fixer.Repair(record, rule, values, log)
    ok, why = _drive(s, record.get("id") or engine.incident_id(record["event"]))
    if ok:
        return {**s.repair.done, "jenkins": s.info("jenkins")}
    with s.calls:
        if s.change_tried:  # it may have touched the network: finish it here, never run it twice
            if not s.done.is_set():
                _abandon(s, f"The Jenkins build broke part-way: {why}")
            return {**s.repair.done, "jenkins": s.info("jenkins", broke=why)}
        s.expect = "done"
        s.done.set()
    log(f"{record.get('id')}: {rule.id} repair ran here, not in Jenkins: {why}")
    _resync_later(why)
    return {**fixer.run_here(record, rule, values, log), "jenkins": s.info("here", why=why)}


ENABLED = False


def enable(log=print) -> None:
    """Send every rule and repair through Jenkins from now on, in this process."""
    global ENABLED, _LOG
    _LOG = log
    engine.RUN_RULE = run_rule
    fixer.APPLY = apply
    ENABLED = True
    _serve()
