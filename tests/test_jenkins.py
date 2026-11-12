"""The rules as Jenkins pipelines: what is generated, and the troubleshooter serving a build.

Jenkins itself is faked: a thread plays the pipeline, asking the troubleshooter's step server for each
stage over HTTP exactly as the Groovy does with curl, so the protocol is tested end to end.
"""

import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager

import pytest

from tests.conftest import REPO, diag, fix, rules
from ts import engine, fixer, jenkins_api, jenkins_runs, lab, pipelines
from ts.lab import Result
from ts.lint import errors, lint
from ts.registry import ACTIONS, CHECKS
from ts.ruleset import load_ruleset

EVENT = {"event": "interface.down", "device": "s1", "interface": "Ethernet1", "subject": "Ethernet1", "time": 1_800_000_000.0, "platform": "eos", "role": "access"}
THREE = diag("three-steps", mode="active", causes={"interface.no_link": "No link.", "interface.admin_shut": "Shut."}, steps=[
    {"id": "first", "check": "interface_status {device} {interface}", "then": {"admin_down": "cause interface.admin_shut", "up": "jump third"}},
    {"id": "second", "check": "ping {device} 10.0.0.1", "then": {"unreachable": "cause interface.no_link"}},
    {"id": "third", "check": "ping {device} 10.0.0.2", "then": {"reachable": "stop It works now."}},
])


# --- what is generated ---------------------------------------------------------------

def test_every_installed_rule_becomes_a_valid_pipeline():
    wanted = pipelines.wanted(load_ruleset(REPO / "rules").parsed)
    assert len(wanted) >= 30
    for name, rule in wanted.items():
        xml, print_ = pipelines.config_xml(rule)
        root = ET.fromstring(xml.encode())
        script = root.find("definition/script").text
        assert script.startswith(f"// fingerprint {print_}") and pipelines.config_xml(rule)[1] == print_, name
        assert root.find("disabled").text == ("true" if rule.mode == "retired" else "false")
        if rule.table == "diagnose":
            assert script.count("exits:") == len(rule.steps), name


def test_the_pipeline_routes_every_answer_where_the_engine_does(stubbed):
    rule = rules(THREE)[0]
    table = pipelines.flow(rule)
    assert [r["exits"] for r in table] == [{"admin_down": "cause", "up": "step/3"}, {"unreachable": "cause"}, {"reachable": "stop"}]
    assert [r["otherwise"] for r in table] == ["step/2", "step/3", "end"]
    for status, ping, path in [("up", "reachable", ["step/3", "stop"]), ("down", "reachable", ["step/2", "step/3", "stop"]), ("down", "unreachable", ["step/2", "cause"])]:
        stubbed(interface_status=status, ping=ping)
        runner, at, went = engine.Runner(rule, EVENT), "step/1", []
        while at.startswith("step/"):
            n = int(at[5:])
            rec, token = runner.step(n - 1)
            assert table[n - 1]["exits"].get(rec["outcome"], table[n - 1]["otherwise"]) == token  # what the Groovy would pick
            went.append(token)
            at = token
        assert went == path


def test_two_rules_may_not_share_a_pipeline():
    found = errors(lint(rules(diag("one-rule", pipeline="shared-name"), diag("two-rule", pipeline="shared-name"))))
    assert any("already belongs to the rule" in i.message for i in found)
    assert pipelines.clash(rules(diag("one-rule", pipeline="shared-name")), "shared-name", "two-rule")
    assert pipelines.clash(rules(diag("one-rule")), "one-rule", "one-rule") is None


def test_proposed_rules_get_no_pipeline_until_accepted():
    assert list(pipelines.wanted(rules(diag("not-yet", mode="proposed"), diag("live-one", mode="active")))) == ["live-one"]


# --- serving a build ----------------------------------------------------------------

