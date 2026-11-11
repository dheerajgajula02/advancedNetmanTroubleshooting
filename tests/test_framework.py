"""Tags, where a rule sits in the order, the safety gate, and the review queue."""

from __future__ import annotations

import pytest
import yaml

from tests.conftest import diag, fix, rules
from ts import engine, history, review, rulebook, safety, store, tags
from ts.lab import Result
from ts.lint import errors, lint
from ts.registry import CHECKS
from ts.ruleset import load_ruleset
from ts.vocab import EVENT_TYPES


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite"
    monkeypatch.setattr(history, "DB", path)
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "incidents")
    return path


def test_the_tag_list_is_even_and_covers_every_event():
    assert {len(names) for names in tags.AREAS.values()} == {4}
    assert set(EVENT_TYPES) == set(tags.EVENT_TAGS)
    assert all(tags.is_known(tag) for kinds in tags.EVENT_TAGS.values() for tag in kinds)


def test_an_area_covers_its_tags_and_only_those():
    assert tags.covers(["routing"], ["routing/ospf"]) and tags.covers(["routing/ospf"], ["routing/ospf"])
    assert not tags.covers(["routing"], ["switching/vrrp"]) and not tags.covers(["routing/bgp"], ["routing/ospf"])
    assert "ospf.neighbor_down" in tags.events_for(["routing"]) and "dhcp.failure" not in tags.events_for(["routing"])


def test_every_installed_rule_has_tags_and_a_place(rules_dir):
    for rule in load_ruleset(rules_dir).parsed:
        assert rule.tags and (rule.position == "main" or rule.source.by == "ai"), rule.id  # only a suggested rule goes before or after the main one


def test_a_new_tag_must_fit_the_list(db):
    assert tags.name_problem("mtu", "link") is None
    assert "not an area" in tags.name_problem("mtu", "layer1")
    assert "already exists" in tags.name_problem("ospf", "routing")
    assert "variation" in tags.name_problem("ospf-down", "routing")
    assert "lowercase" in tags.name_problem("Big Tag", "link")


def _stub(monkeypatch, outcome_by_name):
    calls = []

    def make(name, outcome):
        def run(ctx, **kw):
            calls.append(name)
            return Result(outcome, f"{name} said {outcome}")
        return run

    for name, outcome in outcome_by_name.items():
        monkeypatch.setattr(CHECKS[name].__class__, "__hash__", CHECKS[name].__class__.__hash__, raising=False)
        object.__setattr__(CHECKS[name], "func", make(name, outcome))
    return calls


@pytest.fixture
def stubbed(monkeypatch):
    saved = {n: CHECKS[n].func for n in ("interface_status", "ping")}
    yield lambda **outcomes: _stub(monkeypatch, outcomes)
    for name, func in saved.items():
        object.__setattr__(CHECKS[name], "func", func)


EVENT = {"event": "interface.down", "device": "s1", "interface": "Ethernet1", "subject": "Ethernet1", "time": 1_800_000_000.0, "platform": "eos", "role": "access"}
MAIN = diag("the-main", mode="active", steps=[{"check": "interface_status {device} {interface}", "then": {"down": "cause interface.no_link"}}], causes={"interface.no_link": "No link."})


def extra(rule_id, position, cause="firewall.blocked", mode="active", match=None, outcome="reachable"):
    return diag(rule_id, mode=mode, position=position, match=match or {"tags": ["link/port"]}, causes={cause: "It was blocked."}, tags=["link/port"],
                steps=[{"check": "ping {device} 10.0.0.1", "then": {outcome: f"cause {cause}"}}])


def test_order_is_beginning_then_main_then_end():
    chain = engine.chain_for(rules(extra("at-end", "end"), MAIN, extra("at-start", "beginning")), EVENT)
    assert [(r.id, r.position) for r in chain] == [("at-start", "beginning"), ("the-main", "main"), ("at-end", "end")]


def test_a_rule_only_runs_for_its_tags():
    assert [r.id for r in engine.chain_for(rules(extra("other", "end", match={"tags": ["routing/ospf"]}), MAIN), EVENT)] == ["the-main"]


def test_proposed_and_retired_rules_never_run():
    assert [r.id for r in engine.chain_for(rules(extra("rule-a", "end", mode="proposed"), extra("rule-b", "end", mode="retired"), MAIN), EVENT)] == ["the-main"]


