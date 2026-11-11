"""The return routes a border router's startup script adds outside FRR."""

from __future__ import annotations

import pytest

from ts import sweep
from ts.blocks import actions, services

SCRIPT = """#!/bin/bash
ip route replace default via 20.20.20.1 dev eth1 2>/dev/null || true
ip -6 route replace default via 2001:db8:20::1 dev eth1 2>/dev/null || true
ip -6 route replace 2001:db8:10::/64 via 2001:db8:100:33::1 dev eth3 2>/dev/null || true
ip -6 route replace 2001:db8:30::/64 via 2001:db8:100:33::1 dev eth3 2>/dev/null || true
iptables -t nat -F
"""
TEN = "2001:db8:10::/64 via 2001:db8:100:33::1 dev eth3 metric 1024 pref medium"
THIRTY = "2001:db8:30::/64 via 2001:db8:100:33::1 dev eth3 metric 1024 pref medium"
UP = "5: eth3@if9: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500"
DOWN = "5: eth3@if9: <BROADCAST,MULTICAST> mtu 1500"


@pytest.fixture
def device(monkeypatch):
    """A fake border router: tests set its routing table and port state."""
    state = {"script": SCRIPT, "table": f"{TEN}\n{THIRTY}", "link": UP, "script_rc": 0, "changes": [], "refuse": False}

    def shell(_device, argv, timeout=20):
        shown = " ".join(argv)
        if argv[0] == "cat":
            return shown, state["script"], state["script_rc"]
        if argv[:3] == ["ip", "-6", "route"]:
            return shown, state["table"], 0
        return shown, state["link"], 0

    def run_change(_device, argv):
        state["changes"].append(argv)
        return " ".join(argv), "Error: refused" if state["refuse"] else "", 1 if state["refuse"] else 0

    monkeypatch.setattr(services.lab, "shell", shell)
    monkeypatch.setattr(actions.lab, "run_change", run_change)
    return state


def test_default_routes_are_left_to_frr(device):
    _ev, routes = services.boot_routes("r3")
    assert routes == [("2001:db8:10::/64", "2001:db8:100:33::1", "eth3"), ("2001:db8:30::/64", "2001:db8:100:33::1", "eth3")]


def test_all_there(device):
    assert services.border_routes_present(None, "r3").outcome == "present"


def test_a_copy_installed_by_frr_counts(device):
    device["table"] = "2001:db8:10::/64 nhid 30 via 2001:db8:100:33::1 dev eth3 proto static metric 20 pref medium\n" + THIRTY
    assert services.border_routes_present(None, "r3").outcome == "present"


def test_a_route_through_the_wrong_gateway_does_not_count(device):
    device["table"] = "2001:db8:10::/64 via 2001:db8:20::1 dev eth1 metric 1024\n" + THIRTY
    assert services.border_routes_present(None, "r3").outcome == "missing"


def test_missing_with_the_port_up(device):
    device["table"] = TEN
    result = services.border_routes_present(None, "r3")
    assert result.outcome == "missing" and "2001:db8:30::/64" in result.saw


def test_missing_with_the_port_down_is_the_port_fault(device):
    device["table"], device["link"] = "", DOWN
    assert services.border_routes_present(None, "r3").outcome == "interface_down"


def test_an_unreadable_script_is_not_bad_news(device):
    device["script"], device["script_rc"] = "cat: no such file", 1
    assert services.border_routes_present(None, "r3").outcome == "unreadable"
    with pytest.raises(RuntimeError):
        sweep._return_routes("r3")


def test_sweep_only_raises_it_when_the_port_could_carry_them(device):
    device["table"], device["link"] = "", DOWN
    assert sweep._return_routes("r3")[1][0].ok is True
    device["link"] = UP
    finding = sweep._return_routes("r3")[1][0]
    assert finding.ok is False and finding.event["event"] == "routes.missing"


def test_repair_adds_only_what_is_missing_and_never_runs_the_script(device):
    device["table"] = TEN
    result = actions.restore_border_routes(None, "r3")
    assert result.outcome == "done"
    assert device["changes"] == [["ip", "-6", "route", "replace", "2001:db8:30::/64", "via", "2001:db8:100:33::1", "dev", "eth3"]]


def test_repair_reports_a_refusal(device):
    device["table"], device["refuse"] = "", True
    result = actions.restore_border_routes(None, "r3")
    assert result.outcome == "failed" and "2001:db8:10::/64" in result.saw


def test_repair_changes_nothing_when_the_script_is_unreadable(device):
    device["script"], device["script_rc"] = "", 1
    assert actions.restore_border_routes(None, "r3").outcome == "failed"
    assert device["changes"] == []
