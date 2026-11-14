"""Rules the AI wrote: the first look puts drift back, then the rules, then the AI checks before the AI fixes."""

from __future__ import annotations

import pytest

from ts import agent, aicheck, engine, fixer, history, pipeline, report
from ts.ruleset import load_ruleset
from tests.test_agent import Scripted, run, world  # noqa: F401  (world is a fixture)


def learned_run(**kw):
    rec = run(**kw)
    rec["learned"] = True
    rec["rule"]["source"] = "ai"
    rec["event"]["source"] = "monitor"
    return rec


def verdict(word, summary="It has stayed up for twelve minutes."):
    return ("verdict", {"verdict": word, "summary": summary, "evidence": "uptime 12m"})


@pytest.fixture
def turn(world, monkeypatch):  # noqa: F811
    """take_over with a scripted check and the support agent and report replaced by markers."""
    seen = {"agent": 0, "concluded": 0, "saved": 0}

    def support(rec, rules, log, root=None):
        seen["agent"] += 1
        rec["agent"] = {**rec["agent"], "status": "done", "outcome": "needs_person", "calls": [{"tool": "finish"}]}

    monkeypatch.setattr(agent, "take_over", support)
    monkeypatch.setattr(pipeline, "conclude", lambda rec, log=print: seen.__setitem__("concluded", seen["concluded"] + 1))
    monkeypatch.setattr(aicheck, "_DONE", [])

    def go(rec, *turns):
        model = Scripted(*turns)
        monkeypatch.setattr(aicheck.llm, "chat", model)
        rec.setdefault("agent", {"status": "queued", "why": "the rules found the cause, but no rule can repair it", "calls": [], "changes": [], "proposals": []})
        pipeline.take_over(rec, load_ruleset(world["root"]).parsed, lambda _m: None, world["root"], saved=lambda r: seen.__setitem__("saved", seen["saved"] + 1))
        return rec, model
    seen["go"] = go
    return seen


def test_the_check_is_only_for_alarms_on_rules_the_ai_wrote():
    assert aicheck.wanted(learned_run())
    assert not aicheck.wanted(run())
    ticket = learned_run()
    ticket["event"]["source"] = "ticket"
    assert not aicheck.wanted(ticket)  # a person who wrote in has already said it is broken


def test_working_means_the_support_agent_never_starts(turn):
    rec, model = turn["go"](learned_run(), [verdict("working")])
    assert turn["agent"] == 0 and turn["concluded"] == 1 and turn["saved"] >= 1
    assert rec["check"]["verdict"] == "working" and rec["check"]["status"] == "done"
    assert rec["agent"]["status"] == "not_needed" and "AI check found it working" in rec["agent"]["note"]
    assert "WHAT THE RULES FOUND" in model.sent[0][1]["content"] and "(written by the AI)" in model.sent[0][1]["content"]


def test_broken_hands_it_to_the_support_agent(turn):
    rec, _ = turn["go"](learned_run(), [verdict("broken", "It dropped again two minutes ago.")])
    assert turn["agent"] == 1 and rec["check"]["verdict"] == "broken" and rec["agent"]["status"] == "done"


def test_a_check_that_cannot_decide_hands_it_on(turn):
    rec, _ = turn["go"](learned_run(), [], [], [])
    assert turn["agent"] == 1 and rec["check"]["verdict"] == "unsure" and rec["check"]["status"] == "failed"


def test_a_dead_model_hands_it_on(turn):
    from ts import llm
    rec, _ = turn["go"](learned_run(), llm.LLMError("gone"))
    assert turn["agent"] == 1 and rec["check"]["verdict"] == "unsure" and "stopped answering" in rec["check"]["error"]


def test_the_check_can_only_read(turn):
    rec, model = turn["go"](learned_run(), [("try_command", {"device": "r3", "argv": ["reboot"]})], [verdict("working")])
    said = [m for m in model.sent[1] if m.get("role") == "tool"][0]["content"]
    assert "Only check_* tools" in said and turn["agent"] == 0
    names = {d["function"]["name"] for d in aicheck._definitions()}
    assert "verdict" in names and "show" in names and not {"try_command", "finish", "propose_rule"} & names


def test_the_support_agents_limits_still_apply_after_the_check(turn, monkeypatch):
    monkeypatch.setattr(agent, "blocked", lambda rec: "The same problem had the AI half an hour ago.")
    rec, _ = turn["go"](learned_run(), [verdict("broken")])
    assert turn["agent"] == 0 and rec["agent"]["status"] == "skipped" and "half an hour" in rec["agent"]["note"]


def test_a_check_already_answered_is_not_asked_again(turn):
    rec = learned_run()
    rec["check"] = {"status": "done", "verdict": "broken"}
    turn["go"](rec, )
    assert turn["agent"] == 1