def test_a_rule_at_the_beginning_can_settle_it_before_the_main_rule_runs(stubbed):
    calls = stubbed(interface_status="down", ping="reachable")
    rec = engine.run_chain(engine.chain_for(rules(MAIN, extra("first", "beginning")), EVENT), EVENT)
    assert rec["result"]["label"] == "firewall.blocked" and rec["rule"]["id"] == "first" and calls == ["ping"]


def test_a_rule_at_the_end_only_runs_when_the_main_rule_ran_out(stubbed):
    calls = stubbed(interface_status="up", ping="reachable")
    rec = engine.run_chain(engine.chain_for(rules(MAIN, extra("last", "end")), EVENT), EVENT)
    assert rec["rule"]["id"] == "last" and calls == ["interface_status", "ping"]
    assert [t["rule"] for t in rec["tried"]] == ["the-main"]


def test_a_rule_at_the_end_does_not_run_once_there_is_a_cause(stubbed):
    calls = stubbed(interface_status="down", ping="reachable")
    assert engine.run_chain(engine.chain_for(rules(MAIN, extra("last", "end")), EVENT), EVENT)["rule"]["id"] == "the-main" and calls == ["interface_status"]


def test_a_rule_in_shadow_is_recorded_but_decides_nothing(stubbed):
    stubbed(interface_status="down", ping="reachable")
    rec = engine.run_chain(engine.chain_for(rules(MAIN, extra("watching", "beginning", mode="shadow")), EVENT), EVENT)
    assert rec["rule"]["id"] == "the-main" and rec["tried"][0]["result"]["label"] == "firewall.blocked"


def test_nothing_further_runs_when_the_problem_was_not_confirmed(stubbed):
    calls = stubbed(interface_status="up", ping="reachable")
    confirm = diag("confirming", mode="active", steps=[{"phase": "confirm", "check": "interface_status {device} {interface}", "then": {"up": "stop Not confirmed."}}])
    rec = engine.run_chain(engine.chain_for(rules(confirm, extra("last", "end")), EVENT), EVENT)
    assert engine.cleared_at_confirm(rec) and calls == ["interface_status"]


def test_rules_at_the_edges_may_overlap_but_main_rules_may_not():
    assert not errors(lint(rules(MAIN, extra("rule-a", "end"), extra("rule-b", "end"))))
    assert errors(lint(rules(MAIN, diag("rival", mode="active"))))


def test_an_unknown_tag_is_an_error():
    assert any("not a tag" in i.message for i in errors(lint(rules(diag("x-y-z", match={"tags": ["made/up"]})))))
    assert any("not a tag" in i.message for i in errors(lint(rules(diag("x-y-z", tags=["made/up"])))))


@pytest.mark.parametrize("command", ["show run\nconfigure", "show ip route; reload", "show version | bash", "ip route flush all", "iptables -F", "rm -rf /", "cat /etc/shadow", "show tech-support", "tc qdisc del dev eth3 root", "sysctl -w net.ipv4.ip_forward=0", "ip link set eth3 down"])
def test_commands_that_are_not_plainly_read_only_are_refused(command):
    assert safety.read_problem(command)


@pytest.mark.parametrize("command", ["show ip route", "show ip ospf neighbor | json", "ip -6 route show", "iptables -t nat -S POSTROUTING", "tc qdisc show dev eth3", "sysctl net.ipv4.ip_forward", "ss -lntu"])
def test_ordinary_reading_is_allowed(command):
    assert safety.read_problem(command) is None


def test_pinging_the_isp_is_refused():
    assert "ISP" in safety.read_problem("show ip route 20.20.20.1")


@pytest.mark.parametrize("line", ["reload", "write erase", "shutdown", "no router ospf 1", "router bgp 65000", "username admin secret x", "interface Management0", "snmp-server community x", "no vlan 10", "neighbor 1.1.1.1 remote-as 1"])
def test_dangerous_config_is_refused(line):
    assert safety.config_problem("s1", [line])


def test_small_ordinary_config_is_allowed():
    assert safety.config_problem("s1", ["interface Ethernet3", "ip ospf cost 10"]) is None
    assert "at most" in safety.config_problem("s1", [f"line {n}" for n in range(21)])


