"""The scenarios beyond link-down: trap parsing, waiting, the sweep, config comparison, and every way out of every rule."""

import dataclasses
import http.client
import json
import threading

import pytest

from tests.conftest import REPO
from ts import configs, engine, intake, lab, sweep, tags
from ts.intake import HOLD, MAX_HOLD, Waiting, parse_line
from ts.lab import Result
from ts.registry import CHECKS
from ts.ruleset import load_ruleset
from ts.vocab import EVENT_TYPES

HEAD = "[2026-11-08 07:00:00] TRAPv2 from clab-lab1-{dev}: iso.3.6.1.6.3.1.1.4.1.0 = OID: {oid}"
OSPF = (HEAD.format(dev="s3", oid="iso.3.6.1.2.1.14.16.2.2") + "\tiso.3.6.1.2.1.14.10.1.1.10.0.31.1.0 = IpAddress: 10.0.31.1"
        "\tiso.3.6.1.2.1.14.10.1.3.10.0.31.1.0 = IpAddress: 1.1.1.1\tiso.3.6.1.2.1.14.10.1.6.10.0.31.1.0 = INTEGER: {state}")
VRRP = HEAD.format(dev="s1", oid="iso.3.6.1.2.1.68.0.1") + "\tiso.3.6.1.2.1.68.1.3.1.7.2000010.10 = IpAddress: 10.10.10.2"
COLD = HEAD.format(dev="r3", oid="iso.3.6.1.6.3.1.1.5.1")
SAVED = HEAD.format(dev="s3", oid="iso.3.6.1.4.1.30065.3.9.3.0.1")

RULES = [r for r in load_ruleset(REPO / "rules").parsed if r.table == "diagnose"]
EXITS = [(r.id, i, name) for r in RULES for i, s in enumerate(r.steps) for name, o in s.then.items() if o.kind != "next"]


def test_ospf_only_a_neighbor_going_down_is_an_event():
    ev = parse_line(OSPF.format(state=1))
    assert (ev["event"], ev["device"], ev["neighbor_id"], ev["neighbor_ip"]) == ("ospf.neighbor_down", "s3", "1.1.1.1", "10.0.31.1")
    assert ev["subject"] == "OSPF 1.1.1.1" and ev["title"] == "s3 lost OSPF neighbor 1.1.1.1"
    assert parse_line(OSPF.format(state=8)) is None and parse_line(OSPF.format(state=4)) is None


def test_vrrp_names_the_vlan_and_merges_by_gateway():
    ev = parse_line(VRRP)
    assert (ev["interface"], ev["vrid"], ev["master_ip"], ev["merge_key"]) == ("Vlan10", "10", "10.10.10.2", "vrrp:Vlan10")


def test_restart_and_config_events():
    assert parse_line(COLD)["event"] == "device.restarted" and parse_line(COLD)["merge_key"] == "restart:r3"
    assert parse_line(SAVED)["event"] == "config.changed" and parse_line(SAVED)["merge_key"] == "config:s3"


def ev(t, key="k", kind="config.changed"):
    return {"event": kind, "time": t, "merge_key": key, "device": "s3"}


def test_waiting_merges_until_quiet():
    w = Waiting(), HOLD["config.changed"]
    room, hold = w
    assert room.add(ev(0)) is True
    assert room.add(ev(100)) is False
    assert room.ready(hold) == []  # the second one pushed it back
    done = room.ready(100 + hold)
    assert len(done) == 1 and done[0]["time"] == 100 and done[0]["merged"] == 2
    assert room.items == []


def test_waiting_gives_up_on_a_burst_that_never_ends():
    room = Waiting()
    t = 0
    while t < MAX_HOLD + 500:
        room.add(ev(t))
        t += 60
    assert len(room.ready(MAX_HOLD)) == 1


def test_waiting_keeps_different_things_apart():
    room = Waiting()
    assert room.add(ev(0, "a")) and room.add(ev(0, "b"))
    assert room.add({"event": "interface.down", "time": 0, "device": "s1"})
    assert room.add({"event": "interface.down", "time": 0, "device": "s2"})  # no key, never merged
    assert len(room.items) == 4


def finding(key, ok):
    return sweep.Finding(key, ok, {"event": "x", "device": "d", "key": key})


def test_sweep_fires_once_when_something_goes_bad():
    state = {}
    assert sweep.step(state, [("a:", [finding("a:1", True)])], 10) == []  # the first look only learns
    fired = sweep.step(state, [("a:", [finding("a:1", False)])], 20)
    assert len(fired) == 1 and fired[0]["time"] == 20
    assert sweep.step(state, [("a:", [finding("a:1", False)])], 30) == []
    assert sweep.step(state, [("a:", [finding("a:1", True)])], 40) == []
    assert len(sweep.step(state, [("a:", [finding("a:1", False)])], 50)) == 1


def test_sweep_notices_something_vanishing():
    state = {}
    sweep.step(state, [("a:", [finding("a:1", True), finding("a:2", True)])], 10)
    fired = sweep.step(state, [("a:", [finding("a:1", True)])], 20)
    assert [e["key"] for e in fired] == ["a:2"]
    assert sweep.step(state, [("a:", [finding("a:1", True)])], 30) == []


def test_no_answer_is_not_bad_news():
    state = {}
    sweep.step(state, [("a:", [finding("a:1", True)])], 10)
    assert sweep.step(state, [], 20) == [] and state["a:1"]["ok"]