@pytest.fixture
def stubbed(monkeypatch):
    saved = {n: CHECKS[n].func for n in ("interface_status", "ping", "change_window_open")} | {"config_lines": ACTIONS["config_lines"].func}

    def stub(**outcomes):
        for name, outcome in outcomes.items():
            block = CHECKS.get(name) or ACTIONS[name]
            object.__setattr__(block, "func", outcome if callable(outcome) else (lambda o: lambda ctx, **kw: Result(o, f"it said {o}"))(outcome))
    yield stub
    for name, func in saved.items():
        object.__setattr__(CHECKS.get(name) or ACTIONS[name], "func", func)


class FakeJenkins:
    """Plays one pipeline per build in a thread, asking the step server like the Groovy does."""

    def __init__(self, monkeypatch, play):
        self.play, self.stages, self.result, self.ended = play, [], None, threading.Event()
        monkeypatch.setattr(jenkins_api, "free_executors", lambda: 4)
        monkeypatch.setattr(jenkins_api, "start", self.start)
        monkeypatch.setattr(jenkins_api, "wait_started", lambda queue_url, within: (7, ""))
        monkeypatch.setattr(jenkins_api, "build", lambda name, number: {"building": not self.ended.is_set(), "result": self.result})

    def start(self, name, params):
        self.name = name

        def build():
            try:
                self.play(self, params)
                self.result = "SUCCESS"
            except Exception:  # a stage the troubleshooter refused, as curl's exit 1 fails the build
                self.result = "FAILURE"
            self.ended.set()
        threading.Thread(target=build, daemon=True).start()
        return "http://jenkins.invalid/queue/item/1/"

    def tell(self, params, what, key=None):
        req = urllib.request.Request(f"{params['TS_URL']}/jenkins/{params['TS_SESSION']}/{what}", data=b"", method="POST", headers={"X-TS-Key": key or params["TS_KEY"]})
        with urllib.request.urlopen(req, timeout=10) as answer:
            outcome, nxt, rest = (answer.read().decode() + "\n\n").split("\n", 2)
        self.stages.append(what)
        return outcome, nxt


def diagnose_pipeline(rule):
    table = pipelines.flow(rule)

    def play(fj, params):
        at = "step/1"
        while at.startswith("step/"):
            n = int(at[5:])
            outcome, nxt = fj.tell(params, f"step/{n}")
            mine = table[n - 1]["exits"].get(outcome, table[n - 1]["otherwise"])
            assert mine == nxt, "the pipeline is older than its rule"
            at = mine
        fj.tell(params, f"finish/{at}")
    return play


def test_a_rule_runs_as_a_build_and_reaches_what_it_would_here(monkeypatch, stubbed):
    stubbed(interface_status="down", ping="unreachable")
    rule = rules(THREE)[0]
    fj = FakeJenkins(monkeypatch, diagnose_pipeline(rule))
    record = jenkins_runs.run_rule(rule, EVENT)
    assert fj.stages == ["step/1", "step/2", "finish/cause"] and fj.name == "three-steps"
    assert record["result"]["label"] == "interface.no_link" == engine.run_here(rule, EVENT)["result"]["label"]
    j = record["jenkins"]
    assert j["ran"] == "jenkins" and j["number"] == 7 and j["result"] == "SUCCESS" and j["url"].endswith("/job/troubleshooting-pipelines/job/three-steps/7/")


def test_the_server_refuses_a_wrong_key_and_steps_out_of_order(monkeypatch, stubbed):
    stubbed(interface_status="down", ping="unreachable")
    rule, seen = rules(THREE)[0], {}

    def play(fj, params):
        for what, key in [("step/1", "not-the-key"), ("step/2", None)]:
            try:
                fj.tell(params, what, key)
            except urllib.error.HTTPError as exc:
                seen[what] = exc.code
        raise RuntimeError("the build stops")
    FakeJenkins(monkeypatch, play)
    record = jenkins_runs.run_rule(rule, EVENT)
    assert seen == {"step/1": 403, "step/2": 409}
    assert record["jenkins"]["ran"] == "here" and "FAILURE" in record["jenkins"]["why"] and record["result"]["label"] == "interface.no_link"


