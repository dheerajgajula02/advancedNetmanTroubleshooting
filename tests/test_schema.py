import pytest

from tests.conftest import diag, fix, rules
from ts.schema import ActionCall, CheckCall, Condition, FixRule, Outcome, RuleLoadError, parse_rule


def test_compact_and_mapping_calls_agree():
    a = CheckCall.model_validate("ping {device} 8.8.8.8 source=Loopback0")
    b = CheckCall.model_validate({"name": "ping", "device": "{device}", "target": "8.8.8.8", "source": "Loopback0"})
    assert a.args == b.args == {"device": "{device}", "target": "8.8.8.8", "source": "Loopback0"}
    assert a.render() == "ping {device} 8.8.8.8 Loopback0"


@pytest.mark.parametrize("call, message", [
    ("nope {device}", "unknown check"),
    ("interface_status {device}", "missing argument"),
    ("ping a b c d", "too many values"),
    ({"name": "ping", "device": "r1", "target": "x", "color": "red"}, "has no argument"),
])
def test_bad_check_calls(call, message):
    with pytest.raises(ValueError, match=message):
        CheckCall.model_validate(call)


def test_list_params_need_mapping_form():
    with pytest.raises(ValueError, match="takes a list"):
        ActionCall.model_validate("config_lines r1 shutdown")
    ok = ActionCall.model_validate({"name": "config_lines", "device": "r1", "lines": ["interface Ethernet1", "shutdown"]})
    assert ok.args["lines"] == ["interface Ethernet1", "shutdown"]


def test_conditions():
    c = Condition.model_validate("ospf_neighbor_state {device} {neighbor_id} in [full, forming]")
    assert c.expect == ["full", "forming"]
    with pytest.raises(ValueError, match="never returns"):
        Condition.model_validate("interface_status {device} {interface} == sideways")
    with pytest.raises(ValueError, match="looks like"):
        Condition.model_validate("interface_status {device} {interface}")


@pytest.mark.parametrize("text, kind, target, params", [
    ("next", "next", "", {}),
    ("cause ospf.{param}_mismatch device={peer_device}", "cause", "ospf.{param}_mismatch", {"device": "{peer_device}"}),
    ("goto interface-down", "goto", "interface-down", {}),
    ("escalate no peer for {interface}", "escalate", "no peer for {interface}", {}),
])
def test_outcomes(text, kind, target, params):
    o = Outcome.model_validate(text)
    assert (o.kind, o.target, o.params) == (kind, target, params)


@pytest.mark.parametrize("text, message", [
    ("cause Broken", "must look like"),
    ("goto Not_A_Rule", "rule id"),
    ("stop", "needs a message"),
    ("teleport somewhere", "unknown outcome"),
])
def test_bad_outcomes(text, message):
    with pytest.raises(ValueError, match=message):
        Outcome.model_validate(text)


def test_table_picks_the_model_and_extra_keys_fail():
    assert isinstance(rules(fix())[0], FixRule)
    with pytest.raises(RuleLoadError, match="Extra inputs"):
        parse_rule(diag(colour="blue"))
    with pytest.raises(RuleLoadError, match="never returns"):
        parse_rule(diag(steps=[{"check": "interface_status {device} {interface}", "then": {"sideways": "next"}}]))
    with pytest.raises(RuleLoadError, match="id"):
        parse_rule(diag(rule_id="Bad ID"))


def test_end_cannot_continue():
    with pytest.raises(RuleLoadError, match="nothing after it"):
        parse_rule(diag(end="next"))
