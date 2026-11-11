import pytest

from tests.conftest import REPO, diag, fix, rules
from ts.lint import errors, lint
from ts.ruleset import load_ruleset


def messages(issues, level="error"):
    return [i.message for i in issues if i.level == level]


def test_shipped_rules_are_clean():
    rs = load_ruleset(REPO / "rules")
    assert rs.errors == []
    assert lint(rs.parsed) == []


def test_unbound_placeholder():
    bad = diag(steps=[{"check": "interface_status {peer_device} {interface}"}])
    assert any("peer_device" in m for m in messages(lint(rules(bad))))


def test_produced_values_are_bound_after_their_step():
    ok = diag(steps=[{"check": "topology_peer {device} {interface}", "then": {"none": "stop no peer"}},
                     {"check": "interface_status {peer_device} {peer_interface}"}])
    assert errors(lint(rules(ok))) == []


def test_event_fields_depend_on_the_event():
    bad = diag(event="config.changed")  # config.changed carries no interface
    assert any("interface" in m for m in messages(lint(rules(bad))))


def test_jump_skipping_a_producer_unbinds_its_values():
    bad = diag(steps=[
        {"check": "interface_status {device} {interface}", "then": {"up": "jump far"}},
        {"check": "topology_peer {device} {interface}"},
        {"id": "far", "check": "interface_status {peer_device} {peer_interface}"},
    ])
    assert any("peer_device" in m for m in messages(lint(rules(bad))))


def test_backward_jump():
    bad = diag(steps=[{"id": "a", "check": "interface_status {device} {interface}"},
                      {"check": "device_up {device}", "then": {"down": "jump a"}}])
    assert any("jumps back" in m for m in messages(lint(rules(bad))))


def test_goto_loop_and_missing_target():
    a = diag("loop-a", steps=[{"check": "interface_status {device} {interface}", "then": {"down": "goto loop-b"}}])
    b = diag("loop-b", event="interface.up",
             steps=[{"check": "interface_status {device} {interface}", "then": {"down": "goto loop-a"}}])
    msgs = messages(lint(rules(a, b)))
    assert any("goto loop" in m for m in msgs)
    lone = diag("lonely", steps=[{"check": "interface_status {device} {interface}", "then": {"down": "goto nowhere"}}])
    assert any("no such rule" in m for m in messages(lint(rules(lone))))


def test_goto_needs_the_targets_event_fields():
    target = diag("needs-iface")
    caller = diag("from-config", event="config.changed",
                  steps=[{"check": "changed_sections {device}", "then": {"changed": "goto needs-iface"}}])
    assert any("needs {interface}" in m for m in messages(lint(rules(target, caller))))


@pytest.mark.parametrize("data, needle", [
    (diag(match={"event": "interface.down", "device": "r5"}), "ISP router"),
    (diag(match={"event": "interface.down", "role": "isp"}), "ISP router"),
    (diag(steps=[{"check": "interface_status r5 Ethernet1"}]), "ISP router"),
    (diag(match={"event": "interface.melted"}), "matches no known event"),
    (diag(match={"event": "interface.down", "cause": "x.y"}), "cannot match on cause"),
])
def test_match_and_device_rules(data, needle):
    assert any(needle in m for m in messages(lint(rules(data))))


def test_management_interfaces_and_forbidden_lines():
    mgmt = fix(action={"do": {"name": "config_lines", "device": "{device}",
                              "lines": ["interface Management0", "shutdown"]}})
    reload = fix(action={"do": {"name": "config_lines", "device": "{device}", "lines": ["reload now"]}})
    issues = lint(rules(diag(), mgmt)) + lint(rules(diag(), reload))
    msgs = messages(issues)
    assert any("management interface" in m for m in msgs)
    assert any("not allowed" in m for m in msgs)


def test_overlap_at_same_priority():
    a = diag("first-rule")
    b = diag("second-rule")
    assert any("overlaps" in m for m in messages(lint(rules(a, b))))
    b["priority"] = 60
    assert not any("overlaps" in m for m in messages(lint(rules(a, b))))
    c = diag("third-rule", match={"event": "interface.down", "role": "border"})
    d = diag("fourth-rule", match={"event": "interface.down", "role": "access"})
    assert not any("overlaps" in m for m in messages(lint(rules(c, d))))


def test_fix_rules():
    assert any("high-risk" in m for m in messages(lint(rules(diag(), fix(risk="high", mode="active")))))
    dangling = lint(rules(fix(cause="nothing.makes_this")))
    assert any("can never run" in m for m in messages(dangling, "warning"))
    no_window = fix(require=[])
    assert any("Jenkins" in m for m in messages(lint(rules(diag(), no_window)), "warning"))


def test_fix_placeholders_come_from_the_cause():
    producer = diag(steps=[{"check": "interface_status {device} {interface}",
                            "then": {"admin_down": "cause config.drift"}}])  # sets no {section}
    uses_section = fix(cause="config.drift", action={"do": "restore_golden_section {device} {section}"},
                       verify=["known_good_match {device} {section} == match"])
    assert any("section" in m for m in messages(lint(rules(producer, uses_section))))
    producer["steps"][0]["then"]["admin_down"] = "cause config.drift section=interfaces"
    assert errors(lint(rules(producer, uses_section))) == []


def test_templated_cause_matches_concrete_fix():
    producer = diag(event="ospf.neighbor_down", steps=[
        {"check": "topology_peer {device} {interface}", "then": {"none": "stop no peer"}},
        {"check": "ospf_params_match {device} {interface} {peer_device} {peer_interface}",
         "then": {"mismatch": "cause ospf.{param}_mismatch"}}])
    mtu = fix(cause="ospf.mtu_mismatch", action={"do": "escalate fix_mtu_by_hand"}, verify=["change_window_open == closed"])
    assert not any("can never run" in m for m in messages(lint(rules(producer, mtu)), "warning"))


def test_platform_warning():
    vrrp_anywhere = diag(event="vrrp.master_change",
                         steps=[{"check": "vrrp_split_brain {device} {interface}"}])
    assert any("does not support frr" in m for m in messages(lint(rules(vrrp_anywhere)), "warning"))
    vrrp_access = diag(event="vrrp.master_change", match={"event": "vrrp.master_change", "role": "access"},
                       steps=[{"check": "vrrp_split_brain {device} {interface}"}])
    assert not messages(lint(rules(vrrp_access)), "warning")