def test_a_rule_runs_here_when_jenkins_is_away(monkeypatch, stubbed):
    stubbed(interface_status="admin_down")

    def away(*_a, **_k):
        raise jenkins_api.Unavailable("Jenkins did not answer: connection refused")
    monkeypatch.setattr(jenkins_api, "free_executors", away)
    record = jenkins_runs.run_rule(rules(THREE)[0], EVENT)
    assert record["result"]["label"] == "interface.admin_shut"
    assert record["jenkins"]["ran"] == "here" and "did not answer" in record["jenkins"]["why"] and record["jenkins"]["pipeline_url"].endswith("/three-steps/")


def test_a_rule_runs_here_at_once_when_every_executor_is_busy(monkeypatch, stubbed):
    stubbed(interface_status="admin_down")
    monkeypatch.setattr(jenkins_api, "free_executors", lambda: 0)
    monkeypatch.setattr(jenkins_api, "start", lambda *a: pytest.fail("nothing should be queued"))
    assert jenkins_runs.run_rule(rules(THREE)[0], EVENT)["jenkins"]["why"] == "every Jenkins executor was busy"


# --- a repair -------------------------------------------------------------------------

@pytest.fixture
def repairing(monkeypatch, stubbed):
    """A repair with every outside look stubbed: what the change did and whether the lock was held."""
    state = {"undone": 0, "locked": 0, "held_at_change": None}

    @contextmanager
    def lock():
        state["locked"] += 1
        try:
            yield
        finally:
            state["locked"] -= 1

    def change(ctx, **kw):
        state["held_at_change"] = state["locked"]
        ctx.undo.append(lambda: state.__setitem__("undone", state["undone"] + 1) or "Undid it.")
        return Result("done", "Changed it.")
    monkeypatch.setattr(lab, "change_lock", lock)
    for name, value in {"_guard": lambda *a: None, "_job_in_the_way": lambda: None, "_left_in_place": lambda *a: ""}.items():
        monkeypatch.setattr(fixer, name, value)
    monkeypatch.setattr(fixer.firstlook, "take", lambda *a, **k: {"working": {"verdict": "yes"}})
    monkeypatch.setattr(fixer, "SETTLE", 0)
    stubbed(change_window_open="closed", interface_status="up", config_lines=change)
    record = {"id": "a-run", "event": EVENT, "values": {"device": "s1", "interface": "Ethernet1"}, "result": {"kind": "cause", "label": "interface.admin_shut", "params": {}}}
    rule = rules(fix("put-it-back", mode="active", pipeline="port-no-shutdown"))[0]
    return state, record, rule


def fix_pipeline(stop_after_change=False, say_goodbye=True):
    def play(fj, params):
        if fj.tell(params, "fix/check")[0] != "go":
            return fj.tell(params, "fix/finish")
        if fj.tell(params, "fix/change")[0] == "changed":
            if stop_after_change:
                if say_goodbye:
                    fj.tell(params, "fix/abandon")
                raise RuntimeError("someone pressed stop")
            state = fj.tell(params, "fix/verify")[0]
            while state == "not_yet":
                state = fj.tell(params, "fix/verify")[0]
            fj.tell(params, "fix/settle" if state == "ok" else "fix/undo")
        fj.tell(params, "fix/finish")
    return play


def test_a_repair_runs_as_a_build_holding_the_lock_from_change_to_end(monkeypatch, repairing):
    state, record, rule = repairing
    fj = FakeJenkins(monkeypatch, fix_pipeline())
    done = jenkins_runs.apply(record, rule, fixer.fix_values(record), lambda *_: None)
    assert fj.stages == ["fix/check", "fix/change", "fix/verify", "fix/settle", "fix/finish"] and fj.name == "port-no-shutdown"
    assert done["status"] == "fixed" and done["jenkins"]["ran"] == "jenkins" and done["jenkins"]["result"] == "SUCCESS"
    assert state["held_at_change"] == 1 and state["locked"] == 0 and state["undone"] == 0


