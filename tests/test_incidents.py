"""The interface-down pipeline without touching the lab: trap line -> event -> run -> stored record."""

import dataclasses
import json

import pytest

from tests.conftest import REPO
from ts import engine, store
from ts.intake import parse_line
from ts.lab import Evidence, Result
from ts.registry import CHECKS
from ts.ruleset import load_ruleset

TRAP = ("[2026-11-06 13:26:57] TRAPv2 from clab-lab1-s4: iso.3.6.1.2.1.1.3.0 = Timeticks: (126; 14 days)\t"
        "iso.3.6.1.6.3.1.1.4.1.0 = OID: iso.3.6.1.6.3.1.1.5.3\t"
        "iso.3.6.1.2.1.2.2.1.1.2 = INTEGER: 2\t"
        'iso.3.6.1.2.1.2.2.1.2.2 = STRING: "Ethernet2"\t'
        "iso.3.6.1.2.1.2.2.1.7.2 = INTEGER: 1\t"
        "iso.3.6.1.2.1.2.2.1.8.2 = INTEGER: 2\t"
        'iso.3.6.1.2.1.31.1.1.1.18.2 = STRING: "To-S3-Core"')
MAC_LEARN = ("[2026-11-06 16:00:03] TRAPv2 from clab-lab1-s3: iso.3.6.1.2.1.1.3.0 = Timeticks: (1) 0:00:00.01\t"
             "iso.3.6.1.6.3.1.1.4.1.0 = OID: iso.3.6.1.4.1.30065.3.2.0.3")


@pytest.fixture
def rule():
    return load_ruleset(REPO / "rules").get("interface-down").rule


@pytest.fixture
def event():
    return parse_line(TRAP)


def fake(monkeypatch, answers):
    """Make each named check return a canned Result. answers[name] is a Result or a callable(args) -> Result."""
    calls = []

    def stub(name):
        def run(ctx, **args):
            calls.append((name, args))
            a = answers[name]
            return a(args) if callable(a) else a
        return run

    for name in answers:
        monkeypatch.setitem(CHECKS, name, dataclasses.replace(CHECKS[name], func=stub(name)))
    return calls


def test_parse_link_down(event):
    assert event["event"] == "interface.down"
    assert (event["device"], event["interface"], event["description"]) == ("s4", "Ethernet2", "To-S3-Core")
    assert (event["role"], event["platform"], event["admin_status"]) == ("core", "eos", "up")
    assert event["trap_time"] == "2026-11-06 13:26:57" and isinstance(event["time"], float)


def test_other_traps_are_ignored():
    assert parse_line(MAC_LEARN) is None
    assert parse_line("not a trap line") is None


def test_short_flap_stops_at_the_first_check(monkeypatch, rule, event):
    fake(monkeypatch, {"snmp_saw_down": Result("up", "Polling never saw it down.")})
    rec = engine.run(rule, event)
    assert len(rec["steps"]) == 1
    assert rec["result"]["kind"] == "stop" and rec["result"]["step"] == 1
    assert "Ethernet2" in rec["result"]["text"]
    assert rec["id"].endswith("-s4-Ethernet2")


def test_far_end_shut_walks_the_whole_route(monkeypatch, rule, event):
    calls = fake(monkeypatch, {
        "snmp_saw_down": Result("down", "down in 5 of 9 samples"),
        "traffic_stopped": Result("stopped", "0 packets either way",
                                  evidence=[Evidence("prometheus", "rate(...)", series=[[1, 0], [2, 0]])]),
        "device_log": Result("link_lost", "changed state to down"),
        "interface_status": lambda a: Result("down" if a["device"] == "s4" else "admin_down", "state"),
        "topology_peer": Result("found", "s3 Ethernet2", values={"peer_device": "s3", "peer_interface": "Ethernet2"}),
        "device_up": Result("up", "answers"),
    })
    rec = engine.run(rule, event)
    assert [s["id"] for s in rec["steps"]] == ["polling", "traffic", "log", "now", "peer",
                                               "peer-device", "peer-port"]
    assert calls[-1] == ("interface_status", {"device": "s3", "interface": "Ethernet2"})
    assert rec["result"]["kind"] == "cause" and rec["result"]["label"] == "interface.far_end_shut"
    assert "Ethernet2 on s3" in rec["result"]["text"]
    assert rec["steps"][1]["evidence"][0]["series"] == [[1, 0], [2, 0]]
    assert rec["mode"] == "active"
    json.dumps(rec)  # the record must be storable as is