def test_isp_router_is_refused_for_everything():
    assert safety.config_problem("r5", ["x"]) and safety.command_problem("r5", ["ip", "route", "add", "x"])
    with pytest.raises(ValueError):
        safety.read("r5", "show version")


@pytest.mark.parametrize("argv", [["ip", "link", "set", "eth0", "up"], ["ip", "link", "set", "eth3", "down"], ["ip", "route", "flush", "all"], ["iptables", "-F"], ["iptables", "-P", "FORWARD", "DROP"], ["sysctl", "-w", "kernel.panic=1"], ["bash", "-c", "x"], ["ip", "route", "del", "default", ";reboot"]])
def test_dangerous_commands_are_refused(argv):
    assert safety.command_problem("r3", argv)


@pytest.mark.parametrize("argv", [["iptables", "-D", "FORWARD", "-o", "eth1", "-j", "DROP"], ["ip", "-6", "route", "replace", "2001:db8:10::/64", "via", "2001:db8:100:33::1", "dev", "eth3"], ["ip", "link", "set", "eth3", "up"], ["sysctl", "-w", "net.ipv4.ip_forward=1"], ["tc", "qdisc", "del", "dev", "eth3", "root"]])
def test_narrow_repairs_are_allowed(argv):
    assert safety.command_problem("r3", argv) is None


def test_eos_devices_take_config_not_commands():
    assert "config lines" in safety.command_problem("s1", ["ip", "route", "add", "x"])


def test_secrets_never_leave_in_output():
    assert "hunter2" not in safety._redact("hostname s1\nusername admin secret hunter2\nsnmp-server community hunter2 ro")


# --- review -----------------------------------------------------------------

RULE_BODY = {"rules": [extra("ai-written", "end", cause="firewall.blocked", mode="proposed")], "position": "end"}


def test_nothing_applies_until_accepted(db, rules_dir):
    number = review.add("rule", "A rule", "because", RULE_BODY, print_="p1")
    assert review.get(number)["status"] == "pending" and review.counts()["pending"] == 1
    assert load_ruleset(rules_dir).get("ai-written") is None


def test_accepting_installs_the_rule_in_the_chosen_mode(db, rules_dir):
    review.accept(review.add("rule", "A rule", "because", RULE_BODY, print_="p1"), "me", "shadow", root=rules_dir)
    rule = load_ruleset(rules_dir).get("ai-written").rule
    assert rule.mode == "shadow" and rule.position == "end" and rule.source.approved_by == "me"
    assert yaml.safe_load((rules_dir / "diagnose" / "ai-written.yaml").read_text())["tags"] == ["link/port"]


def test_a_rule_that_stopped_being_valid_is_not_installed(db, rules_dir):
    bad = {"rules": [{**RULE_BODY["rules"][0], "match": {"tags": ["made/up"]}}]}
    number = review.add("rule", "Bad", "because", bad, print_="p2")
    with pytest.raises(rulebook.RuleRefused):
        review.accept(number, root=rules_dir)
    assert review.get(number)["status"] == "pending"


def test_a_rejection_needs_a_reason_and_keeps_it(db):
    number = review.add("rule", "Block the port", "because", RULE_BODY, print_="p3")
    with pytest.raises(ValueError):
        review.reject(number, " ")
    review.reject(number, "That port carries voice, never block it")
    row = review.get(number)
    assert row["status"] == "rejected" and row["reason"].startswith("That port")


def test_a_decision_cannot_be_made_twice(db):
    number = review.add("rule", "Twice", "because", RULE_BODY, print_="p4")
    review.reject(number, "not this one")
    with pytest.raises(review.NotPending):
        review.reject(number, "again please")


def test_the_same_suggestion_cannot_come_back(db):
    number = review.add("rule", "First wording", "because", RULE_BODY, print_="same")
    review.reject(number, "no thanks to this")
    with pytest.raises(review.Duplicate) as raised:
        review.add("rule", "Second wording", "other", RULE_BODY, print_="same")
    assert raised.value.row["reason"] == "no thanks to this"