def test_a_repair_the_ai_wrote_is_checked_and_still_reported_as_fixed(turn, monkeypatch):
    rec = learned_run(result={"kind": "cause", "label": "config.drift", "text": "x"})
    rec["repair"] = {"status": "fixed", "rule": "ai-repair"}
    rules = [type("R", (), {"id": "ai-repair", "source": type("S", (), {"by": "ai"})()})()]
    assert pipeline._learned_repair_worked(rec, rules)
    rec["agent"] = {"status": "queued", "why": "A repair the AI wrote ran.", "calls": [], "changes": [], "proposals": []}
    turn["go"](rec, [verdict("working")])
    assert "agent" not in rec and turn["agent"] == 0
    assert report.should_send(rec)  # the FIXED email still goes out


def test_working_sends_no_email_unless_it_keeps_coming_back(turn):
    rec, _ = turn["go"](learned_run(), [verdict("working")])
    assert not report.should_send(rec)


@pytest.mark.parametrize("learned, change_running, handed_over", [(True, False, True), (False, False, False), (True, True, False)])
def test_a_cause_whose_repair_only_watched_goes_to_the_check(world, monkeypatch, learned, change_running, handed_over):  # noqa: F811
    """Rules the AI wrote found the cause, but its repair is observe-only: nothing changed, so the AI checks it
    (live test 2026-11-14: r3 forwarding off was found, the repair only watched, and the run ended with r3 still broken)."""
    monkeypatch.setattr(fixer, "attempt", lambda rec, rules, log=print: {"status": "would_fix", "rule": "border-ip-forwarding-off-fix", "mode": "shadow"})
    monkeypatch.setattr(pipeline, "conclude", lambda rec, log=print: None)
    rec = learned_run(result={"kind": "cause", "label": "config.forwarding_disabled", "text": "r3 has IPv4 forwarding off"})
    rec["learned"] = learned
    rec["event"]["change_running"] = change_running
    pipeline.finish(rec, load_ruleset(world["root"]).parsed, lambda _m: None, inline=False, root=world["root"])
    assert (rec.get("agent") or {}).get("status") == ("queued" if handed_over else None)
    if handed_over:
        assert "observe-only" in rec["agent"]["why"]


def test_a_cause_only_a_person_can_fix_is_not_checked_for_a_watching_repair(world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fixer, "attempt", lambda rec, rules, log=print: {"status": "would_fix", "rule": "x", "mode": "shadow"})
    monkeypatch.setattr(pipeline, "conclude", lambda rec, log=print: None)
    rec = learned_run(result={"kind": "cause", "label": "link.physical_fault", "text": "the cable is cut"})
    pipeline.finish(rec, load_ruleset(world["root"]).parsed, lambda _m: None, inline=False, root=world["root"])
    assert "agent" not in rec


def test_finish_does_not_stop_a_learned_run_before_its_check(world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(agent, "blocked", lambda rec: pytest.fail("checked the limits before the AI check"))
    rec = learned_run()
    pipeline.finish(rec, load_ruleset(world["root"]).parsed, lambda _m: None, inline=False, root=world["root"])
    assert rec["agent"]["status"] == "queued"


# --- the first look, before the rules the AI wrote ------------------------------

def test_drift_goes_back_before_the_rules(monkeypatch):
    from ts import firstlook
    monkeypatch.setattr(firstlook, "take", lambda *a, **k: {"changed": {"drift": ["r3"], "summary": "r3 differs from known-good."}})
    monkeypatch.setattr(fixer, "find_rule", lambda rules, sub, values: "put-back")
    monkeypatch.setattr(fixer, "APPLY", lambda sub, rule, values, log: {"status": "fixed", "rule": rule, "device_seen": sub["values"]["device"]})
    forgot = []
    monkeypatch.setattr(firstlook, "forget_recent", lambda: forgot.append(1))
    out = pipeline.before_rules({"event": "monitor.fired", "device": "s4", "time": 1.0}, [], "this-run", lambda _m: None)
    assert out["drift"] == ["r3"] and out["repairs"][0]["status"] == "fixed" and out["repairs"][0]["device_seen"] == "r3" and forgot


def test_a_failed_put_back_does_not_stop_the_rules(monkeypatch):
    from ts import firstlook
    monkeypatch.setattr(firstlook, "take", lambda *a, **k: {"changed": {"drift": ["r3"]}})
    monkeypatch.setattr(fixer, "find_rule", lambda *a: 1 / 0)
    out = pipeline.before_rules({"event": "monitor.fired", "device": "s4", "time": 1.0}, [], "this-run", lambda _m: None)
    assert out["repairs"][0]["status"] == "failed" and "ZeroDivisionError" in out["repairs"][0]["why"]


# --- a watch-only rule decides nothing ------------------------------------------

def test_a_watch_only_rules_cause_does_not_become_the_runs(monkeypatch):
    rule = type("R", (), {"position": "pre", "mode": "shadow"})()

    def fake_run(r, event):
        return {"rule": {"id": "watcher"}, "mode": "shadow", "result": {"kind": "cause", "label": "link.flapping", "text": "It flaps."}, "steps": []}

    monkeypatch.setattr(engine, "run", fake_run)
    rec = engine.run_chain([rule], {"event": "monitor.fired"})
    assert rec["result"]["kind"] == "stop" and rec["result"]["text"] == engine.WATCH_ONLY and rec["watch_only_result"]["label"] == "link.flapping"
    assert history  # imported for the world fixture's history db
