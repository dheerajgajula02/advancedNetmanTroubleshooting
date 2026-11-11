"""The AI's turn: every change is a transaction, every step is recorded, and suggestions wait for a person."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from ts import agent, configs, engine, fixer, history, lab, llm, mcp, pipeline, report, review, safety, store, tools
from ts.lab import Result
from ts.registry import ACTIONS, CHECKS
from ts.ruleset import load_ruleset

NOW = 1_800_000_000.0


def run(result=None, steps=None, **kw):
    return {"id": "this-run", "event": {"event": "internet.unreachable", "time": NOW, "device": "h4", "subject": "internet", "title": "h4 cannot reach the internet", "internet_target": "8.8.8.8", "role": "host", "platform": "linux"},
            "result": result or {"kind": "escalate", "text": "Both borders reach the internet but h4 does not.", "step": "end"}, "rule": {"id": "internet-unreachable", "position": "main", "tags": []}, "steps": steps if steps is not None else [{"n": 1, "ask": "x?", "outcome": "y", "saw": "z", "phase": "investigate"}],
            "values": {}, "finished": NOW + 5, **kw}


@pytest.fixture
def world(tmp_path, monkeypatch, rules_dir):
    """A network that is only pretend: change_lock is free, health is whatever the test says, and nothing is reachable."""
    db = tmp_path / "history.sqlite"
    monkeypatch.setattr(history, "DB", db)
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "incidents")
    monkeypatch.setattr(tools, "AI_CHANGES_OFF", tmp_path / "ai-changes.off")
    monkeypatch.setattr(agent, "OFF_SWITCH", tmp_path / "ai.off")
    monkeypatch.setattr(fixer, "OFF_SWITCH", tmp_path / "converge.off")
    monkeypatch.setattr(fixer, "_job_in_the_way", lambda: None)
    monkeypatch.setattr(tools, "VERIFY_FOR", 0)
    monkeypatch.setattr(tools, "VERIFY_EVERY", 0)
    monkeypatch.setattr(tools, "SETTLE", 0)

    @contextmanager
    def free():
        yield

    monkeypatch.setattr(lab, "change_lock", free)
    state = {"health": [{"internet:h4"}, set()], "ran": [], "verify": ["unreachable", "reachable"]}

    def health():
        return set(state["health"].pop(0)) if len(state["health"]) > 1 else set(state["health"][0])

    monkeypatch.setattr(tools, "health", health)

    def run_change(device, argv):
        state["ran"].append((device, list(argv)))
        return f"{device}$ {' '.join(argv)}", "", 0

    monkeypatch.setattr(lab, "run_change", run_change)
    saved = CHECKS["ping"].func

    def ping(ctx, device, target, source=None):
        answer = state["verify"].pop(0) if len(state["verify"]) > 1 else state["verify"][0]
        return Result(answer, f"ping said {answer}")

    object.__setattr__(CHECKS["ping"], "func", ping)
    state["root"] = rules_dir
    yield state
    object.__setattr__(CHECKS["ping"], "func", saved)


def session(world, **kw):
    return tools.Session(run(**kw), load_ruleset(world["root"]).parsed, root=world["root"], model="deepseek-v4.1-flash")


VERIFY = {"check": "ping", "args": {"device": "h4", "target": "8.8.8.8"}, "expect": ["reachable"]}
DROP = {"device": "r3", "argv": ["iptables", "-D", "FORWARD", "-j", "DROP"], "undo_argv": ["iptables", "-I", "FORWARD", "-j", "DROP"], "why": "It blocks forwarding.", "verify": VERIFY, "was_working": "Yes, until 10:00.", "what_changed": "Nothing explains it."}


def try_it(world, s=None, **args):
    s = s or session(world)
    return s, tools.call(s, "try_command", {**DROP, **args})


def test_a_change_that_helps_stays(world):
    s, out = try_it(world)
    assert out["outcome"] == "kept" and out["healed"] == ["internet:h4"] and s.changes[0]["status"] == "kept"
    assert world["ran"] == [("r3", DROP["argv"])]


def test_a_change_whose_check_never_passes_comes_back_out(world):
    world["verify"] = ["unreachable"]
    s, out = try_it(world)
    assert out["outcome"] == "reverted" and world["ran"] == [("r3", DROP["argv"]), ("r3", DROP["undo_argv"])]
    assert s.changes[0]["steps"][0]["kind"] == "undo"


def test_a_change_that_breaks_something_else_comes_back_out(world):
    world["health"] = [{"internet:h4"}, {"bgp:r3:1.1.1.1"}]
    _s, out = try_it(world)
    assert out["outcome"] == "reverted" and out["new_problems"] == ["bgp:r3:1.1.1.1"] and world["ran"][-1] == ("r3", DROP["undo_argv"])


def test_a_change_that_makes_nothing_better_comes_back_out(world):
    world["health"] = [{"internet:h4"}, {"internet:h4"}]
    _s, out = try_it(world)
    assert out["outcome"] == "reverted" and "nothing that was wrong got better" in out["saw"]


def test_when_the_sweep_saw_nothing_wrong_the_check_decides(world):
    world["health"] = [set(), set()]
    assert try_it(world)[1]["outcome"] == "kept"


def test_a_check_that_already_passes_proves_nothing(world):
    world["verify"] = ["reachable"]
    _s, out = try_it(world)
    assert out["outcome"] == "refused" and "already answers" in out["saw"] and world["ran"] == []


def test_a_change_needs_a_real_check(world):
    _s, out = try_it(world, verify={"check": "made_up", "args": {}, "expect": ["x"]})
    assert out["outcome"] == "refused" and world["ran"] == []
    _s, out = try_it(world, verify={**VERIFY, "expect": ["banana"]})
    assert out["outcome"] == "refused"


def test_a_change_needs_its_undo(world):
    _s, out = try_it(world, undo_argv=[])
    assert out["outcome"] == "refused" and "undo" in out["saw"] and world["ran"] == []


@pytest.mark.parametrize("args", [{"device": "r5"}, {"argv": ["iptables", "-F"]}, {"argv": ["ip", "link", "set", "eth0", "up"]}, {"undo_argv": ["rm", "-rf", "/"]}, {"device": "s1"}])
def test_unsafe_changes_never_run(world, args):
    _s, out = try_it(world, **args)
    assert out["outcome"] == "refused" and world["ran"] == []


def test_the_switches_stop_changes(world):
    tools.AI_CHANGES_OFF.touch()
    _s, out = try_it(world)
    assert out["outcome"] == "refused" and world["ran"] == []
    tools.AI_CHANGES_OFF.unlink()
    fixer.OFF_SWITCH.touch()
    assert try_it(world)[1]["outcome"] == "refused"


def test_no_change_while_a_job_is_running(world, monkeypatch):
    monkeypatch.setattr(fixer, "_job_in_the_way", lambda: "A job is running.")
    assert try_it(world)[1]["saw"] == "A job is running."


def test_no_change_when_the_lock_is_taken(world, monkeypatch):
    @contextmanager
    def busy():
        raise lab.LockBusy("x")
        yield

    monkeypatch.setattr(lab, "change_lock", busy)
    _s, out = try_it(world)
    assert out["outcome"] == "refused" and world["ran"] == []


def test_changes_per_run_are_limited(world, monkeypatch):
    world["health"] = [{"internet:h4"}, set()] * 5
    world["verify"] = ["unreachable", "reachable"] * 5
    s = session(world)
    for _ in range(tools.MAX_CHANGES):
        assert try_it(world, s)[1]["outcome"] == "kept"
    assert "already made" in try_it(world, s)[1]["saw"]


def test_changes_per_hour_are_limited(world, monkeypatch):
    monkeypatch.setattr(history, "ai_changes_lately", lambda: tools.MAX_CHANGES_PER_HOUR)
    assert "last hour" in try_it(world)[1]["saw"]


def test_config_change_is_undone_from_a_snapshot(world, monkeypatch):
    world["verify"] = ["unreachable"]
    configured, config = [], {"text": ["interface Ethernet3", "   ip ospf cost 10"]}
    monkeypatch.setattr(tools.configs, "running", lambda device: ("shown", list(config["text"])))

    def configure(device, lines):
        configured.append(lines)
        config["text"] = ["interface Ethernet3", "   ip ospf cost 500"]
        return True, ""

    monkeypatch.setattr(lab, "configure", configure)
    monkeypatch.setattr(lab, "platform", lambda device: "eos")
    s = session(world)
    out = tools.call(s, "try_config", {"device": "s3", "lines": ["interface Ethernet3", "ip ospf cost 500"], "why": "x", "verify": VERIFY, "was_working": "yes", "what_changed": "nothing"})
    assert out["outcome"] == "reverted" and len(configured) == 2 and any("cost 10" in line for line in configured[1])


def test_dangerous_config_never_reaches_the_device(world, monkeypatch):
    monkeypatch.setattr(lab, "configure", lambda *a: pytest.fail("reached the device"))
    s = session(world)
    assert tools.call(s, "try_config", {"device": "s3", "lines": ["shutdown"], "why": "x", "verify": VERIFY, "was_working": "yes", "what_changed": "nothing"})["outcome"] == "refused"


def test_every_call_is_recorded_on_the_run_and_in_the_audit_table(world):
    s, _out = try_it(world)
    tools.call(s, "list_tags", {})
    assert [c["tool"] for c in s.calls] == ["try_command", "list_tags"] and s.calls[0]["kind"] == "change"
    with history._open() as db:
        rows = db.execute("SELECT tool, kind, outcome FROM audit ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("try_command", "change", "kept"), ("list_tags", "read", "ok")]


def test_a_broken_tool_is_an_answer_not_a_crash(world, monkeypatch):
    monkeypatch.setattr(tools, "_list_tags", lambda s, a: 1 / 0)
    s = session(world)
    assert tools.call(s, "show", {"device": "r5", "command": "show version"})["outcome"] == "refused"
    assert tools.call(s, "nonsense", {})["outcome"] == "invalid"
    assert tools.call(s, "show", {})["outcome"] == "invalid"
    assert tools.call(s, "show", None)["outcome"] == "invalid"


def test_calls_run_out_but_finish_still_works(world, monkeypatch):
    monkeypatch.setattr(tools, "MAX_READS", 1)
    s = session(world)
    assert tools.call(s, "list_tags", {})["reads_left"] == 0
    assert tools.call(s, "list_tags", {})["outcome"] == "refused"
    assert tools.call(s, "finish", {"outcome": "needs_person", "root_cause": "x", "summary": "x", "assessment": "unknown", "why": "x", "next": "x"})["outcome"] == "done"


def test_reading_can_never_use_up_the_chance_to_act(world, monkeypatch):
    """A real drill: the AI found the cause after 45 reads, and its fix was then refused for want of calls."""
    monkeypatch.setattr(tools, "MAX_READS", 2)
    s = session(world)
    for _ in range(3):
        tools.call(s, "list_tags", {})
    assert try_it(world, s)[1]["outcome"] == "kept"
    s.shown[("r3", "iptables -S FORWARD")] = "-A FORWARD -o eth1 -j DROP"
    assert tools.call(s, "propose_rule", rule_args())["outcome"] == "saved"


def test_changes_and_suggestions_run_out_too(world, monkeypatch):
    monkeypatch.setattr(tools, "MAX_ACTS", 1)
    s = session(world)
    tools.call(s, "propose_tag", {"name": "a", "area": "link", "description": "x", "why": "x"})
    assert "changes and suggestions" in tools.call(s, "propose_tag", {"name": "mtu", "area": "link", "description": "x", "why": "x"})["saw"]


def test_a_second_alarm_starts_from_what_the_first_found(world):
    first = run(agent={"status": "done", "outcome": "needs_person", "root_cause": "A filter on r3 drops forwarded traffic.", "changes": [{"status": "reverted", "what": ["iptables -D FORWARD"]}], "calls": [], "proposals": []})
    first["id"], first["event"] = "first-run", {**first["event"], "device": "h1"}
    store.save(first, store.DATA_DIR)
    store._cache.clear()
    second = run()
    text = agent.brief(second)
    assert "ANOTHER AI RUN JUST WORKED" in text and "A filter on r3 drops forwarded traffic." in text and "reverted: iptables -D FORWARD" in text


def test_an_old_finding_is_not_handed_on(world):
    first = run(agent={"status": "done", "outcome": "fixed", "root_cause": "Something long ago.", "changes": [], "calls": [], "proposals": []})
    first["id"], first["event"] = "old-run", {**first["event"], "time": NOW - 86400}
    store.save(first, store.DATA_DIR)
    store._cache.clear()
    assert "ANOTHER AI RUN" not in agent.brief(run())


def test_it_cannot_claim_a_fix_it_did_not_make(world):
    s = session(world)
    assert tools.call(s, "finish", {"outcome": "fixed", "root_cause": "x", "summary": "x", "assessment": "unknown", "why": "x", "next": "x"})["outcome"] == "invalid" and s.result is None


def test_there_is_a_tool_for_every_check():
    names = tools.registry()
    assert all(f"check_{name}" in names for name in CHECKS)
    assert all(names[f"check_{name}"].kind == "read" for name in CHECKS)
    assert {"action_converge_config", "action_restore_nat", "try_config", "try_command", "propose_rule", "propose_tag", "propose_backdate", "finish"} <= set(names)


def test_a_check_tool_refuses_the_isp_router(world):
    assert tools.call(session(world), "check_ping", {"device": "r5", "target": "1.1.1.1"})["outcome"] == "error"


# --- suggesting a rule --------------------------------------------------------

def rule_args(**kw):
    return {"id": "forward-blocked", "title": "A firewall rule blocks forwarding on r3", "description": "Look for a DROP in the forward chain.", "why": "The normal checks do not read the firewall.",
            "trigger": {"tags": ["service/internet"]}, "position": "end",
            "steps": [{"ask": "Is forwarding blocked on r3?", "check": "output_has", "args": {"device": "r3", "command": "iptables -S FORWARD", "text": "-j DROP"}, "then": {"found": "cause firewall.forward_blocked device=r3"}}],
            "causes": {"firewall.forward_blocked": "{device} has a firewall rule dropping forwarded traffic."},
            "fix": {"title": "Remove the rule", "action": "linux_change", "args": {"device": "{device}", "argv": ["iptables", "-D", "FORWARD", "-j", "DROP"], "undo_argv": ["iptables", "-I", "FORWARD", "-j", "DROP"]},
                    "verify": [{"check": "ping", "args": {"device": "h4", "target": "8.8.8.8"}, "expect": ["reachable"]}], "risk": "low"}, **kw}


def having_seen(world, output="-P FORWARD ACCEPT\n-A FORWARD -o eth1 -j DROP"):
    s = session(world)
    s.shown[("r3", "iptables -S FORWARD")] = output
    s.observed[tools._key("ping", {"device": "h4", "target": "8.8.8.8"})] = {"outcome": "unreachable", "values": {}}
    return s


def test_a_good_rule_is_saved_with_its_proof(world):
    s = having_seen(world)
    out = tools.call(s, "propose_rule", rule_args())
    assert out["outcome"] == "saved", out
    row = review.get(s.proposals[0])
    assert row["status"] == "pending" and row["proof"]["replay"]["reached"] == "firewall.forward_blocked" and row["run_id"] == "this-run"
    assert [r["table"] for r in row["body"]["rules"]] == ["diagnose", "fix"]
    assert load_ruleset(world["root"]).get("forward-blocked") is None  # it waits for a person


def test_an_accepted_rule_runs_without_the_ai(world, monkeypatch):
    s = having_seen(world)
    tools.call(s, "propose_rule", rule_args())
    review.accept(s.proposals[0], "me", "active", root=world["root"])
    rules = load_ruleset(world["root"]).parsed
    chain = [r.id for r in engine.chain_for(rules, run()["event"]) if r.source.by != "ai" or r.id == "forward-blocked"]  # whatever else has been accepted by now
    assert chain == ["internet-unreachable", "forward-blocked"]
    assert fixer.find_rule(rules, {"result": {"label": "firewall.forward_blocked"}}, {"device": "r3"}).id == "forward-blocked-fix"


def test_a_suggested_fix_is_only_picked_where_it_can_work(world):
    """From real suggestions: a Linux-level fix matched by its cause alone would also be picked for switches, which answer unsupported."""
    fix_rule = tools._build(session(world), rule_args())[1]
    assert fix_rule["match"] == {"cause": ["firewall.forward_blocked"], "platform": ["frr", "linux"]}
    on_its_own_device = fix_with(verify=[{"check": "link_loss", "args": {"device": "{device}", "interface": "eth3", "target": "10.0.33.1"}, "expect": ["clean"]}])
    assert tools._build(session(world), on_its_own_device)[1]["match"]["platform"] == ["frr"]


def test_a_rule_must_be_able_to_reach_its_cause_on_what_was_seen(world):
    s = having_seen(world, output="-P FORWARD ACCEPT")
    out = tools.call(s, "propose_rule", rule_args())
    assert out["outcome"] == "invalid" and ("would not reach" in out["saw"] or "runs off the end" in out["saw"]), out
    assert review.counts()["pending"] == 0


def test_a_rule_cannot_use_steps_that_were_never_run(world):
    out = tools.call(session(world), "propose_rule", rule_args())
    assert out["outcome"] == "invalid" and "never ran" in out["saw"]


def fix_with(**change):
    args = rule_args()
    args["fix"] = {**args["fix"], **change}
    return args


def test_a_fix_must_act_where_the_fault_was_found(world):
    """From a real suggestion: it could find the fault on r3 or r4, but always deleted the line on r4."""
    out = tools.call(having_seen(world), "propose_rule", fix_with(args={"device": "r4", "argv": ["iptables", "-D", "FORWARD", "-j", "DROP"], "undo_argv": ["iptables", "-I", "FORWARD", "-j", "DROP"]}))
    assert out["outcome"] == "invalid" and "always runs on r4" in out["saw"]


def test_a_fix_cannot_be_judged_by_a_check_that_never_ran(world):
    out = tools.call(having_seen(world), "propose_rule", fix_with(verify=[{"check": "ping", "args": {"device": "{device}", "target": "8.8.8.8"}, "expect": ["reachable"]}]))
    assert out["outcome"] == "invalid" and "ping r3 8.8.8.8" in out["saw"] and "alarm_device" in out["saw"]


def test_a_fix_cannot_be_judged_by_a_check_that_was_already_passing(world):
    """From the same suggestion: a border router pinging for itself answers reachable whether or not it forwards anything."""
    s = having_seen(world)
    s.observed[tools._key("ping", {"device": "r3", "target": "8.8.8.8"})] = {"outcome": "reachable", "values": {}}
    out = tools.call(s, "propose_rule", fix_with(verify=[{"check": "ping", "args": {"device": "{device}", "target": "8.8.8.8"}, "expect": ["reachable"]}]))
    assert out["outcome"] == "invalid" and "proves nothing" in out["saw"]


def test_a_fix_can_be_judged_from_where_the_alarm_came_from(world):
    out = tools.call(having_seen(world), "propose_rule", fix_with(verify=[{"check": "check_ping", "args": {"device": "{alarm_device}", "target": "8.8.8.8"}, "expect": ["reachable"]}]))
    assert out["outcome"] == "saved", out


def test_a_change_may_name_its_check_either_way(world):
    assert try_it(world, verify={**VERIFY, "check": "check_ping"})[1]["outcome"] == "kept"


def test_a_rule_using_a_check_replays_what_the_check_said(world):
    s = session(world)
    tools.call(s, "check_ping", {"device": "h4", "target": "8.8.8.8"})
    out = tools.call(s, "propose_rule", rule_args(fix=None, steps=[{"ask": "Does h4 reach out?", "check": "ping", "args": {"device": "{device}", "target": "{internet_target}"}, "then": {"unreachable": "cause firewall.forward_blocked device=r3"}}]))
    assert out["outcome"] == "saved", out


@pytest.mark.parametrize("change,word", [({"position": "main"}, "position"), ({"id": "Bad Id"}, "lowercase"), ({"trigger": {"tags": []}}, "tag"), ({"trigger": {"tags": ["made/up"]}}, "not a tag"), ({"id": "interface-down"}, "already exists"), ({"steps": [{"ask": "Anything here?", "check": "made_up", "args": {}}]}, "registered")])
def test_a_broken_rule_comes_back_with_the_reason(world, change, word):
    out = tools.call(having_seen(world), "propose_rule", rule_args(**change))
    assert out["outcome"] == "invalid" and word in out["saw"], out


def test_a_rule_cannot_read_with_a_command_that_is_not_read_only(world):
    s = having_seen(world)
    s.shown[("r3", "iptables -F")] = "x"
    args = rule_args(fix=None)
    args["steps"][0]["args"]["command"] = "iptables -F"
    assert tools.call(s, "propose_rule", args)["outcome"] == "invalid"


def test_a_fix_cannot_carry_a_dangerous_command(world):
    args = rule_args()
    args["fix"]["args"]["argv"] = ["iptables", "-F"]
    assert tools.call(having_seen(world), "propose_rule", args)["outcome"] == "invalid"


def test_a_rejected_rule_is_refused_even_under_another_name(world):
    s = having_seen(world)
    tools.call(s, "propose_rule", rule_args())
    review.reject(s.proposals[0], "Do not touch the firewall automatically")
    again = tools.call(having_seen(world), "propose_rule", rule_args(id="another-name", title="Something else entirely"))
    assert again["outcome"] == "refused" and "Do not touch the firewall" in again["saw"]


def test_a_pending_rule_is_not_suggested_twice(world):
    tools.call(having_seen(world), "propose_rule", rule_args())
    assert "already suggested" in tools.call(having_seen(world), "propose_rule", rule_args(id="same-again"))["saw"]


# --- tags --------------------------------------------------------------------

def old_run(world, run_id="old-run"):
    history.add_run({"id": run_id, "event": {"time": 1.0, "device": "s1", "subject": "x"}, "result": {"kind": "stop"}, "rule": {"id": "r"}, "steps": []})


def test_a_new_tag_is_suggested_with_the_runs_it_goes_on(world):
    old_run(world)
    s = session(world)
    out = tools.call(s, "propose_tag", {"name": "mtu", "area": "link", "description": "MTU mismatch", "why": "No tag covers it.", "runs": [{"run_id": "old-run", "why": "Same thing"}, {"run_id": "no-such-run", "why": "x"}]})
    assert out["outcome"] == "saved" and "no-such-run: no such run" in out["saw"]
    assert review.get(s.proposals[0])["body"]["runs"] == [{"run_id": "old-run", "why": "Same thing"}]


def test_a_tag_that_duplicates_one_is_turned_away(world):
    assert tools.call(session(world), "propose_tag", {"name": "ospf-down", "area": "routing", "description": "x", "why": "x"})["outcome"] == "invalid"


def test_a_rejected_tag_stays_rejected(world):
    s = session(world)
    tools.call(s, "propose_tag", {"name": "mtu", "area": "link", "description": "x", "why": "x"})
    review.reject(s.proposals[0], "Covered by link/loss already")
    assert "Covered by link/loss" in tools.call(session(world), "propose_tag", {"name": "mtu", "area": "link", "description": "y", "why": "y"})["saw"]


def test_back_dating_leaves_out_runs_a_person_excluded(world):
    old_run(world, "one-run")
    old_run(world, "two-run")
    s = session(world)
    tools.call(s, "propose_backdate", {"tag": "routing/ospf", "why": "x", "runs": [{"run_id": "one-run", "why": "x"}]})
    review.reject(s.proposals[0], "That run was about BGP")
    s2 = session(world)
    out = tools.call(s2, "propose_backdate", {"tag": "routing/ospf", "why": "y", "runs": [{"run_id": "one-run", "why": "x"}, {"run_id": "two-run", "why": "x"}]})
    assert out["outcome"] == "saved" and [r["run_id"] for r in review.get(s2.proposals[0])["body"]["runs"]] == ["two-run"]


# --- taking over --------------------------------------------------------------

@pytest.mark.parametrize("result,fix,wanted", [
    ({"kind": "escalate", "text": "x", "step": "end"}, None, True),
    ({"kind": "stop", "text": "No conclusive cause", "step": "end"}, None, True),
    ({"kind": "cause", "label": "link.loss_no_errors", "text": "x"}, None, True),
    ({"kind": "cause", "label": "config.drift", "text": "x"}, {"status": "failed"}, True),
    ({"kind": "cause", "label": "config.drift", "text": "x"}, {"status": "fixed"}, False),
    ({"kind": "cause", "label": "config.drift", "text": "x"}, {"status": "held", "expected": True}, False),
    ({"kind": "cause", "label": "interface.no_link", "text": "x"}, None, False),
    ({"kind": "cause", "label": "bgp.isp_silent", "text": "x"}, None, False),
    ({"kind": "cause", "label": "config.changed_by_automation", "text": "x"}, None, False),
])
def test_when_the_ai_is_needed(result, fix, wanted):
    rec = run(result)
    if fix:
        rec["repair"] = fix
    assert bool(agent.needed(rec)) is wanted


def test_the_ai_stays_out_when_it_was_only_not_confirmed():
    assert agent.needed(run({"kind": "stop", "text": "Not confirmed.", "step": 1}, [{"phase": "confirm"}])) is None


def test_the_ai_stays_out_during_a_job():
    rec = run()
    rec["event"]["change_running"] = True
    assert agent.needed(rec) is None


class Scripted:
    """A model that says what the test tells it to, and keeps what it was sent."""

    def __init__(self, *turns):
        self.turns, self.sent = list(turns), []

    def __call__(self, messages, definitions, model=None, **_kw):
        self.sent.append(json.loads(json.dumps(messages)))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        calls = [{"id": f"c{n}", "name": name, "args": args} for n, (name, args) in enumerate(turn)]
        return {"text": "", "reasoning": "thinking it through", "calls": calls, "message": {"role": "assistant", "content": "", "tool_calls": [{"id": c["id"]} for c in calls]}, "model": model, "seconds": 1.0}


DONE = ("finish", {"outcome": "needs_person", "root_cause": "A fibre is broken.", "summary": "It needs someone.", "assessment": "bad_hardware", "why": "No light.", "next": "Replace it.", "tags": ["link/cabling", "made/up"]})


def over(world, model, rec=None, monkeypatch=None):
    monkeypatch.setattr(agent.llm, "chat", model)
    monkeypatch.setattr(agent, "_still_there", lambda r, rs: True)
    rec = rec or run()
    history.add_run(rec)
    agent.take_over(rec, load_ruleset(world["root"]).parsed, lambda _m: None, root=world["root"])
    return rec


def test_the_ai_works_until_it_finishes(world, monkeypatch):
    model = Scripted([("list_tags", {})], [DONE])
    rec = over(world, model, monkeypatch=monkeypatch)
    ai = rec["agent"]
    assert ai["status"] == "done" and ai["outcome"] == "needs_person" and [c["tool"] for c in ai["calls"]] == ["list_tags", "finish"]
    assert rec["analysis"]["summary"] == "It needs someone." and rec["analysis"]["assessment"] == "bad_hardware"
    assert history.tags_of("this-run") == ["link/cabling"]  # the tag it made up is not applied


def test_what_people_rejected_reaches_the_ai_before_it_suggests_anything(world, monkeypatch):
    review.reject(review.add("rule", "Open the internet firewall", "internet was unreachable", {"rules": []}, print_="x"), "Never open the firewall without a change ticket")
    model = Scripted([DONE])
    over(world, model, monkeypatch=monkeypatch)
    assert "Never open the firewall without a change ticket" in model.sent[0][1]["content"] and "ALREADY REJECTED" in model.sent[0][1]["content"]


def test_a_dead_model_ends_cleanly(world, monkeypatch):
    rec = over(world, Scripted([("list_tags", {})], llm.LLMError("gone")), monkeypatch=monkeypatch)
    assert rec["agent"]["status"] == "failed" and "stopped answering" in rec["agent"]["error"] and len(rec["agent"]["calls"]) == 1


def test_a_model_that_stops_calling_tools_ends_cleanly(world, monkeypatch):
    rec = over(world, Scripted([], [], []), monkeypatch=monkeypatch)
    assert rec["agent"]["status"] == "failed"


def test_time_running_out_ends_cleanly(world, monkeypatch):
    monkeypatch.setattr(agent, "MAX_SECONDS", -1)
    rec = over(world, Scripted(), monkeypatch=monkeypatch)
    assert rec["agent"]["status"] == "failed" and "ran out of time" in rec["agent"]["error"]


def test_if_it_cleared_while_waiting_no_model_is_called(world, monkeypatch):
    monkeypatch.setattr(agent, "_still_there", lambda r, rs: False)
    monkeypatch.setattr(agent.llm, "chat", lambda *a, **k: pytest.fail("called the model"))
    rec = run()
    agent.take_over(rec, [], root=world["root"])
    assert rec["agent"]["status"] == "not_needed" and not report.should_send(rec)


def test_the_ai_is_rate_limited(world):
    assert agent.blocked(run()) is None
    history.ai_session_started("other", "h9", "something.else")
    assert agent.blocked(run()) is None  # another device and problem does not count
    history.ai_session_started("again", "h4", "internet.unreachable")
    assert "half an hour" in agent.blocked(run())
    agent.OFF_SWITCH.touch()
    assert "switched off" in agent.blocked(run())


def test_the_watcher_gets_a_queued_run_and_the_email_waits(world, monkeypatch):
    for name in ("sources",):
        monkeypatch.setattr(pipeline.sources, "gather", lambda ev: ([], {}))
    monkeypatch.setattr(pipeline.fixer, "attempt", lambda *a: None)
    monkeypatch.setattr(pipeline.memory, "recall", lambda rec: ([], None))
    monkeypatch.setattr(pipeline.report, "send", lambda rec: pytest.fail("emailed too early"))
    rec = run()
    pipeline.finish(rec, [], lambda _m: None, inline=False)
    assert rec["agent"]["status"] == "queued" and "email" not in rec


def test_the_email_says_what_the_ai_did(world, monkeypatch):
    rec = run(agent={"status": "done", "outcome": "fixed", "model": "deepseek-v4.1-flash", "started": NOW, "why": "The rules ran out.", "root_cause": "A filter blocks it.", "calls": [{}], "proposals": [3],
                     "changes": [{"status": "kept", "device": "r3", "what": ["iptables -D FORWARD"], "saw": "It stays."}]})
    subject, body = report.compose(rec)
    assert subject.startswith("[FIXED BY AI]") and "WHAT THE AI DID" in body and "KEPT on r3" in body and "#/review" in body


def test_the_tools_are_served_over_mcp(world):
    s = session(world)
    assert mcp.handle(s, {"id": 1, "method": "initialize"})["result"]["serverInfo"]["name"]
    listed = mcp.handle(s, {"id": 2, "method": "tools/list"})["result"]["tools"]
    assert any(t["name"] == "check_interface_status" and t["inputSchema"]["required"] == ["device", "interface"] for t in listed)
    answer = mcp.handle(s, {"id": 3, "method": "tools/call", "params": {"name": "list_tags", "arguments": {}}})
    assert "routing/ospf" in answer["result"]["content"][0]["text"]
    assert mcp.handle(s, {"method": "notifications/initialized"}) is None
    assert "error" in mcp.handle(s, {"id": 4, "method": "nope"})


# --- what was left in place on purpose ---------------------------------------

STOP_GAP = ["interface eth3", "ip ospf cost 10000"]
FIRST = {"was_working": "Yes, until 20:00.", "what_changed": "A stop-gap on r4 from earlier."}


@pytest.fixture
def stop_gap(world, tmp_path, monkeypatch):
    monkeypatch.setattr(configs, "BASELINE_DIR", tmp_path / "baseline")
    monkeypatch.setattr(configs, "running", lambda _d: ("", ["hostname x"]))
    notes = []
    monkeypatch.setattr(tools.tickets, "note", lambda *a, **k: notes.append(a))
    configs.hold("r4", STOP_GAP, "earlier-run", 11, "the port keeps dropping")
    configs.hold("s4", ["interface Ethernet1", "ip ospf cost 10000"], "earlier-run", 11, "the other end")
    world["notes"], world["converged"] = notes, []
    saved = ACTIONS["converge_config"].func

    def converge(ctx, device, also=None):
        world["converged"].append(device)
        ctx.undo.append(lambda: f"put {device} back")
        ctx.changed.append(device)
        return Result("done", f"Put {device} back.")

    converge.__module__ = saved.__module__  # only the real actions are offered to the model
    object.__setattr__(ACTIONS["converge_config"], "func", converge)
    yield world
    object.__setattr__(ACTIONS["converge_config"], "func", saved)


def touch(world, lines, **args):
    return tools.call(session(world), "try_config", {"device": "r4", "lines": lines, "why": "this ticket", "verify": VERIFY, **FIRST, **args})


def retire(world, **args):
    s = session(world)
    return s, tools.call(s, "retire_earlier_change", {"run": "earlier-run", "earlier_change": "No carrier changes on that port for six hours.", "why": "It is steering this traffic.", "verify": VERIFY, **FIRST, **args})


def test_an_earlier_change_cannot_be_overwritten_without_weighing_it(stop_gap):
    out = touch(stop_gap, ["interface eth3", "ip ospf cost 100"])
    assert out["outcome"] == "refused" and "earlier_change" in out["saw"] and "T-11" in out["saw"]


def test_a_patch_on_top_of_an_earlier_change_has_to_be_temporary(stop_gap):
    out = touch(stop_gap, ["interface eth3", "ip ospf cost 100"], earlier_change="Still flapping. 100 keeps it off the link for this traffic too.")
    assert out["outcome"] == "refused" and "temporary" in out["saw"]


def test_a_reasoned_patch_on_top_goes_ahead_and_is_kept_too(stop_gap, monkeypatch):
    monkeypatch.setattr(lab, "configure", lambda *_a: (True, ""))
    out = touch(stop_gap, ["interface eth3", "ip ospf cost 100"], earlier_change="Still flapping. 100 keeps it off the link for this traffic too.", temporary=True)
    assert out["outcome"] == "kept"
    assert sorted(h["run"] for h in configs.holds() if h["device"] == "r4") == ["earlier-run", "this-run"]


def test_a_stop_gap_cannot_be_quietly_removed_by_hand(stop_gap):
    out = touch(stop_gap, ["interface eth3", "no ip ospf cost"], earlier_change="It is in the way.", temporary=True)
    assert out["outcome"] == "refused" and "retire_earlier_change" in out["saw"]


def test_other_parts_of_the_device_need_no_reasoning(stop_gap, monkeypatch):
    monkeypatch.setattr(lab, "configure", lambda *_a: (True, ""))
    assert touch(stop_gap, ["interface eth2", "ip ospf cost 50"])["outcome"] == "kept"


def test_one_route_is_the_same_route_whatever_next_hop_it_names(tmp_path):
    configs.hold("r4", ["ipv6 route 2001:db8::/48 2001:db8:100:34::"], "other", root=tmp_path)
    assert configs.touching("r4", ["ipv6 route 2001:db8::/48 2001:db8:100:99::"], root=tmp_path)
    assert not configs.touching("r4", ["ipv6 route 2001:db8:5::/48 2001:db8:100:99::"], root=tmp_path)
    assert not configs.touching("r3", ["ipv6 route 2001:db8::/48 2001:db8:100:99::"], root=tmp_path)
    assert not configs.touching("r4", ["ipv6 route 2001:db8::/48 x"], skip_run="other", root=tmp_path)


def test_retiring_takes_the_whole_stop_gap_out_and_tells_its_ticket(stop_gap):
    _s, out = retire(stop_gap, verify=VERIFY)
    assert out["outcome"] == "kept" and configs.holds() == []
    assert stop_gap["converged"] == ["r4", "s4"]
    assert stop_gap["notes"][0][0] == 11 and "six hours" in stop_gap["notes"][0][3]


def test_retiring_that_does_not_help_puts_the_reasons_back(stop_gap):
    stop_gap["verify"] = ["unreachable"]
    _s, out = retire(stop_gap)
    assert out["outcome"] == "reverted" and len(configs.holds()) == 2 and configs.holds()[0]["why"] == "the port keeps dropping"
    assert stop_gap["notes"] == []


def test_retiring_that_breaks_something_puts_it_back(stop_gap):
    stop_gap["health"] = [set(), {"links:r4"}]
    _s, out = retire(stop_gap)
    assert out["outcome"] == "reverted" and len(configs.holds()) == 2


def test_retiring_needs_evidence_a_real_run_and_someone_elses_change(stop_gap):
    assert retire(stop_gap, earlier_change=" ")[1]["outcome"] == "refused"
    assert retire(stop_gap, run="no-such-run")[1]["outcome"] == "refused"
    configs.hold("r2", ["interface eth9", "ip ospf cost 7"], "this-run")
    assert retire(stop_gap, run="this-run")[1]["outcome"] == "refused"
    assert len(configs.holds()) == 3 and stop_gap["converged"] == []


def test_putting_back_known_good_stays_even_when_nothing_else_got_better(stop_gap):
    stop_gap["health"] = [{"something:else"}, {"something:else"}]
    out = tools.call(session(stop_gap), "action_converge_config", {"device": "r1", "why": "Nothing explains this difference.", "verify": VERIFY, **FIRST})
    assert out["outcome"] == "kept"


def test_a_rule_does_not_overwrite_what_was_left_on_purpose(stop_gap):
    from types import SimpleNamespace
    rule = SimpleNamespace(action=SimpleNamespace(do=SimpleNamespace(name="config_lines", args={"device": "{device}", "lines": ["interface eth3", "ip ospf cost 5"]})))
    assert "10000" in fixer._left_in_place(rule, {"device": "r4"})
    assert fixer._left_in_place(rule, {"device": "r1"}) == ""
    assert agent.needed({"event": {"device": "r4", "event": "x"}, "rule": {"id": "a"}, "result": {"kind": "cause", "label": "a.b"}, "repair": {"status": "held", "in_the_way": True}, "steps": []})
