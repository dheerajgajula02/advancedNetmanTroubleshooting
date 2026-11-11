"""Getting back to known-good: the config lines, the fixer's guards, the patterns and the email."""

import json
import time
from types import SimpleNamespace as NS

import pytest

from ts import configs, fixer, history, lab, llm, report
from ts.lab import Result

EOS = """interface Ethernet4
   description to s3
   ip ospf cost 10
interface Management0
   ip address 172.20.20.2/24
interface Vlan10
   ip address 10.10.10.2/24
router bgp 65000
   neighbor 1.1.1.1 remote-as 65001
   address-family ipv4
      neighbor 1.1.1.1 activate
   address-family ipv6
      neighbor 1.1.1.1 activate
ip route 10.9.0.0/16 10.0.0.1
username admin privilege 15
""".splitlines()


def changed(*edits):
    lines = list(EOS)
    for old, new in edits:
        i = lines.index(old)
        lines[i:i + 1] = new
    return lines


def test_shut_port_is_unshut():
    now = changed(("   description to s3", ["   description to s3", "   shutdown"]))
    assert configs.repair_lines("eos", EOS, now) == ["interface Ethernet4", "no shutdown"]


def test_changed_value_removes_first_then_adds():
    now = changed(("   ip ospf cost 10", ["   ip ospf cost 500"]))
    assert configs.repair_lines("eos", EOS, now) == ["interface Ethernet4", "no ip ospf cost 500", "ip ospf cost 10"]


def test_no_differences_means_no_lines():
    assert configs.repair_lines("eos", EOS, EOS) == []


def test_extra_physical_port_is_defaulted_and_svi_removed():
    now = EOS + ["interface Ethernet9", "   shutdown", "interface Vlan99", "   ip address 1.1.1.1/24"]
    lines = configs.repair_lines("eos", EOS, now)
    assert "default interface Ethernet9" in lines and "no interface Vlan99" in lines


def test_frr_cannot_delete_an_interface_only_clear_it():
    known, now = ["interface eth3", " ip ospf cost 10"], ["interface eth3", " ip ospf cost 10", "interface eth7", " ip ospf cost 5"]
    assert configs.repair_lines("frr", known, now) == ["interface eth7", "no ip ospf cost 5"]


def test_lost_section_is_rebuilt_with_its_nested_blocks():
    now = [line for line in EOS if not line.startswith(("router bgp", "   neighbor", "   address-family", "      neighbor"))]
    lines = configs.repair_lines("eos", EOS, now)
    assert lines[0] == "router bgp 65000" and lines.count("address-family ipv4") == 1 and "exit" in lines


def test_same_line_lost_in_one_address_family_only():
    now = list(EOS)
    del now[now.index("   address-family ipv6") + 1]
    assert configs.managed_changes(EOS, now)
    assert configs.repair_lines("eos", EOS, now) == ["router bgp 65000", "address-family ipv6", "neighbor 1.1.1.1 activate", "exit"]


def test_no_form_negates_differently_by_platform():
    known, now = ["interface eth3", " ip ospf cost 10"], ["interface eth3", " ip ospf cost 10", " no ip ospf passive"]
    assert configs.repair_lines("frr", known, now)[-1] == "ip ospf passive"
    assert configs.repair_lines("eos", known, now)[-1] == "default ip ospf passive"


@pytest.mark.parametrize("edit", [("   ip address 172.20.20.2/24", ["   ip address 172.20.20.9/24"]), ("username admin privilege 15", ["username admin privilege 1"])])
def test_management_and_accounts_never_converge(edit):
    assert configs.repair_lines("eos", EOS, changed(edit)) == []


def test_changes_need_the_lock_and_refuse_management():
    with pytest.raises(RuntimeError):
        lab.configure("s1", ["interface Ethernet4", "no shutdown"])
    lab._lock_held = True
    try:
        worked, why = lab.configure("s1", ["interface Management0", "shutdown"])
        assert not worked and "refused" in why
    finally:
        lab._lock_held = False


# --- the fixer ------------------------------------------------------------

def call(func, **args):
    return NS(name=func.__name__, args=args, block=NS(func=func), render=lambda: func.__name__)


def cond(func, expect="yes", **args):
    return NS(check=call(func, **args), expect=[expect])


def rule(do, verify, mode="active", require=()):
    return NS(table="fix", mode=mode, priority=1, match={"cause": "interface.admin_shut"}, title="Put it back", risk="medium", id="t",
              require=list(require), action=NS(do=call(do, device="{device}")), verify=[cond(verify)])


