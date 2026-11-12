"""Monitors: building variables, replaying the past, the watcher's firing rules, and the AI's guard rails."""

import time

import pytest

from ts import history, monitor_ai, monitors, review

FLAPS = {"name": "flaps", "source": "metric", "device": "s1", "metric": "interfaces_interface_state_counters_carrier_transitions",
         "port": "Ethernet4", "function": "increase", "window": "10m"}
DOWNS = {"name": "downs", "source": "events", "device": "s1", "protocol": "trap", "trap": "linkDown", "text": "Ethernet4", "window": "10m"}


def spec(**extra):
    base = {"id": "s1-eth4-flapping", "title": "s1 Ethernet4 keeps losing its link", "device": "s1", "interface": "Ethernet4",
            "variables": [FLAPS, DOWNS], "conditions": [{"variable": "flaps", "op": ">=", "value": 2}, {"variable": "downs", "op": ">=", "value": 1}], "match": "all"}
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _own_db(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB", tmp_path / "history.sqlite")
    monkeypatch.setattr(monitors, "check_exists", lambda v: None)
    from ts.sources import jenkins
    monkeypatch.setattr(jenkins, "change_windows", lambda since, scan=400: [])


def test_a_variable_is_built_from_named_parts_never_free_text():
    v = monitors.clean_variable(FLAPS)
    assert monitors.promql(v) == 'max(increase(interfaces_interface_state_counters_carrier_transitions{source="s1",interface_name="Ethernet4"}[10m]))'
    assert "lost and regained its link" in monitors.words(v)
    snmp = monitors.clean_variable({"name": "state", "source": "metric", "device": "s3", "metric": "network_interface_oper_status", "port": "Ethernet2", "function": "changes", "window": "5m"})
    assert monitors.promql(snmp) == 'max(changes(network_interface_oper_status{device="s3",interface="Ethernet2"}[5m]))'
    cpu = monitors.clean_variable({"name": "cpu", "source": "metric", "device": "r1", "metric": "node_cpu_utilization_percent", "function": "avg_over", "window": "15m"})
    assert monitors.promql(cpu) == 'max(avg_over_time(node_cpu_utilization_percent{device="r1"}[15m]))'


@pytest.mark.parametrize("bad, says", [
    ({**FLAPS, "function": "now", "window": None}, "only ever goes up"),
    ({**FLAPS, "metric": "up"}, "not a metric monitors can use"),
    ({**FLAPS, "device": "r5"}, "out of scope"),
    ({**FLAPS, "port": 'Eth4"}) or vector(1'}, "not a port name"),
    ({**FLAPS, "window": "ten minutes"}, "not a duration"),
    ({"name": "cpu", "source": "metric", "device": "r1", "metric": "node_cpu_utilization_percent", "port": "Ethernet1", "function": "now"}, "has no ports"),
    ({"name": "logs", "source": "events", "device": "s2", "protocol": "syslog", "window": "10m"}, "needs text"),
    ({**DOWNS, "trap": "somethingElse"}, "trap must be one of"),
])
def test_a_bad_variable_says_exactly_what_to_change(bad, says):
    with pytest.raises(monitors.Invalid, match=says):
        monitors.clean_variable(bad)


def test_a_monitor_names_every_problem_and_uses_every_variable():
    with pytest.raises(monitors.Invalid) as caught:
        monitors.clean(spec(id="X", conditions=[{"variable": "flaps", "op": "~", "value": 1}]))
    assert "id 'X'" in str(caught.value) and "op must be" in str(caught.value)
    with pytest.raises(monitors.Invalid, match="downs is never used"):
        monitors.clean(spec(conditions=[{"variable": "flaps", "op": ">=", "value": 2}]))
    clean = monitors.clean(spec())
    assert clean["cooldown_minutes"] == 30 and clean["hold_seconds"] == 0
    assert "Starts a run on s1 when" in monitors.describe(clean) and " AND " in monitors.describe(clean)


def test_two_monitors_watching_the_same_thing_share_a_fingerprint():
    a = monitors.clean(spec())
    b = monitors.clean(spec(id="another-name", title="The same thing, said differently",
                            variables=[{**DOWNS, "name": "dd"}, {**FLAPS, "name": "ff"}],
                            conditions=[{"variable": "dd", "op": ">=", "value": 1}, {"variable": "ff", "op": ">=", "value": 2}]))
    assert monitors.fingerprint(a) == monitors.fingerprint(b)
    assert monitors.fingerprint(a) != monitors.fingerprint(monitors.clean(spec(conditions=[{"variable": "flaps", "op": ">=", "value": 3}, {"variable": "downs", "op": ">=", "value": 1}])))


def _series(n, true_at):
    return [3.0 if i in true_at else 0.0 for i in range(n)]


def test_replay_holds_back_what_a_test_caused_and_respects_hold_and_cooldown():
    s = monitors.clean(spec(conditions=[{"variable": "flaps", "op": ">=", "value": 2}], variables=[FLAPS], hold_seconds=120, cooldown_minutes=10))
    times = [float(60 * i) for i in range(60)]
    flaps = _series(60, {1, 2, 3, 4, 10, 11, 12, 13, 14, 16, 17, 18, 19, 40, 41, 42, 43})
    windows = [{"job": "advNetmanTests/advtests-orchestrator #280", "start": 2400.0, "end": 2500.0}]
    episodes = monitors.replay(s, times, {"flaps": flaps}, windows)
    fired = [e for e in episodes if e["fired_at"] is not None]
    assert [e["fired_at"] for e in fired] == [180.0, 780.0]  # after holding 2 minutes; the second waits for the 10-minute cooldown after 180
    assert not any(e["began"] == 960.0 and e["fired_at"] for e in episodes)  # 16-19 is inside the cooldown after 720
    assert [e["held_back"] for e in episodes if e["began"] == 2400.0] == [True]


def test_backtest_says_whether_the_incident_was_caught(monkeypatch):
    times = [float(1000 + 60 * i) for i in range(200)]
    values = {"flaps": _series(200, {50, 51, 52, 150}), "downs": _series(200, {51, 52, 150})}
    monkeypatch.setattr(monitors, "history_of", lambda v, start, end, step=60: (times, values[v["name"]]))
    s = monitors.clean(spec())
    result = monitors.backtest(s, around=times[50], now=times[-1], windows=[])
    assert result["incident"]["caught"] and result["incident"]["minutes_from_incident"] == 1.0
    assert result["fired"] == 2 and result["false_alarms"] == 1


def _watch(monkeypatch, outcomes, tests=()):
    """A watcher whose monitor reads the given sequence of holds."""
    seen = iter(outcomes)
    monkeypatch.setattr(monitors, "evaluate", lambda sp, at=None, since=None: (lambda h: {"values": {"flaps": 3.0 if h else 0.0, "downs": 1.0 if h else 0.0}, "conditions": [h, h], "holds": h, "missing": []})(next(seen)))
    fired = []
    w = monitors.Watch(fired.append, log=lambda m: None)
    jobs = list(tests)  # what each look at the test jobs finds, in order: a job name, or None for nothing running

    def tests_now(now):
        found = jobs.pop(0) if jobs else None
        return [found] if found else []

    monkeypatch.setattr(w, "tests_now", tests_now)
    return w, fired


def test_the_watcher_fires_once_per_episode_and_cools_down(monkeypatch):
    monitors.add(spec(cooldown_minutes=5), "because", "test")
    w, fired = _watch(monkeypatch, [True, True, False, True, False, True])
    for t in (0, 30, 60, 90, 120, 400):
        w.step(10_000 + t)
    assert [e["time"] for e in fired] == [10_000, 10_400]   # the episode at 90 s is inside the 5-minute cooldown
    event = fired[0]
    assert event["event"] == "monitor.fired" and event["source"] == "monitor" and event["device"] == "s1" and event["monitor"] == "s1-eth4-flapping"
    assert event["interface"] == "Ethernet4" and "Why this monitor exists: because" in event["detail"]
    kinds = [e["kind"] for e in monitors.events("s1-eth4-flapping")]
    assert kinds.count("fired") == 2 and "cleared" in kinds
    assert monitors.get("s1-eth4-flapping")["state"] == "fired"
    text = monitors.metrics_text(10_401)
    assert 'ts_monitor_variable{monitor="s1-eth4-flapping",variable="flaps",device="s1"} 3' in text and 'ts_monitor_state{monitor="s1-eth4-flapping"} 2' in text


def test_a_monitor_never_fires_for_what_a_test_job_caused(monkeypatch):
    monitors.add(spec(), "", "test")
    w, fired = _watch(monkeypatch, [True, True, True, False, True], tests=["advNetmanTests/lab1-ping #1", None])
    for t in (0, 30, 60, 90, 120):
        w.step(20_000 + t)
    assert [e["time"] for e in fired] == [20_120]  # held back while it lasted, fires on the next episode
    kinds = [e["kind"] for e in monitors.events("s1-eth4-flapping")]
    assert "held_back" in kinds


def test_a_new_monitor_counts_only_what_happened_after_it_was_accepted(monkeypatch):
    """It was written from trouble that has been looked at already; firing on that again is a wasted run."""
    accepted = monitors.add(spec(), "", "test")["created"]
    asked = []

    class Day:
        def count_documents(self, query):
            asked.append(query["timestamp"]["$gt"])
            return 7

    class Lake:
        def list_collection_names(self):
            return [time.strftime("%Y_%m_%d", time.gmtime(accepted + d)) for d in (-86400, 0, 86400)]

        def __getitem__(self, day):
            return Day()

    monkeypatch.setattr(monitors, "_lake", lambda: Lake())
    queries = []
    monkeypatch.setattr(monitors, "prom_query", lambda expr, at=None: queries.append(expr) or [{"value": [0, "5"]}])
    w = monitors.Watch(lambda e: None, log=lambda m: None)
    monkeypatch.setattr(w, "tests_now", lambda now: [])
    w.step(accepted + 90)
    assert asked and min(asked) == accepted  # not 10 minutes back, which would reach before it was accepted
    assert queries[-1].endswith("[90s]))")
    w.step(accepted + 3600)  # once a whole window has passed, the window is the one it was given
    assert min(asked[-1:]) == accepted + 3600 - 600 and queries[-1].endswith("[10m]))")


def test_its_own_change_makes_a_monitor_wait_not_give_up_the_episode(monkeypatch):
    """An AI stop-gap holds the change lock too. The trouble began without it, so the monitor fires once the change is done."""
    monitors.add(spec(), "", "test")
    w, fired = _watch(monkeypatch, [True, True, True])
    own = [True, False]
    monkeypatch.setattr(w, "own_change", lambda: own.pop(0) if own else False)
    w.step(30_000)
    assert fired == [] and monitors.get("s1-eth4-flapping")["state"] == "holding"
    w.step(30_030)
    assert [e["time"] for e in fired] == [30_030]
    assert "held_back" not in [e["kind"] for e in monitors.events("s1-eth4-flapping")]


def test_only_another_process_holding_the_lock_counts_as_a_job(tmp_path, monkeypatch):
    import os

    from ts import lab
    lock = tmp_path / "change.lock"
    lock.write_text("")
    inode = lock.stat().st_ino
    table = tmp_path / "locks"
    table.write_text(f"1: FLOCK  ADVISORY  WRITE {os.getpid()} 00:2e:{inode} 0 EOF\n2: POSIX  ADVISORY  WRITE 77 00:2e:{inode} 0 EOF\n")
    assert lab.lock_holders(lock, table) == {os.getpid()} and lab.change_running(lock, table)
    table.write_text(f"1: FLOCK  ADVISORY  WRITE 4242 00:2e:{inode} 0 EOF\n")
    assert lab.lock_holders(lock, table) == {4242}
    monkeypatch.setattr(lab, "lock_holders", lambda *a: {os.getpid()})
    from ts.sources import jenkins
    monkeypatch.setattr(jenkins, "running_change_jobs", lambda: [])
    w = monitors.Watch(lambda e: None, log=lambda m: None)
    assert w.tests_now(1.0) == [] and w.own_change()
    monkeypatch.setattr(lab, "lock_holders", lambda *a: {4242})
    assert w.tests_now(2.0) == ["a job holding the network change lock"] and not w.own_change()


def test_a_paused_monitor_is_not_looked_at(monkeypatch):
    monitors.add(spec(), "", "test")
    monitors.set_status("s1-eth4-flapping", "paused")
    w, fired = _watch(monkeypatch, [True])
    assert w.step(1.0) == [] and fired == []


def test_accepting_a_suggestion_makes_an_active_monitor(monkeypatch):
    from ts import grafana_monitors
    monkeypatch.setattr(grafana_monitors, "sync_quietly", lambda: "updated (1 monitors)")
    clean = monitors.clean(spec())
    number = review.add("monitor", clean["title"], "It would have caught the flap.", {"monitor": clean}, {"test": {"passed": True}}, "run-1", "m", monitors.fingerprint(clean))
    outcome = review.accept(number, "operator")
    assert outcome == {"monitor": "s1-eth4-flapping", "grafana": "updated (1 monitors)", "closed": []}
    row = monitors.get("s1-eth4-flapping")
    assert row["status"] == "active" and row["proposal"] == number and row["why"] == "It would have caught the flap."


def _session():
    record = {"id": "20261111T000000Z-s1-Ethernet4", "event": {"time": 5000.0, "device": "s1", "title": "s1 Ethernet4 went down", "event": "interface.down"},
              "result": {"kind": "cause", "label": "link.flapping", "text": "It flaps."}, "steps": []}
    return monitor_ai.Session(record, "test-model")


def test_the_ai_can_only_propose_what_it_tested_and_passed(monkeypatch):
    s = _session()
    tested = {"passed": True, "verdict": "Passes", "catches_this_incident": True}
    monkeypatch.setattr(monitors, "backtest", lambda sp, around=None, windows=None, **k: {"fired": 1, "held_back": 0, "true_share": 0.01, "episodes": [{"began": 4990.0, "fired_at": 5000.0, "until": 5100.0, "held_back": False, "jobs": []}],
                                                                                      "variables": {}, "notes": [], "from": 0, "to": 6000, "false_alarms": 0,
                                                                                      "incident": {"caught": True, "fired_at": 5000.0, "minutes_from_incident": 0.0, "held_back_then": False}})
    why = "It catches s1 Ethernet4 flapping: the link counter rose by 3 at the incident and never above 0 otherwise. The replay found no false alarms."
    refused = monitor_ai.call(s, "propose_monitor", {**spec(), "why": why, "reason": "r"})
    assert refused["outcome"] == "refused" and "test_monitor" in refused["saw"]
    assert monitor_ai.call(s, "test_monitor", {**spec(), "reason": "r"})["outcome"] == "passed"
    saved = monitor_ai.call(s, "propose_monitor", {**spec(), "why": why, "reason": "r"})
    assert saved["outcome"] == "saved" and s.proposals
    again = monitor_ai.call(s, "propose_monitor", {**spec(id="other-id"), "why": why, "reason": "r"})
    assert again["outcome"] == "refused" and "already suggested" in again["saw"]
    assert tested  # the call records show every step
    assert [c["tool"] for c in s.calls] == ["propose_monitor", "test_monitor", "propose_monitor", "propose_monitor"]


def test_a_failed_test_cannot_be_proposed(monkeypatch):
    s = _session()
    monkeypatch.setattr(monitors, "backtest", lambda sp, around=None, windows=None, **k: {"fired": 9, "held_back": 0, "true_share": 0.2, "episodes": [], "variables": {}, "notes": [], "from": 0, "to": 1, "false_alarms": 9,
                                                                                      "incident": {"caught": True, "fired_at": 5000.0, "minutes_from_incident": 0.0, "held_back_then": False}})
    assert monitor_ai.call(s, "test_monitor", {**spec(), "reason": "r"})["outcome"] == "failed"
    out = monitor_ai.call(s, "propose_monitor", {**spec(), "why": "x" * 50, "reason": "r"})
    assert out["outcome"] == "refused" and "false alarms" in out["saw"]


def test_which_runs_get_a_look(monkeypatch):
    s = _session().record
    assert monitor_ai.skip_reason(s) is None
    assert monitor_ai.skip_reason({**s, "event": {**s["event"], "source": "monitor"}}) == "a monitor started this run"
    assert monitor_ai.skip_reason({**s, "result": {"kind": "stop", "text": "back within seconds"}}) == "the run found nothing wrong"
    from ts.sources import jenkins
    monkeypatch.setattr(jenkins, "change_windows", lambda since, scan=400: [{"job": "nightly", "start": 4000.0, "end": 6000.0}])
    assert monitor_ai.skip_reason(s) == "a test or change job was running when it began"
