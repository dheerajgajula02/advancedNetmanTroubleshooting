"""Collection campaigns: planned one at a time, spaced out, and stoppable."""

import pytest

from drills import campaign, scenarios


@pytest.fixture
def world(tmp_path, monkeypatch):
    state = tmp_path / "c.json"
    monkeypatch.setattr(campaign, "STATE", state)
    monkeypatch.setattr(campaign, "_why_not_now", lambda: None)
    monkeypatch.setattr(campaign.history, "ai_sessions_lately", lambda *_a, **_k: 0)
    live = {"now": None}
    monkeypatch.setattr(campaign.scenarios, "current", lambda: live["now"])

    def stage(which):
        live["now"] = {"id": which}
    monkeypatch.setattr(campaign.scenarios, "stage", stage)
    monkeypatch.setattr(campaign.scenarios, "attach", lambda _n: None)
    numbers = iter(range(1, 50))
    monkeypatch.setattr(campaign.tickets, "create", lambda *_a: next(numbers))
    tickets = {}
    monkeypatch.setattr(campaign.tickets, "get", lambda n: tickets.get(n))
    return live, tickets


FIRST, SECOND = scenarios.SCENARIOS[0]["id"], scenarios.SCENARIOS[1]["id"]


def test_a_campaign_needs_a_name_situations_and_sensible_rounds(world):
    with pytest.raises(ValueError, match="Name"):
        campaign.start("Bad Name!", [FIRST], 1)
    with pytest.raises(ValueError, match="at least one"):
        campaign.start("a", ["nope"], 1)
    with pytest.raises(ValueError, match="Rounds"):
        campaign.start("a", [FIRST], 0)


def test_only_one_campaign_runs_at_a_time(world):
    campaign.start("a", [FIRST], 1)
    with pytest.raises(ValueError, match="already running"):
        campaign.start("b", [FIRST], 1)


def test_it_works_through_them_one_at_a_time(world):
    live, tickets = world
    campaign.start("a", [FIRST, SECOND], 1)
    campaign.tick()
    plan = campaign.listing()[0]["plan"]
    assert [i["status"] for i in plan] == ["staged", "waiting"]
    campaign.tick()  # still being worked: nothing else starts
    assert [i["status"] for i in campaign.listing()[0]["plan"]] == ["staged", "waiting"]
    tickets[1] = {"status": "solved", "run_id": "run-1"}
    live["now"] = None
    campaign.tick()
    plan = campaign.listing()[0]["plan"]
    assert plan[0]["status"] == "done" and plan[0]["result"] == "solved" and plan[0]["run_id"] == "run-1"
    campaign.tick()
    assert campaign.listing()[0]["plan"][1]["status"] == "staged"


def test_it_finishes_when_nothing_is_left(world):
    live, tickets = world
    campaign.start("a", [FIRST], 1)
    campaign.tick()
    tickets[1] = {"status": "needs_agent", "run_id": ""}
    live["now"] = None
    campaign.tick()
    campaign.tick()
    assert campaign.listing()[0]["state"] == "done"


def test_the_same_situation_waits_its_turn_again(world):
    live, tickets = world
    campaign.start("a", [FIRST], 2)
    campaign.tick()
    tickets[1] = {"status": "solved", "run_id": "r"}
    live["now"] = None
    campaign.tick()
    campaign.tick()
    assert campaign.listing()[0]["plan"][1]["status"] == "waiting"


def test_it_waits_while_the_ai_has_used_its_hour(world, monkeypatch):
    campaign.start("a", [FIRST], 1)
    monkeypatch.setattr(campaign.history, "ai_sessions_lately", lambda *_a, **_k: campaign.agent.MAX_SESSIONS_PER_HOUR)
    campaign.tick()
    assert campaign.listing()[0]["plan"][0]["status"] == "waiting"


def test_stopping_skips_what_has_not_started(world):
    made = campaign.start("a", [FIRST, SECOND], 1)
    campaign.tick()
    campaign.stop(made["id"])
    found = campaign.listing()[0]
    assert found["state"] == "stopped" and [i["status"] for i in found["plan"]] == ["staged", "skipped"]