def record(label="interface.admin_shut", params=None):
    return {"id": "x", "event": {"time": time.time(), "device": "s1"}, "values": {"device": "s1"},
            "result": {"kind": "cause", "label": label, "params": params or {}}, "steps": []}


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(fixer, "OFF_SWITCH", tmp_path / "off")
    monkeypatch.setattr(fixer, "VERIFY_FOR", 0)
    monkeypatch.setattr(fixer, "SETTLE", 0)
    monkeypatch.setattr(fixer.jenkins, "running_change_jobs", lambda: [])
    monkeypatch.setattr(fixer.jenkins, "recent_deploys", lambda: [])
    monkeypatch.setattr(fixer.history, "fixes_lately", lambda d, l: 0)
    monkeypatch.setattr(fixer.lab, "change_lock", lambda: NS(__enter__=lambda s: s, __exit__=lambda s, *a: False) and _Nolock())
    monkeypatch.setattr(fixer.configs, "running", lambda d: ("show", None))
    return tmp_path


class _Nolock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def done(ctx, device):
    ctx.undo.append(lambda: "undone")
    return Result("done", "changed it")


def yes(ctx):
    return Result("yes", "fine")


def no(ctx):
    return Result("no", "still wrong")


def test_no_fix_rule_means_no_repair(quiet):
    assert fixer.attempt(record("link.physical_fault"), [rule(done, yes)]) is None


def test_stops_without_cause(quiet):
    rec = record()
    rec["result"]["kind"] = "stop"
    assert fixer.attempt(rec, [rule(done, yes)]) is None


def test_repair_verifies(quiet):
    r = fixer.attempt(record(), [rule(done, yes)])
    assert r["status"] == "fixed" and [s["kind"] for s in r["steps"]] == ["do", "verify"]


def test_failed_verify_undoes(quiet):
    r = fixer.attempt(record(), [rule(done, no)])
    assert r["status"] == "failed" and r["steps"][-1]["kind"] == "undo" and r["steps"][-1]["saw"] == "undone"


def test_kill_switch(quiet):
    (quiet / "off").touch()
    assert fixer.attempt(record(), [rule(done, yes)])["status"] == "held"


def test_never_touches_the_isp(quiet):
    rec = record()
    rec["values"]["device"] = "r5"
    assert fixer.attempt(rec, [rule(done, yes)])["status"] == "held"


def test_stops_fighting_after_three(quiet, monkeypatch):
    monkeypatch.setattr(fixer.history, "fixes_lately", lambda d, l: 3)
    r = fixer.attempt(record(), [rule(done, yes)])
    assert r["status"] == "held" and "keeps coming back" in r["why"]


def test_running_job_is_expected_and_held(quiet, monkeypatch):
    monkeypatch.setattr(fixer.jenkins, "running_change_jobs", lambda: ["deploy #1"])
    r = fixer.attempt(record(), [rule(done, yes)])
    assert r["status"] == "held" and r["expected"]


def test_shadow_only_says_what_it_would_do(quiet):
    assert fixer.attempt(record(), [rule(done, yes, mode="shadow")])["status"] == "would_fix"


def test_requirement_not_met_changes_nothing(quiet):
    calls = []
    def do(ctx, device):
        calls.append(device)
        return Result("done", "x")
    r = fixer.attempt(record(), [rule(do, yes, require=[cond(no)])])
    assert r["status"] == "held" and not calls


def test_cause_params_choose_the_device(quiet):
    calls = []
    def do(ctx, device):
        calls.append(device)
        return Result("done", "x")
    rec = record(params={"device": "{peer_device}"})
    rec["values"]["peer_device"] = "s2"
    fixer.attempt(rec, [rule(do, yes)])
    assert calls == ["s2"]


# --- history --------------------------------------------------------------

def run(path, n, when, label="", kind="interface.down", repair=None, text="x"):
    rec = {"id": f"r{n}", "event": {"time": when, "device": "s1", "subject": "Ethernet4", "event": kind}, "rule": {"id": "t"},
           "result": {"kind": "cause" if label else "stop", "label": label, "text": text}, "steps": [{"ask": "a", "saw": "b"}]}
    if repair:
        rec["repair"] = {"status": repair}
    history.add_run(rec, path)
    return rec


def test_one_off_is_nothing(tmp_path):
    db = tmp_path / "h.sqlite"
    assert history.pattern(run(db, 1, time.time()), path=db)["verdict"] == "none"