def test_sweep_sits_out_a_change_job(monkeypatch):
    monkeypatch.setattr(lab, "change_running", lambda *a: True)
    monkeypatch.setattr(sweep, "_gather", lambda log: pytest.fail("looked while a job was running"))
    assert sweep.run({}) == []


def test_secrets_never_reach_a_diff():
    lines = configs.clean("username admin secret 5 abc\nsnmp-server community public ro\nenable password x\nhostname s1\n! comment\n")
    assert lines == ["hostname s1"]


def test_known_good_prefers_the_newer_copy(tmp_path):
    golden_root = tmp_path / "golden" / "20260924T025651Z"
    golden_root.mkdir(parents=True)
    (golden_root / "s1.cfg").write_text("hostname old\n")
    assert configs.known_good("s1", tmp_path, tmp_path / "golden")[1] == ["hostname old"]
    configs.save_known_good("s1", ["hostname new"], tmp_path)
    source, lines = configs.known_good("s1", tmp_path, tmp_path / "golden")
    assert lines == ["hostname new"] and "known-good copy" in source
    newer = tmp_path / "golden" / "20991231T000000Z"
    newer.mkdir()
    (newer / "s1.cfg").write_text("hostname newest\n")
    assert configs.known_good("s1", tmp_path, tmp_path / "golden")[1] == ["hostname newest"]


def _walk(monkeypatch, rule, stop_at=None, outcome=None):
    """Every step moves on, except stop_at, which answers with outcome."""
    order = iter(range(len(rule.steps)))

    def answer(ctx, **args):
        i = next(order)
        step, block = rule.steps[i], rule.steps[i].check.block
        picked = outcome if i == stop_at else next(o for o in block.outcomes if o not in step.then or step.then[o].kind == "next")
        return Result(picked, "saw", {name: f"{name}_value" for name in block.produces})

    for step in rule.steps:
        monkeypatch.setitem(CHECKS, step.check.name, dataclasses.replace(step.check.block, func=answer))
    wanted = rule.match.get("event") or sorted(tags.events_for(rule.match["tags"])) or ["monitor.fired"]  # a rule the AI wrote says which tags it runs for; only a monitor carries some
    kind = wanted if isinstance(wanted, str) else wanted[0]
    carried = {"tags": list(rule.match["tags"])} if kind == "monitor.fired" else {}
    return engine.run(rule, {"event": kind, "device": "s1", "role": "access", "platform": "eos", "time": 1.79e9, "subject": "thing",
                             "trap_time": "2026-11-08 07:00:00", "raw": "", **{f: f"{f}_value" for f in EVENT_TYPES[kind]}, **carried})


@pytest.mark.parametrize("rule_id,i,outcome", EXITS)
def test_every_exit_reads_properly(monkeypatch, rule_id, i, outcome):
    rule = next(r for r in RULES if r.id == rule_id)
    rec = _walk(monkeypatch, rule, i, outcome)
    assert len(rec["steps"]) == i + 1 and rec["result"]["kind"] == rule.steps[i].then[outcome].kind
    for text in [rec["result"].get("text", "")] + [s["ask"] for s in rec["steps"]]:
        assert "{" not in text, text


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.id)
def test_every_rule_reaches_its_end(monkeypatch, rule):
    # A step that ends the rule on every answer it can give (the last one, usually) decides; nothing after it runs.
    decides = next((i for i, s in enumerate(rule.steps) if all(o in s.then and s.then[o].kind != "next" for o in s.check.block.outcomes)), None)
    if decides is not None:
        rec = _walk(monkeypatch, rule, decides, rule.steps[decides].check.block.outcomes[0])
        assert rec["result"]["step"] == decides + 1 and len(rec["steps"]) == decides + 1
    else:
        rec = _walk(monkeypatch, rule)
        assert rec["result"]["step"] == "end" and len(rec["steps"]) == len(rule.steps)
    assert "{" not in rec["result"]["text"]


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.id)
def test_every_rule_says_what_it_is_and_what_starts_it(rule):
    assert rule.title and rule.trigger
    assert rule.position != "main" or rule.steps[0].phase == "confirm"  # the main rule first asks if the alarm is still true; one placed before or after it is an extra look


def test_unreached_steps_keep_their_blanks():
    rule = next(r for r in RULES if r.id == "interface-down")
    shown = json.dumps(engine.rule_map(rule, {"device": "s1", "interface": "Ethernet4"}))
    assert "{peer_device}" in shown and "Is unknown" not in shown


def test_scenarios_endpoint_lists_every_rule():
    from http.server import ThreadingHTTPServer

    from ts import web

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(REPO / "rules"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", "/api/scenarios")
        rows = json.loads(conn.getresponse().read())
        conn.close()
        assert {r["id"] for r in rows} == {r.id for r in RULES} and all(r["title"] for r in rows)
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("device,platform,source,expected", [("s4", "eos", "health", "config-drift-routers"), ("r4", "frr", "health", "config-drift-routers"), ("s4", "eos", "trap", "unexpected-config-change")])
def test_drift_the_sweep_finds_is_put_back_on_routers_and_switches(device, platform, source, expected):
    chain = engine.chain_for(RULES, {"event": "config.changed", "device": device, "role": "core", "platform": platform, "source": source, "subject": "config"})
    assert [r.id for r in chain if r.position == "main"] == [expected]