@pytest.mark.parametrize("goodbye", [True, False])
def test_a_build_stopped_after_the_change_is_undone_here_and_never_run_twice(monkeypatch, repairing, goodbye):
    state, record, rule = repairing
    FakeJenkins(monkeypatch, fix_pipeline(stop_after_change=True, say_goodbye=goodbye))
    done = jenkins_runs.apply(record, rule, fixer.fix_values(record), lambda *_: None)
    assert done["status"] == "failed" and state["undone"] == 1 and state["locked"] == 0
    assert done["jenkins"]["ran"] == "jenkins" and ("broke" in done["jenkins"]) != goodbye


def test_a_repair_that_never_reached_jenkins_runs_here(monkeypatch, repairing):
    state, record, rule = repairing
    monkeypatch.setattr(jenkins_api, "free_executors", lambda: 0)
    done = jenkins_runs.apply(record, rule, fixer.fix_values(record), lambda *_: None)
    assert done["status"] == "fixed" and done["jenkins"]["ran"] == "here" and state["locked"] == 0


def test_a_quiet_build_holding_the_lock_is_undone_by_the_reaper(monkeypatch, repairing):
    state, record, rule = repairing
    s = jenkins_runs.Session("fix", rule, "port-no-shutdown")
    s.repair = fixer.Repair(record, rule, fixer.fix_values(record), lambda *_: None)
    assert s.repair.check() is None
    s.expect = "change"
    assert jenkins_runs._fix(s, "fix/change")[0] == 200 and state["locked"] == 1
    s.last = time.time() - jenkins_runs.STALE - 1
    jenkins_runs._SESSIONS[s.id] = s
    try:
        with s.calls:  # a stage that is still running, however long, is not a quiet build
            jenkins_runs._reap_once()
        assert state["undone"] == 0 and state["locked"] == 1 and s.repair.done is None
        jenkins_runs._reap_once()
    finally:
        jenkins_runs._SESSIONS.pop(s.id, None)
    assert state["undone"] == 1 and state["locked"] == 0 and s.repair.done["status"] == "failed" and s.done.is_set()


# --- every build, searchable for the AI ------------------------------------------------

from ts import jenkins_index, tools  # noqa: E402
from ts.sources import jenkins as jenkins_source  # noqa: E402

NOW = 1_800_000_000.0


def _job(root, name, builds):
    """A job folder as Jenkins writes it: (number, start, result, console, extra build.xml)."""
    folder = root.joinpath(*[p for part in name.split("/") for p in (part, "jobs")][:-1])
    for parent in [root.joinpath(*[p for part in name.split("/")[:i] for p in (part, "jobs")]) for i in range(1, name.count("/") + 1)]:
        (parent.parent / parent.name).mkdir(parents=True, exist_ok=True)
    (folder / "builds").mkdir(parents=True, exist_ok=True)
    (folder / "nextBuildNumber").write_text(str(max(b[0] for b in builds) + 1))
    for number, start, result, console, extra in builds:
        b = folder / "builds" / str(number)
        b.mkdir()
        (b / "build.xml").write_text(f"<build><startTime>{int(start * 1000)}</startTime>{'<result>' + result + '</result>' if result else ''}<duration>3000</duration>{extra}</build>")
        (b / "log").write_text(console)


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setattr(jenkins_source, "ROOT", root)
    monkeypatch.setattr(jenkins_index, "DB", tmp_path / "jenkins.sqlite")
    _job(root, "ci-cd/password-rotation", [(1, NOW - 3600, "SUCCESS", "[1/4] Setting 'admin' user with password 'Xy9secretQ' and admin role\n[+] Login check: admin / Xy9secretQ\ncurl -u dheeraj:11aabbccddeeff00112233445566778899 http://j\n",
                                            "<hudson.model.StringParameterValue><name>ADMIN_PASSWORD</name><value>Xy9secretQ</value></hudson.model.StringParameterValue><hudson.model.TimerTriggerCause/>")])
    _job(root, "advNetmanTests/lab1-ping", [(n, NOW - 7200 + n, "FAILURE" if n == 2 else "SUCCESS", f"[FAIL] H3 -> 8.8.8.8 [Loss: 100%]\nFinished: {'FAILURE' if n == 2 else 'SUCCESS'}\n" if n == 2 else "all good\n",
                                             "<upstreamProject>advNetmanTests/advtests-orchestrator</upstreamProject><upstreamUrl>x</upstreamUrl><upstreamBuild>9</upstreamBuild>") for n in (1, 2)])
    _job(root, "Lab3-Monitoring/lab3-sync-events", [(n, NOW - 60 * (200 - n), "SUCCESS", "synced 4 events\n", "") for n in range(1, 200)])
    _job(root, "troubleshooting-pipelines/interface-down", [(1, NOW - 100, "SUCCESS", "[Pipeline] stage\n[Pipeline] { (1. polling)\nQuestion: Is it down?\n", "<userId>dheeraj</userId>")])
    _job(root, "metaGlobalHack/anything", [(1, NOW - 100, "SUCCESS", "not ours\n", "")])
    _job(root, "lab1-reset-network", [(n, NOW - 90 * 86400 + n, "SUCCESS", "reset done\n", "") for n in range(1, 9)])
    return root