def test_flaps_without_config_change_are_hardware(tmp_path):
    db = tmp_path / "h.sqlite"
    now = time.time()
    run(db, 1, now - 300), run(db, 2, now - 200)
    assert history.pattern(run(db, 3, now), path=db)["verdict"] == "bad_hardware"


def test_repeated_hand_change_is_tampering(tmp_path):
    db = tmp_path / "h.sqlite"
    now = time.time()
    run(db, 1, now - 3000, "interface.admin_shut", repair="fixed")
    p = history.pattern(run(db, 2, now, "interface.admin_shut"), path=db)
    assert p["verdict"] == "tampering" and p["put_back_before"] == 1


def test_same_hour_each_day_is_a_schedule(tmp_path):
    db = tmp_path / "h.sqlite"
    now = time.time()
    for i in range(1, 5):
        run(db, i, now - i * 86400)
    assert history.pattern(run(db, 9, now), path=db)["verdict"] == "scheduled"


def test_similar_finds_earlier_runs_only(tmp_path):
    db = tmp_path / "h.sqlite"
    now = time.time()
    run(db, 1, now - 100, text="Ethernet4 shut down by hand")
    run(db, 2, now + 100, text="Ethernet4 shut down by hand")
    found = history.similar(run(db, 3, now, text="Ethernet4 shut down"), path=db)
    assert [f["id"] for f in found] == ["r1"]


# --- the email and the model ----------------------------------------------

def full(status="fixed", **extra):
    rec = record()
    rec["id"] = "20261108-s1-Ethernet4"
    rec["event"].update(title="s1 Ethernet4 went down", detail="A trap arrived from clab-lab1-s1.")
    rec["result"].update(text="Ethernet4 on s1 was shut down by hand.")
    rec["steps"] = [{"n": 1, "ask": "Is it still down?", "saw": "It is shut down.", "outcome": "admin_down", "evidence": [{"ran": "s1# show interfaces"}]}]
    if status:
        rec["repair"] = {"status": status, "why": "", "steps": [{"kind": "require", "ask": "safe?", "saw": "yes"}, {"kind": "do", "ask": "Put it back", "saw": "no shutdown applied"}]}
    rec.update(extra)
    return rec


def test_email_tells_the_whole_story():
    subject, body = report.compose(full(pattern={"verdict": "none", "note": "", "count_7d": 1}, analysis={"summary": "All good.", "why": "", "next": "nothing", "related": []}))
    assert subject.startswith("[FIXED]")
    for part in ("WHAT HAPPENED", "WHAT I CHECKED", "WHAT IT CAME TO", "WHAT I DID ABOUT IT", "Put it back", "SEE THE WHOLE RUN"):
        assert part in body
    assert "clab" not in body and "safe?" not in body


def test_what_gets_emailed(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "STATE", tmp_path / "mail.json")
    assert report.should_send(full())
    assert not report.should_send(full("already"))
    plain = full(None)
    plain["result"] = {"kind": "stop", "text": "Not confirmed."}
    assert not report.should_send(plain)
    plain["pattern"] = {"verdict": "bad_hardware"}
    assert report.should_send(plain)
    during_job = full()
    during_job["event"]["change_running"] = True
    assert not report.should_send(during_job)
    automation = full(None)
    automation["result"]["label"] = "config.changed_by_automation"
    assert not report.should_send(automation)


def test_a_pattern_is_only_reported_once_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "STATE", tmp_path / "mail.json")
    plain = full(None)
    plain["result"] = {"kind": "stop", "text": "Not confirmed."}
    plain["pattern"] = {"verdict": "bad_hardware"}
    (tmp_path / "mail.json").write_text(json.dumps({"sent": [], "keys": {report._pattern_key(plain): time.time()}}))
    assert not report.should_send(plain)


def test_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "STATE", tmp_path / "mail.json")
    (tmp_path / "mail.json").write_text(json.dumps({"sent": [time.time()] * report.MAX_PER_HOUR, "keys": {}}))
    assert report.send(full())["sent"] is False


def test_a_model_nobody_chose_is_refused():
    with pytest.raises(llm.LLMError):
        llm.complete([{"role": "user", "content": "hi"}], model="gpt-oss-120b")


def test_parse_json_takes_the_first_object():
    assert llm.parse_json('Sure:\n{"a": 1} trailing') == {"a": 1}
    assert llm.parse_json("no json") is None