def test_rejections_are_found_by_what_they_are_about(db):
    review.reject(review.add("rule", "Reset OSPF on flap", "OSPF flaps a lot", RULE_BODY, print_="a"), "resetting OSPF drops every neighbor")
    review.add("rule", "Reset OSPF again", "still waiting", RULE_BODY, print_="b")
    review.reject(review.add("tag", "New tag link/mtu", "mtu problems", {"tag": "link/mtu", "description": "x"}, print_="c"), "too narrow")
    found = review.rejected_like("OSPF neighbor keeps flapping on s3")
    assert [r["title"] for r in found] == ["Reset OSPF on flap"] and "drops every neighbor" in found[0]["reason"]


def test_accepting_a_tag_adds_it_and_tags_only_the_chosen_runs(db):
    history.add_run({"id": "old-1", "event": {"time": 1.0, "device": "s1", "subject": "x"}, "result": {"kind": "stop"}, "rule": {"id": "r"}, "steps": []})
    history.add_run({"id": "old-2", "event": {"time": 2.0, "device": "s2", "subject": "y"}, "result": {"kind": "stop"}, "rule": {"id": "r"}, "steps": []})
    body = {"tag": "link/mtu", "area": "link", "name": "mtu", "description": "MTU mismatch", "events": ["link.degraded"], "runs": [{"run_id": "old-1", "why": "a"}, {"run_id": "old-2", "why": "b"}]}
    outcome = review.accept(review.add("tag", "New tag link/mtu", "mtu", body, print_="t"), runs=["old-2"])
    assert outcome["tagged"] == 1 and history.tags_of("old-2") == ["link/mtu"] and history.tags_of("old-1") == []
    assert tags.is_known("link/mtu") and "link/mtu" in tags.for_event("link.degraded")


def test_past_runs_a_person_excluded_are_remembered(db):
    body = {"tag": "routing/ospf", "runs": [{"run_id": "run-9", "why": "x"}]}
    review.reject(review.add("backdate", "Add", "w", body, print_="b1"), "that run was BGP")
    assert review.rejected_runs("routing/ospf") == {"run-9"}


def test_rule_modes_can_be_changed_and_stay_valid(db, rules_dir):
    rulebook.set_mode(rules_dir, "interface-down", "retired", "me")
    assert load_ruleset(rules_dir).get("interface-down").rule.mode == "retired"
    with pytest.raises(rulebook.RuleRefused):
        rulebook.set_mode(rules_dir, "nope", "active", "me")


def _row(n, **kw):
    return {"id": f"run-{n}", "event": {"time": 1000.0 + n, "device": kw.get("device", "s1"), "subject": "x", "title": kw.get("title", f"thing {n}"), "event": "interface.down"},
            "result": {"kind": kw.get("kind", "cause"), "label": kw.get("label", "interface.no_link"), "text": "words", "step": 1}, "rule": {"id": "r"}, "steps": [{"phase": "investigate"}], "finished": 1100.0 + n}


def test_the_failures_list_pages_filters_and_sorts(tmp_path):
    root = tmp_path / "incidents"
    for n in range(30):
        store.save(_row(n, device="r3" if n % 3 == 0 else "s1"), root)
    tagged = {f"run-{n}": ["routing/ospf"] for n in range(10)}
    first = store.page({"size": "10"}, tagged, root)
    assert (first["total"], first["pages"], len(first["items"])) == (30, 3, 10) and first["items"][0]["id"] == "run-29"
    assert store.page({"size": "10", "page": "3"}, tagged, root)["items"][-1]["id"] == "run-0"
    assert store.page({"tag": "routing/ospf"}, tagged, root)["total"] == 10
    assert store.page({"tag": "routing"}, tagged, root)["total"] == 10
    assert store.page({"device": "r3"}, tagged, root)["total"] == 10
    assert store.page({"q": "thing 7"}, tagged, root)["total"] == 1
    assert store.page({"sort": "time", "dir": "asc", "size": "5"}, tagged, root)["items"][0]["id"] == "run-0"
    facets = store.page({"device": "r3"}, tagged, root)["facets"]
    assert facets["device"] == {"r3": 10, "s1": 20} and facets["tag"] == {"routing/ospf": 4}


def test_a_page_past_the_end_shows_the_last_page(tmp_path):
    root = tmp_path / "incidents"
    store.save(_row(1), root)
    assert store.page({"page": "9"}, {}, root)["page"] == 1