def test_every_build_is_indexed_with_its_secrets_hidden(jobs_dir):
    added = jenkins_index.keep_up(now=NOW)
    assert "metaGlobalHack/anything" not in added and added["troubleshooting-pipelines/interface-down"] == 1
    assert added["lab1-reset-network"] == jenkins_index.FIRST_KEEP  # idle for months, still shows its last builds
    with jenkins_index._open() as db:
        everything = "\n".join(t for (t,) in db.execute("SELECT text || params FROM builds"))
    assert "Xy9secretQ" not in everything and "11aabbccddeeff" not in everything and everything.count("[hidden]") >= 3
    rotation = jenkins_index.search("password", job="rotation")[0]
    assert rotation["started_by"] == "started by its timer" and "ADMIN_PASSWORD=[hidden]" in everything
    assert jenkins_index.keep_up(now=NOW) == {}  # nothing new the second time


def test_a_job_that_runs_all_day_keeps_a_line_not_a_console_when_it_passed(jobs_dir):
    jenkins_index.keep_up(now=NOW)
    sync = jenkins_index.search(job="lab3-sync-events", k=500)
    assert len(sync) == 199 and all("synced" not in b["text"] for b in sync)
    assert {r["job"]: r["frequent"] for r in jenkins_index.jobs()}["Lab3-Monitoring/lab3-sync-events"] is True


def test_the_ai_finds_a_failing_test_and_reads_it_whole(jobs_dir, monkeypatch):
    jenkins_index.keep_up(now=NOW)
    found = tools._search_jenkins(None, {"text": "H3 loss", "result": "failure"})
    assert "advNetmanTests/lab1-ping #2 FAILURE" in found["output"] and "started by advNetmanTests/advtests-orchestrator #9" in found["output"]
    assert "lab1-ping: latest #2" in tools._search_jenkins(None, {})["output"]
    monkeypatch.setattr(jenkins_index, "PAGE", 20)
    page = tools._read_build(None, {"job": "advNetmanTests/lab1-ping", "number": 2, "part": 2})
    assert page["saw"].endswith("part 2 of 3.") and "ask for the next part" in page["output"]
    assert tools._read_build(None, {"job": "metaGlobalHack/anything", "number": 1})["outcome"] == "invalid"
    assert tools._read_build(None, {"job": "troubleshooting-pipelines/interface-down", "number": 1})["output"].count("== 1. polling ==") == 1


def test_the_brief_lists_this_runs_builds():
    record = {"rule": {"id": "interface-down"}, "jenkins": {"ran": "jenkins", "folder": "troubleshooting-pipelines", "pipeline": "interface-down", "number": 4, "result": "SUCCESS"},
              "tried": [{"rule": "other", "jenkins": {"ran": "here", "pipeline": "other", "why": "every Jenkins executor was busy"}}], "repair": {"title": "Put it back", "jenkins": None}}
    assert jenkins_index.of_run(record) == ["- interface-down: troubleshooting-pipelines/interface-down #4, SUCCESS", "- other: Jenkins could not run it, so it ran here (every Jenkins executor was busy)"]
