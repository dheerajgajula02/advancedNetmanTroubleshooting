"""When the health check emails: only on news, and only after a problem shows up twice."""

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "deploy" / "health" / "health_check.py"
spec = importlib.util.spec_from_file_location("health_check", HERE)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def results(**states):
    return {name: hc.Result(state, f"{name} is {state}") for name, state in states.items()}


def test_one_failure_is_not_news_but_two_in_a_row_are():
    state = {}
    assert hc.decide(results(backups="fail", nmas="ok"), state, 1000) is None
    subject, body = hc.decide(results(backups="fail", nmas="ok"), state, 1300)
    assert "1 problem" in subject and "backups" in subject
    assert "NEW PROBLEMS" in body and "backups is fail" in body


def test_a_known_problem_is_not_repeated_until_the_reminder_is_due():
    state = {}
    hc.decide(results(backups="fail"), state, 0)
    hc.decide(results(backups="fail"), state, 300)
    assert hc.decide(results(backups="fail"), state, 600) is None
    subject, body = hc.decide(results(backups="fail"), state, 300 + hc.REMIND + 1)
    assert "STILL FAILING" in body


def test_recovery_is_news():
    state = {}
    hc.decide(results(backups="fail"), state, 0)
    hc.decide(results(backups="fail"), state, 300)
    subject, body = hc.decide(results(backups="ok"), state, 600)
    assert subject == "[boulder health] all clear again"
    assert "WORKING AGAIN" in body and state["alerted"] == {}


def test_a_flap_back_to_ok_before_the_second_look_says_nothing():
    state = {}
    hc.decide(results(disk="fail"), state, 0)
    assert hc.decide(results(disk="ok"), state, 300) is None
    assert hc.decide(results(disk="fail"), state, 600) is None


def test_warnings_never_email():
    state = {}
    for t in range(0, 3000, 300):
        assert hc.decide(results(walk="warn"), state, t) is None


def test_build_results_come_newest_first_and_skip_running_builds(tmp_path, monkeypatch):
    builds = tmp_path / "job" / "builds"
    for n, xml in [(1, "<result>SUCCESS</result>"), (2, "<result>FAILURE</result>"), (3, "<building/>")]:
        (builds / str(n)).mkdir(parents=True)
        (builds / str(n) / "build.xml").write_text(xml)
    monkeypatch.setattr(hc, "JENKINS", tmp_path)
    assert [r for r, _ in hc.last_builds("job", 2)] == ["FAILURE", "SUCCESS"]