def test_a_failing_check_is_recorded_not_fatal(monkeypatch, rule, event):
    def boom(_args):
        raise RuntimeError("prometheus is down")
    fake(monkeypatch, {"snmp_saw_down": boom, "traffic_stopped": Result("flowing", "packets kept moving")})
    rec = engine.run(rule, event)
    assert rec["steps"][0]["outcome"] == "error" and "prometheus is down" in rec["steps"][0]["saw"]
    assert rec["result"]["step"] == 2


def test_undeclared_outcome_becomes_an_error(monkeypatch, rule, event):
    fake(monkeypatch, {"snmp_saw_down": Result("sideways", "?"), "traffic_stopped": Result("flowing", "moving")})
    rec = engine.run(rule, event)
    assert rec["steps"][0]["outcome"] == "error" and "does not declare" in rec["steps"][0]["saw"]


def test_missing_value_stops_the_step_from_running(monkeypatch, rule, event):
    calls = fake(monkeypatch, {
        "snmp_saw_down": Result("down", ""), "traffic_stopped": Result("stopped", ""),
        "device_log": Result("nothing", ""), "interface_status": Result("down", ""),
        "topology_peer": Result("found", "but said nothing"),  # no peer_device / peer_interface
        "device_up": Result("up", ""),
    })
    rec = engine.run(rule, event)
    peer_device = next(s for s in rec["steps"] if s["id"] == "peer-device")
    assert peer_device["outcome"] == "error" and "peer_device was never found" in peer_device["saw"]
    assert ("device_up", {"device": "unknown"}) not in calls


def test_rule_map_keeps_blanks_on_its_own(rule):
    view = json.dumps(engine.rule_map(rule, {}))
    assert "{interface}" in view and "{peer_device}" in view
    assert "unknown on unknown" not in view  # how a blank used to show up
    filled = engine.rule_map(rule, {"device": "s4", "interface": "Ethernet2"})
    now = next(s for s in filled["steps"] if s["id"] == "now")
    assert now["ask"] == "Is Ethernet2 still down?"


def test_store_round_trip(tmp_path, monkeypatch, rule, event):
    fake(monkeypatch, {"snmp_saw_down": Result("up", "no")})
    rec = engine.run(rule, event)
    store.save(rec, tmp_path)
    assert store.exists(rec["id"], tmp_path)
    assert store.load(rec["id"], tmp_path)["result"] == rec["result"]
    [summary] = store.summaries(tmp_path)
    assert (summary["device"], summary["steps"], summary["rule"]) == ("s4", 1, "interface-down")
    assert store.load("../incidents", tmp_path) is None


def test_store_delete(tmp_path, monkeypatch, rule, event):
    fake(monkeypatch, {"snmp_saw_down": Result("up", "no")})
    rec = engine.run(rule, event)
    store.save(rec, tmp_path)
    outside = tmp_path.parent / "outside.json"
    outside.write_text("{}")
    assert store.delete(f"../{outside.stem}", tmp_path) is False and outside.exists()
    assert store.delete("no-such-run", tmp_path) is False
    assert store.delete(rec["id"], tmp_path) is True
    assert not store.exists(rec["id"], tmp_path) and store.summaries(tmp_path) == []


def test_ui_delete_endpoint(tmp_path, monkeypatch, rule, event):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    from ts import web

    fake(monkeypatch, {"snmp_saw_down": Result("up", "no")})
    rec = engine.run(rule, event)
    store.save(rec, tmp_path)
    delete = store.delete
    monkeypatch.setattr(store, "delete", lambda incident_id: delete(incident_id, tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(REPO / "rules"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def call(method, path):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            conn.request(method, path)
            status = conn.getresponse().status
            conn.close()
            return status

        assert call("OPTIONS", f"/api/incidents/{rec['id']}") == 501  # so another site's page cannot send the DELETE
        assert call("DELETE", "/api/rules/interface-down") == 404
        assert store.exists(rec["id"], tmp_path)
        assert call("DELETE", f"/api/incidents/{rec['id']}") == 204
        assert not store.exists(rec["id"], tmp_path)
        assert call("DELETE", f"/api/incidents/{rec['id']}") == 404
    finally:
        httpd.shutdown()
