"""The parts of a run, as the UI and the troubleshooting-run build show them."""

from __future__ import annotations

import json
import urllib.parse

from ts import jenkins_api, monitors, pipelines, stages


def rec(**kw):
    base = {"id": "r1", "event": {"event": "monitor.fired", "source": "monitor", "monitor": "s4-flap", "device": "s4", "interface": "Ethernet2", "time": 1_800_000_000.0, "title": "Monitor fired: s4 flapping"},
            "rule": {"id": "s4-flap-rule", "title": "S4 flap", "source": "ai", "position": "main"}, "mode": "live", "steps": [{"n": 1, "ask": "Is it flapping?", "outcome": "yes"}],
            "result": {"kind": "cause", "label": "link.flapping", "text": "It flaps."}, "tried": [], "order": ["s4-flap-rule"]}
    base.update(kw)
    return base


AI_MADE = {"by": "ai", "proposal": 31, "accepted_by": "operator", "accepted": 1.0, "from_run": "r0"}


def test_a_monitor_the_ai_suggested_is_marked_from_the_start():
    r = rec()
    r["event"]["monitor_origin"] = AI_MADE
    parts = stages.of(r)
    assert parts[0]["label"] == "Trigger: AI monitor s4-flap" and parts[0]["ai_monitor"]
    assert stages.mark(r) == "AI monitor" and "which the AI suggested (suggestion #31)" in stages.badge(r)


def test_a_monitor_a_person_set_up_is_not():
    r = rec()
    r["event"]["monitor_origin"] = {"by": "person"}
    assert stages.of(r)[0]["label"] == "Trigger: monitor s4-flap" and stages.mark(r) == ""


def test_monitor_rows_say_who_put_them_there():
    assert monitors.origin({"proposal": 31, "by": "operator", "created": 5.0, "run_id": "r0"})["by"] == "ai"
    assert monitors.origin({"proposal": None, "by": "operator"})["by"] == "person"
    spec = {"id": "m", "device": "s4", "title": "t", "variables": [], "conditions": [], "match": "all", "hold_seconds": 0, "cooldown_minutes": 0}
    ev = monitors.fired_event({**spec, "query": ""}, {"values": {}}, 1.0, "", {"by": "ai", "proposal": 3})
    assert ev["monitor_origin"]["proposal"] == 3


def test_a_learned_run_shows_first_look_rules_check_agent_report_in_order():
    r = rec(learned=True, before_rules={"time": 1.0, "look": {"changed": {"summary": "r3 differs."}}, "drift": ["r3"], "repairs": [{"device": "r3", "status": "fixed"}]},
            check={"status": "done", "verdict": "broken", "summary": "Still flapping.", "calls": [], "model": "m", "started": 1, "finished": 31},
            agent={"status": "working", "why": "no repair", "calls": [{}]})
    keys = [p["key"] for p in stages.of(r)]
    assert keys == ["trigger", "first_look", "rule:s4-flap-rule", "check", "agent", "report"]
    parts = {p["key"]: p for p in stages.of(r)}
    assert parts["rule:s4-flap-rule"]["label"].endswith("[AI]") and parts["check"]["state"] == "failed" and parts["agent"]["state"] == "running"
    assert stages.ready(stages.of(r)) == 4  # told up to the check; the agent is still working


def test_a_check_that_found_it_working_ends_the_run_without_the_agent():
    r = rec(learned=True, before_rules={"drift": [], "look": {}}, check={"status": "done", "verdict": "working", "summary": "Up 12 min."},
            agent={"status": "not_needed", "note": "The AI check found it working"}, concluded=2.0, ai_note="Nothing here needed a report.")
    parts = stages.of(r)
    assert [p["key"] for p in parts][-2:] == ["check", "report"] and stages.ready(parts) == len(parts)


def test_a_watch_only_rule_is_marked_and_its_own_finding_shown():
    r = rec(mode="shadow", watch_only_result={"kind": "cause", "text": "It flaps."}, result={"kind": "stop", "text": "nothing decided"})
    part = stages.of(r)[1]
    assert "[watch-only]" in part["label"] and "It flaps." in part["text"]


def test_labels_are_unique_and_rule_order_is_kept():
    r = rec(tried=[{"rule": "a", "mode": "live", "result": {"kind": "stop"}, "steps": []}], order=["a", "s4-flap-rule"], repair={"status": "fixed", "rule": "fix-it", "jenkins": {"url": "u"}})
    parts = stages.of(r)
    assert [p["key"] for p in parts] == ["trigger", "rule:a", "rule:s4-flap-rule", "repair", "report"]
    assert parts[3]["link"] == "u" and len({p["label"] for p in parts}) == len(parts)


def test_nothing_is_told_past_a_part_still_going():
    parts = [{"state": "done"}, {"state": "running"}, {"state": "waiting"}]
    assert stages.ready(parts) == 1


def test_jenkins_is_told_each_stage_with_the_name_of_the_next(monkeypatch):
    sent = []

    def request(method, path, data=None, content_type="", want="json", timeout=20):
        form = dict(urllib.parse.parse_qsl(data.decode()))
        sent.append((path.rsplit("/input/", 1)[1], {p["name"]: p["value"] for p in json.loads(form["json"])["parameter"]}))
        return ""

    monkeypatch.setattr(jenkins_api, "_request", request)
    monkeypatch.setattr(stages, "_TOLD", {})
    parts = [{"label": "A", "state": "done", "text": "a"}, {"label": "B", "state": "failed", "text": "b"}, {"label": "C", "state": "done", "text": "c"}]
    stages._WORK.put(("r1", 7, parts, 0))
    import threading
    t = threading.Thread(target=stages._work_forever, daemon=True)
    t.start()
    for _ in range(100):
        if len(sent) == 3:
            break
        t.join(0.05)
    assert [s[0] for s in sent] == ["Stage1/submit", "Stage2/submit", "Stage3/submit"]
    assert [s[1]["NEXT"] for s in sent] == ["B", "C", ""] and sent[1][1]["STATE"] == "failed"


def test_the_run_pipeline_waits_without_an_executor():
    script = pipelines.run_script()
    assert "node" not in script.split("currentBuild", 1)[1].split("input(", 1)[0]
    assert "input(id: \"Stage${n}\"" in script and "params.MARK" in script
    xml, _print = pipelines.run_config_xml()
    assert "<name>MARK</name>" in xml and "<name>BADGE</name>" in xml


def test_nothing_reaches_jenkins_unless_the_watcher_turned_it_on(monkeypatch):
    monkeypatch.setattr(stages, "ENABLED", False)
    monkeypatch.setattr(jenkins_api, "start", lambda *a: (_ for _ in ()).throw(AssertionError("started a build")))
    r = rec()
    stages.advance(r)
    assert "jenkins_run" not in r
