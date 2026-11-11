"""The ticket as a conversation, the people at the computers, stop-gaps, and choosing models."""

import json
import shutil
import subprocess
import re
import stat
import threading
import time
from pathlib import Path

import pytest

from drills import people, scenarios
from ts import agent, aiconfig, configs, firstlook, llm, overview, store, sweep, tickets, tools


def stored(root, run_id, device="h1", **extra):
    store.save({"id": run_id, "event": {"time": 1000.0, "device": device, "subject": "internet", "title": "x", "event": "ticket.report", "source": "ticket"},
                "result": {"kind": "stop", "label": "", "text": "found it"}, "rule": {"id": "none", "title": "none"}, "steps": [], "finished": 1010.0, **extra}, root)
    store._cache.clear()


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "incidents"
    root.mkdir()
    monkeypatch.setattr(store, "DATA_DIR", root)
    monkeypatch.setattr(tickets.history, "DB", tmp_path / "history.sqlite")
    monkeypatch.setattr(tickets.llm, "complete", lambda *_a, **_k: (_ for _ in ()).throw(tickets.llm.LLMError("no model in tests")))
    monkeypatch.setattr(tickets.llm, "chat", lambda *_a, **_k: (_ for _ in ()).throw(tickets.llm.LLMError("no model in tests")))
    monkeypatch.setattr(tickets, "_works_from_their_computer", lambda *_a: True)
    monkeypatch.setattr(tickets.lab, "change_running", lambda: False)
    return root


PLAN = {"kind": "internet", "title": "No internet", "priority": "P1"}
QUIET = lambda _m: None


def ends_as(world, **extra):
    seen = []

    def investigate(event):
        seen.append(event)
        stored(world, tickets.engine.incident_id(event), device=event["device"], **extra)
        return "done"
    investigate.seen = seen
    return investigate


FIXED = {"repair": {"status": "fixed"}}
STOP_GAP = {"agent": {"status": "done", "outcome": "workaround", "root_cause": "the port keeps dropping", "calls": [], "changes": [{"status": "kept", "temporary": True, "device": "s4", "what": ["ip ospf cost 500"]}]}}


def confirming(world, then=FIXED, person="priya"):
    number = tickets.create(person, "no internet")
    investigate = ends_as(world, **then)
    assert tickets.work_one(tickets.get(number), investigate, QUIET, PLAN) == tickets.CONFIRMING
    return number


def answer(number, words):
    ticket = tickets.get(number)
    tickets.reply(number, ticket["name"], "person", words)
    return tickets._answered()


def test_a_ticket_goes_straight_to_the_agent_whatever_it_is_about(world):
    for kind in ("internet", "address", "other"):
        event = tickets.event_from(tickets.get(tickets.create("lena", "no internet")), {"kind": kind, "title": "t", "priority": "P2"})
        assert event["event"] == "ticket.report" and event["source"] == "ticket"
    assert agent.needed({"event": event, "rule": {"id": "none"}, "result": {"kind": "stop"}}) == "A person wrote in about this."


def test_when_they_say_it_works_the_ticket_settles(world):
    number = confirming(world)
    ticket = answer(number, "Yes, all good now, thanks")
    assert ticket["id"] == number
    assert tickets._hear_back(ticket, lambda _e: pytest.fail("no need to look again"), QUIET) == tickets.SOLVED
    found = tickets.get(number)
    assert found["status"] == tickets.SOLVED and found["waiting_note"] == 0 and "confirmed by Priya" in found["stage"]


def test_a_stop_gap_stays_open_for_an_engineer_after_they_confirm(world):
    number = confirming(world, STOP_GAP)
    assert tickets.get(number)["then_status"] == tickets.WORKAROUND
    assert tickets._hear_back(answer(number, "It seems fine now"), lambda _e: pytest.fail("no"), QUIET) == tickets.WORKAROUND
    found = tickets.get(number)
    assert found["status"] == tickets.WORKAROUND and found["status"] in tickets.OPEN and not found["solved"]


def test_nothing_found_still_asks_them(world):
    number = confirming(world, {"agent": {"status": "done", "outcome": "nothing_wrong", "calls": [], "changes": []}, "result": {"kind": "cleared"}})
    assert tickets.get(number)["then_status"] == tickets.NO_FAULT


def test_when_they_say_it_does_not_work_it_is_looked_at_again_with_everything_so_far(world):
    number = confirming(world)
    investigate = ends_as(world, **FIXED)
    tickets._hear_back(answer(number, "No, it still keeps dropping out"), investigate, QUIET)
    event = investigate.seen[0]
    assert event["follow_up"] is True and "THIS IS A FOLLOW-UP" in event["detail"] and "keeps dropping" in event["detail"]
    assert tickets.get(number)["rounds"] == 1 and tickets.get(number)["status"] == tickets.CONFIRMING


def test_after_too_many_goes_an_engineer_takes_it(world):
    number = confirming(world)
    investigate = ends_as(world, **FIXED)
    for _ in range(tickets.MAX_ROUNDS - 1):
        tickets._hear_back(answer(number, "No, still broken"), investigate, QUIET)
    assert tickets._hear_back(answer(number, "No, still broken"), investigate, QUIET) == tickets.NEEDS_AGENT
    assert tickets.get(number)["status"] == tickets.NEEDS_AGENT and len(investigate.seen) == tickets.MAX_ROUNDS - 1


def test_no_reply_at_all_does_not_wait_forever(world, monkeypatch):
    number = confirming(world)
    monkeypatch.setattr(tickets, "NO_REPLY_AFTER", -1)
    tickets._stop_waiting(QUIET)
    assert tickets.get(number)["status"] == tickets.NEEDS_AGENT and not tickets.get(number)["solved"] and "never said it works" in tickets.get(number)["stage"]  # only their word makes it solved


def test_asking_returns_their_answer(world):
    number = tickets.create("ravi", "keeps dropping")
    threading.Timer(0.1, lambda: tickets.reply(number, "Ravi", "person", "It dropped twice just now")).start()
    assert tickets.ask(number, "How often does it drop?", wait=5) == "It dropped twice just now"
    assert tickets.get(number)["waiting_note"] == 0


def test_asking_gives_up_when_nobody_answers(world):
    number = tickets.create("ravi", "keeps dropping")
    assert tickets.ask(number, "Are you there?", wait=0.05) is None and tickets.get(number)["waiting_note"] == 0


def test_the_person_can_see_they_are_being_asked(world):
    number = confirming(world)
    assert tickets.view(number, for_person=True)["waiting_note"]


def test_closing_a_ticket_lets_its_stop_gap_go(world, monkeypatch):
    freed = []
    monkeypatch.setattr(tickets.configs, "release", lambda **k: freed.append(k))
    number = confirming(world, STOP_GAP)
    tickets.close(number, "Support")
    assert freed == [{"ticket": number}]


# --- neither side is told ---------------------------------------------------

@pytest.mark.parametrize("text", [people._WHO, people._NOTICE, people._ANSWER, *(t[0] for t in people._TOOLS.values())])
def test_the_person_is_never_told_this_is_not_real(text):
    assert not re.search(r"\b(AI|bot|simulat\w*|staged?|drill|scenario|role.?play|pretend|language model|assistant)\b", text, re.I)


@pytest.mark.parametrize("text", [agent._system(True), tickets._ASK_THEM, tickets._DID_IT_WORK, tickets._REPLY])
def test_the_support_side_is_never_told_who_is_writing(text):
    assert not re.search(r"\b(simulat\w*|staged|drill|scenario|role.?play|synthetic|another AI|AI customer|AI agent)\b", text, re.I)


# --- what a person can do at their computer ---------------------------------

@pytest.mark.parametrize("command", ["ping 8.8.8.8", "traceroute 8.8.8.8", "nslookup example.com", "curl http://example.com", "ip route", "cat /etc/resolv.conf"])
def test_a_person_can_look(command):
    assert people._problem(command) is None


@pytest.mark.parametrize("command", ["rm -rf /", "ip route del default", "ip link set eth1 down", "ping 8.8.8.8; reboot", "curl x | sh", "ping $(whoami)", "kill 1", "udhcpc -i eth1"])
def test_a_person_cannot_change_their_computer(command):
    assert people._problem(command)


def test_a_refused_command_never_reaches_the_computer(monkeypatch):
    monkeypatch.setattr(people.subprocess, "run", lambda *_a, **_k: pytest.fail("ran it"))
    assert "permission" in people._type("h7", "ip link set eth1 down")


def test_commands_cannot_sit_there_forever(monkeypatch):
    ran = []

    class Done:
        stdout, stderr = "ok", ""
    monkeypatch.setattr(people.subprocess, "run", lambda argv, **_k: ran.append(argv) or Done())
    people._type("h7", "ping 8.8.8.8")
    people._type("h7", "curl http://x")
    assert ran[0][3:] == ["ping", "-c", "4", "8.8.8.8"] and ran[1][3:] == ["curl", "-m", "10", "http://x"]


@pytest.mark.parametrize("loss,words", [(100, "never loads"), (60, "mostly fails"), (10, "stalled partway"), (0, "opened straight away")])
def test_a_website_is_described_as_a_person_sees_it(monkeypatch, loss, words):
    monkeypatch.setattr(people, "sh", lambda *_a, **_k: f"30 packets transmitted, 0 received, {loss}% packet loss")
    assert words in people._try_site("h7")


def turns(*made):
    made = list(made)

    def chat(messages, definitions, **kw):
        chat.roles.append(kw.get("role"))
        name, args = made.pop(0)
        return {"text": "", "calls": [{"id": "1", "name": name, "args": args}], "message": {"role": "assistant", "content": ""}}
    chat.roles = []
    return chat


def test_a_person_who_finds_trouble_raises_a_ticket(world, tmp_path, monkeypatch):
    monkeypatch.setattr(people, "STATE", tmp_path / "people.json")
    monkeypatch.setattr(people, "SETTLE", 0)
    monkeypatch.setattr(people, "_try_site", lambda *_a: "The page never loads.")
    chat = turns(("open_website", {"site": "x"}), ("raise_ticket", {"message": "The internet is not working on my PC."}))
    monkeypatch.setattr(people.llm, "chat", chat)
    got = []
    people._notice("ravi", got.append, lambda: pytest.fail("they did notice"))
    assert tickets.get(got[0])["body"] == "The internet is not working on my PC." and chat.roles == ["h7", "h7"]
    assert people.last()["ticket"] == got[0]


def test_a_person_stops_trying_and_decides(world, tmp_path, monkeypatch):
    monkeypatch.setattr(people, "STATE", tmp_path / "people.json")
    monkeypatch.setattr(people, "DIARY", tmp_path / "diary.jsonl")
    monkeypatch.setattr(people, "_try_site", lambda *_a: "It stalled.")
    offered = []

    def chat(messages, definitions, **_k):
        offered.append({d["function"]["name"] for d in definitions})
        name, args = ("raise_ticket", {"message": "It keeps stalling."}) if "open_website" not in offered[-1] else ("open_website", {"site": "x"})
        return {"text": "", "calls": [{"id": "1", "name": name, "args": args}], "message": {"role": "assistant", "content": ""}}
    monkeypatch.setattr(people.llm, "chat", chat)
    ended = people._sit_down(tickets.person("ravi"), "go", ("open_website", "raise_ticket", "all_fine"), ("raise_ticket", "all_fine"))
    assert ended["tool"] == "raise_ticket" and len(offered) == people.MOST_TRIES + 1


def test_a_person_who_sees_nothing_wrong_stays_quiet(world, tmp_path, monkeypatch):
    monkeypatch.setattr(people, "STATE", tmp_path / "people.json")
    monkeypatch.setattr(people, "SETTLE", 0)
    monkeypatch.setattr(people.llm, "chat", turns(("all_fine", {})))
    nothing = []
    people._notice("ravi", lambda _n: pytest.fail("no ticket"), lambda: nothing.append(1))
    assert nothing == [1] and not tickets.snapshot()["tickets"]


def test_a_person_answers_what_they_were_asked_once(world, tmp_path, monkeypatch):
    monkeypatch.setattr(people, "STATE", tmp_path / "people.json")
    number = confirming(world, person="ravi")
    people.take(number, "ravi")
    monkeypatch.setattr(people, "_try_site", lambda *_a: "The page opened straight away and everything on it loaded.")
    monkeypatch.setattr(people.llm, "chat", turns(("open_website", {"site": "x"}), ("last_message", {"message": "Yes, it is working fine now."})))
    ticket = tickets.get(number)
    people._busy.add(str(number))
    people._answer(str(number), ticket, ticket["waiting_note"], "ravi")
    assert people._load()["tickets"][str(number)]["answered"] == ticket["waiting_note"]
    assert tickets._answered()["id"] == number
    started = []
    monkeypatch.setattr(people.threading, "Thread", lambda **k: started.append(k))
    people.tick()
    assert not started


def test_flapping_can_be_stopped_after_the_desk_restarts(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(scenarios, "sh", lambda *a, **_k: ran.append(a) or "")
    monkeypatch.setattr(scenarios.CablePull, "restore", lambda self: ran.append(("restored", self.saved, self.routes)))
    drill = scenarios.KeepsFlapping("f", "x", "h7", "ticket", {"escalate"})
    monkeypatch.setattr(scenarios.KeepsFlapping, "SAVED", tmp_path / "saved.json")
    scenarios.KeepsFlapping.SAVED.write_text(json.dumps({"saved": ["10.0.44.0/31"], "routes": []}))
    drill.restore()
    assert ran == [("restored", ["10.0.44.0/31"], [])]


def test_flapping_really_goes_up_and_down_and_stops(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(scenarios, "sh", lambda *a, **_k: ran.append(a) or "")
    monkeypatch.setattr(scenarios.KeepsFlapping, "SAVED", tmp_path / "saved.json")
    monkeypatch.setattr(scenarios.CablePull, "restore", lambda self: None)
    monkeypatch.setattr(scenarios.CablePull, "put_back", lambda self: None)
    drill = scenarios.KeepsFlapping("f", "x", "h7", "ticket", {"escalate"})
    drill.DOWN, drill.UP = 0.01, 0.01
    monkeypatch.setattr(drill, "snapshot", lambda: (setattr(drill, "saved", []), setattr(drill, "routes", [])))
    drill.inject()
    time.sleep(1.6)
    drill.restore()
    moves = [a[-1] for a in ran if a[1:4] == ("ip", "link", "set")]
    assert "down" in moves and "up" in moves and not drill.flapper.is_alive()


# --- a stop-gap is not undone behind everyone's back --------------------------

def test_a_held_section_is_left_out_of_known_good_comparisons(tmp_path, monkeypatch):
    monkeypatch.setattr(configs, "BASELINE_DIR", tmp_path)
    lines = ["hostname s4", "interface Ethernet1", "   ip ospf cost 10", "interface Ethernet2", "   no switchport"]
    configs.hold("s4", ["interface Ethernet1", "ip ospf cost 500"], "run1", 7, "port flapping", root=tmp_path)
    kept = configs.without_held("s4", lines, root=tmp_path)
    assert "   ip ospf cost 10" not in kept and "interface Ethernet2" in kept
    configs.release(ticket=7, root=tmp_path)
    assert configs.without_held("s4", lines, root=tmp_path) == lines


def test_a_held_route_is_left_out_but_the_rest_of_the_device_is_still_compared(tmp_path, monkeypatch):
    monkeypatch.setattr(configs, "BASELINE_DIR", tmp_path)
    configs.hold("r4", ["no ipv6 route 2001:db8::/48 2001:db8:100:44::1", "ipv6 route 2001:db8::/48 2001:db8:100:34::"], "run1", 7, "stop-gap", root=tmp_path)
    good = ["hostname r4", "ipv6 route 2001:db8::/48 2001:db8:100:44::1", "router ospf", " passive-interface eth1"]
    now = ["hostname r4", "ipv6 route 2001:db8::/48 2001:db8:100:34::", "router ospf", " passive-interface eth9"]
    assert configs.without_held("r4", good, root=tmp_path) == ["hostname r4", "router ospf", " passive-interface eth1"]
    assert configs.without_held("r4", now, root=tmp_path) == ["hostname r4", "router ospf", " passive-interface eth9"]


def test_only_a_kept_temporary_change_can_finish_as_a_workaround():
    session = tools.Session.__new__(tools.Session)
    session.changes, session.result = [], None
    args = {"outcome": "workaround", "root_cause": "x", "summary": "x", "assessment": "x", "why": "x", "next": "replace the cable"}
    assert tools._finish(session, args)["outcome"] == "invalid" and session.result is None
    session.changes.append({"status": "kept", "temporary": False})
    assert tools._finish(session, args)["outcome"] == "invalid" and session.result is None
    session.changes.append({"status": "kept", "temporary": True})
    assert tools._finish(session, args)["outcome"] == "done" and session.result["outcome"] == "workaround"


def test_only_a_ticket_gets_to_ask_someone():
    names = lambda flag: {d["function"]["name"] for d in tools.definitions(flag)}
    assert "ask_customer" in names(True) and "ask_customer" not in names(False)


# --- choosing models ----------------------------------------------------------

@pytest.fixture
def models(tmp_path, monkeypatch):
    monkeypatch.setattr(aiconfig, "KEYS", tmp_path / "keys.json")
    monkeypatch.setattr(aiconfig, "SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr(aiconfig, "CATALOG", tmp_path / "catalog.json")
    monkeypatch.setattr(aiconfig, "_opencode", lambda: {"OPENCODE_BASE": "http://opencode/v1", "OPENCODE_API_KEY": "the-opencode-key"})
    monkeypatch.setattr(aiconfig, "usage", lambda: {"day": [], "week": []})
    monkeypatch.setattr(aiconfig, "models_of", lambda _e, refresh=False: [])


def test_no_key_ever_leaves(models):
    endpoint = aiconfig.save_endpoint("Mine", "http://example.test/v1", "super-secret-key")
    shown = json.dumps(aiconfig.page())
    assert "super-secret-key" not in shown and "the-opencode-key" not in shown
    assert next(e for e in aiconfig.endpoints() if e["id"] == endpoint)["has_key"] is True
    assert stat.S_IMODE(aiconfig.KEYS.stat().st_mode) == 0o600


def test_a_saved_key_is_not_sent_to_a_new_address(models):
    endpoint = aiconfig.save_endpoint("Mine", "http://example.test/v1", "super-secret-key")
    with pytest.raises(ValueError, match="key"):
        aiconfig.save_endpoint("Mine", "http://elsewhere.test/v1", "", endpoint)
    assert aiconfig.save_endpoint("Renamed", "http://example.test/v1", "", endpoint) == endpoint


def test_a_computer_uses_the_default_until_it_gets_its_own(models):
    assert aiconfig.choice("h7").model == aiconfig.DEFAULTS["customer"] and aiconfig.choice("h7").role == "h7"
    aiconfig.set_role("h7", aiconfig.BUILT_IN, "gemini-4-argon", "high")
    assert aiconfig.choice("h7").model == "gemini-4-argon" and aiconfig.choice("h7").effort == "high"
    aiconfig.set_role("h7", "", "", "")
    assert aiconfig.choice("h7").model == aiconfig.DEFAULTS["customer"]


def test_an_endpoint_in_use_cannot_be_deleted(models):
    endpoint = aiconfig.save_endpoint("Mine", "http://example.test/v1", "k")
    aiconfig.set_role("h3", endpoint, "m")
    with pytest.raises(ValueError, match="still chosen"):
        aiconfig.delete_endpoint(endpoint)


def test_nothing_unknown_can_be_chosen(models):
    with pytest.raises(ValueError):
        aiconfig.set_role("nobody", aiconfig.BUILT_IN, "m")
    with pytest.raises(ValueError):
        aiconfig.set_role("support", "no-such-endpoint", "m")
    with pytest.raises(ValueError, match="http"):
        aiconfig.save_endpoint("Bad", "file:///etc/passwd", "k")


def test_a_run_cannot_name_a_model_nobody_chose(models):
    with pytest.raises(llm.LLMError):
        llm._route("support", "some-other-model")


@pytest.mark.parametrize("page", ["index.html", "app.js", "desk.html"])
def test_the_pages_are_valid_javascript(page, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed here")
    text = (Path(__file__).resolve().parent.parent / "ts" / "static" / page).read_text()
    script = tmp_path / "page.js"
    script.write_text(text if page.endswith(".js") else "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", text, re.S)))
    done = subprocess.run([node, "--check", str(script)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[:500]


# --- saying only what needs saying --------------------------------------------

def test_when_they_confirm_nobody_has_the_last_word(world):
    number = confirming(world)
    ticket = answer(number, "Yes, it works now")
    before = len(tickets.notes_of(number, private=False))
    tickets._hear_back(ticket, lambda _e: pytest.fail("no"), QUIET)
    assert len(tickets.notes_of(number, private=False)) == before


@pytest.mark.parametrize("prompt", [people._NOTICE, people._ANSWER])
def test_a_person_is_told_not_to_write_letters(prompt):
    assert "No greeting, no thanks, no sign-off" in prompt


def asking(monkeypatch, changes=()):
    session = tools.Session.__new__(tools.Session)
    session.record, session.calls, session.changes, session.waited = {"event": {"ticket": 5}}, [], list(changes), 0.0
    asked = []
    monkeypatch.setattr(tools.tickets, "ask", lambda number, message: asked.append(message) or "ok")
    return session, asked


def test_support_cannot_ask_whether_a_change_worked_once_one_is_kept(monkeypatch):
    session, asked = asking(monkeypatch, [{"status": "kept"}])
    assert tools._ask_customer(session, {"message": "Is it working now?"})["outcome"] == "refused" and not asked


def test_support_cannot_send_thanks_as_a_question(monkeypatch):
    session, asked = asking(monkeypatch)
    assert tools._ask_customer(session, {"message": "Thanks for that, Ravi."})["outcome"] == "refused" and not asked


def test_support_cannot_ask_the_same_thing_twice(monkeypatch):
    session, asked = asking(monkeypatch)
    assert tools._ask_customer(session, {"message": "How often does it drop?"})["outcome"] == "answered"
    session.calls.append({"tool": "ask_customer", "args": {"message": "How often does it drop?"}})
    assert tools._ask_customer(session, {"message": "how often does it drop?"})["outcome"] == "refused" and len(asked) == 1


# --- the two questions come first ---------------------------------------------

def checks(monkeypatch, **state):
    monkeypatch.setattr(firstlook.sweep, "load_state", lambda: {"_tick": {"n": 1, "at": time.time()}, **state})


def seen(**extra):
    return {"event": {"device": "h7", "subject": "internet"}, **extra}


EVENT = {"device": "h7", "subject": "internet"}


def test_it_was_working_at_the_last_check(world, monkeypatch):
    checks(monkeypatch, **{"internet:h7": seen(ok=True, last_ok=time.time())})
    got = firstlook._was_it_working(EVENT, "")
    assert got["verdict"] == "yes" and got["summary"].startswith("Yes. The last check")


def test_it_worked_until_it_started_failing(world, monkeypatch):
    checks(monkeypatch, **{"internet:h7": seen(ok=False, last_ok=time.time() - 600, since=time.time() - 300)})
    got = firstlook._was_it_working(EVENT, "")
    assert got["verdict"] == "yes" and "It has been failing since" in got["summary"] and "failing since" in got["lines"][0]


def test_something_that_never_worked_says_so(world, monkeypatch):
    checks(monkeypatch, **{"internet:h7": seen(ok=False, first_seen=time.time() - 2 * 86400)})
    assert firstlook._was_it_working(EVENT, "")["verdict"] == "no"


def test_new_records_do_not_claim_it_never_worked(world, monkeypatch):
    checks(monkeypatch, **{"internet:h7": seen(ok=False)})
    assert firstlook._was_it_working(EVENT, "")["verdict"] == "unknown"


def test_a_device_nothing_checks_is_not_known(world, monkeypatch):
    checks(monkeypatch)
    assert firstlook._was_it_working(EVENT, "")["verdict"] == "unknown"


def test_config_is_compared_with_known_good(monkeypatch):
    monkeypatch.setattr(firstlook.configs, "known_good", lambda _d: ("golden snapshot 1", ["interface Ethernet1", "   no switchport"]))
    monkeypatch.setattr(firstlook.configs, "running", lambda _d: ("show", ["interface Ethernet1", "   no switchport", "   shutdown"]))
    monkeypatch.setattr(firstlook.configs, "holds", lambda _r=None: [])
    device, changes, note = firstlook._one_device("s4")
    assert device == "s4" and changes["interface Ethernet1"]["added"] == ["shutdown"] and note == "golden snapshot 1"


def test_a_device_that_will_not_answer_is_reported_not_passed(monkeypatch):
    monkeypatch.setattr(firstlook.configs, "known_good", lambda _d: ("golden", ["x"]))
    monkeypatch.setattr(firstlook.configs, "running", lambda _d: ("show", None))
    assert firstlook._one_device("s4")[1] is None and "did not return" in firstlook._one_device("s4")[2]


def test_a_difference_from_known_good_leads_the_answer(world, monkeypatch):
    differs = [{"device": "r4", "against": "golden snapshot 1", "sections": [{"section": "interface eth3", "removed": ["ip ospf cost 10"], "added": ["ip ospf cost 500"]}]}]
    monkeypatch.setattr(firstlook, "_config_differences", lambda: (differs, []))
    monkeypatch.setattr(firstlook.configs, "holds", lambda _r=None: [])
    got = firstlook._has_anything_changed([], "")
    assert got["verdict"] == "yes" and got["summary"].startswith("r4 differs from known-good")
    assert "ip ospf cost 500" in firstlook.text({"working": {"summary": "Yes."}, "changed": got})


def test_a_quiet_network_says_nothing_changed(world, monkeypatch):
    monkeypatch.setattr(firstlook.configs, "holds", lambda _r=None: [])
    got = firstlook._has_anything_changed([], "")
    assert got["verdict"] == "no" and "matches known-good" in got["summary"]


def test_the_look_never_stops_an_investigation(world, monkeypatch):
    monkeypatch.setattr(firstlook.sweep, "load_state", lambda: 1 / 0)
    assert firstlook.take(EVENT)["working"]["verdict"] == "unknown"


def test_the_sweep_remembers_when_things_last_worked():
    state, finding = {}, sweep.Finding("internet:h7", True, {"event": "x"})
    sweep.step(state, [("internet:h7", [finding])], 100.0)
    assert state["internet:h7"]["last_ok"] == 100.0 and state["internet:h7"]["first_seen"] == 100.0
    sweep.step(state, [("internet:h7", [sweep.Finding("internet:h7", False, {"event": "x"})])], 200.0)
    assert state["internet:h7"]["last_ok"] == 100.0 and state["internet:h7"]["since"] == 200.0 and state["internet:h7"]["first_seen"] == 100.0


def test_the_agent_is_told_the_answers_before_anything_else():
    record = {"first_look": {"working": {"summary": "Yes, until 20:00."}, "changed": {"summary": "r4 differs from known-good."}}}
    assert firstlook.text(record["first_look"]).startswith("FIRST LOOK") and "HAS ANYTHING CHANGED? r4 differs" in firstlook.text(record["first_look"])
    assert "Was it working before? Has anything changed?" in agent._system(True) and "Was it working before? Has anything changed?" in agent._system(False)


# --- where each thing came from -----------------------------------------------

def test_a_failing_check_says_where_it_came_from(monkeypatch):
    monkeypatch.setattr(overview.sweep, "load_state", lambda: {"link:r4:eth3": {"ok": False, "event": {"source": "health", "device": "r4", "subject": "eth3", "title": "t"}}})
    assert overview._health([], 1000.0)[1][0]["source"] == "health"


def test_a_failing_check_being_worked_on_a_ticket_names_the_ticket(monkeypatch):
    monkeypatch.setattr(overview.sweep, "load_state", lambda: {"internet:h7": {"ok": False, "event": {"source": "health", "device": "h7", "subject": "internet", "title": "t"}}})
    row = {"id": "r", "device": "h7", "subject": "internet", "time": 990.0, "state": "working", "by": "ai", "headline": "x", "agent_doing": None, "agent_steps": 3, "source": "ticket", "ticket": 11}
    found = overview._health([row], 1000.0)[1][0]
    assert found["source"] == "ticket" and found["ticket"] == 11


# --- why something differs ---------------------------------------------------

def differing(*sections):
    return [{"device": "r4", "against": "golden", "sections": [{"section": name, "removed": removed, "added": added} for name, removed, added in sections]}]


EARLIER_FIX = {"device": "r4", "what": ["interface eth3", "ip ospf cost 50"], "run": "r-1", "ticket": 9, "why": "steer it away", "started": 1000.0, "temporary": False}


def test_a_difference_an_earlier_change_explains_is_not_called_drift():
    found = differing(("interface eth3", [], ["ip ospf cost 50"]))
    firstlook._explain(found, [EARLIER_FIX])
    assert found[0]["sections"][0]["because"]["run"] == "r-1"


def test_a_difference_nobody_explains_stays_drift():
    found = differing(("interface eth3", [], ["shutdown"]))
    firstlook._explain(found, [EARLIER_FIX])
    assert "because" not in found[0]["sections"][0]


def test_a_stray_line_next_to_an_explained_one_is_still_drift():
    found = differing(("interface eth3", [], ["ip ospf cost 50", "shutdown"]))
    firstlook._explain(found, [EARLIER_FIX])
    assert "because" not in found[0]["sections"][0]


def test_another_devices_change_explains_nothing():
    found = differing(("interface eth3", [], ["ip ospf cost 50"]))
    firstlook._explain(found, [{**EARLIER_FIX, "device": "r3"}])
    assert "because" not in found[0]["sections"][0]


def test_the_two_kinds_of_difference_are_told_apart_for_the_agent(world, monkeypatch):
    monkeypatch.setattr(firstlook, "_config_differences", lambda: (differing(("interface eth1", [], ["shutdown"])), []))
    monkeypatch.setattr(firstlook.configs, "holds", lambda _r=None: [{"device": "r4", "sections": ["interface eth3"], "lines": ["interface eth3", "ip ospf cost 10000"], "run": "r-2", "ticket": None, "why": "the port keeps dropping", "since": 1000.0}])
    got = firstlook._has_anything_changed([], "")
    words = firstlook.text({"working": {"summary": "Yes."}, "changed": got})
    assert "nothing on record explains it" in got["summary"] and got["drift"] == ["r4"] and got["earlier"][0]["changes"][0]["why"] == "the port keeps dropping"
    assert words.index("DRIFT") < words.index("CHANGED HERE EARLIER") and "the port keeps dropping" in words


def test_the_agent_is_told_how_to_weigh_an_earlier_change():
    for words in ("Never put one back just because it differs", "retire_earlier_change", "temporary true", "needs_person", "bad hardware"):
        assert words in agent._system(True)
    assert "did this work" not in agent._system(False) and "worked for them before" in agent._system(True)


# --- how a conversation ends --------------------------------------------------

def _names(definitions):
    return {d["function"]["name"] for d in definitions}


def test_the_desk_closes_the_conversation_with_a_tool(world, monkeypatch):
    offered = []

    def chat(_messages, definitions, **_k):
        offered.append(_names(definitions))
        return {"text": "", "calls": [{"id": "1", "name": "send_closing_message", "args": {"message": "We changed a setting. Can you see it working now?"}}]}
    monkeypatch.setattr(tickets.llm, "chat", chat)
    number = confirming(world)
    last = tickets.notes_of(number, private=False)[-1]
    assert offered == [{"send_closing_message"}] and last["closing"] == 1 and last["body"].startswith("We changed") and tickets.get(number)["waiting_note"] == last["id"]


def test_a_closing_message_still_goes_when_the_model_cannot_write_it(world):
    number = confirming(world)
    last = tickets.notes_of(number, private=False)[-1]
    assert last["closing"] == 1 and "working" in last["body"]


def test_an_ordinary_question_is_not_a_closing_message(world):
    number = tickets.create("ravi", "no internet")
    tickets.note(number, "Service desk", "desk", "How often does it drop?")
    assert not tickets.notes_of(number, private=False)[-1]["closing"]


def _at_their_computer(tmp_path, monkeypatch, *planned):
    monkeypatch.setattr(people, "STATE", tmp_path / "people.json")
    monkeypatch.setattr(people, "DIARY", tmp_path / "diary.jsonl")
    monkeypatch.setattr(people, "_try_site", lambda *_a: "The page opened straight away and everything on it loaded.")
    made = list(planned)
    seen = []

    def chat(messages, definitions, **k):
        if k.get("role") == "desk":
            raise tickets.llm.LLMError("no model in tests")
        seen.append((messages[1]["content"], _names(definitions)))
        name, args = made.pop(0)
        return {"text": "", "calls": [{"id": "1", "name": name, "args": args}], "message": {"role": "assistant", "content": ""}}
    monkeypatch.setattr(people.llm, "chat", chat)
    return seen


def test_after_the_closing_message_their_next_reply_is_the_last(world, tmp_path, monkeypatch):
    seen = _at_their_computer(tmp_path, monkeypatch, ("last_message", {"message": "It is working."}))
    number = confirming(world, person="ravi")
    people.take(number, "ravi")
    ticket = tickets.get(number)
    people._busy.add(str(number))
    people._answer(str(number), ticket, ticket["waiting_note"], "ravi")
    assert "last message" in seen[0][0] and "last_message" in seen[0][1] and "reply" not in seen[0][1]
    assert tickets._hear_back(tickets._answered(), lambda _e: pytest.fail("no need to look again"), QUIET) == tickets.SOLVED
    assert tickets.notes_of(number, private=False)[-1]["body"] == "It is working."  # nothing comes after it
    started = []
    monkeypatch.setattr(people.threading, "Thread", lambda **k: started.append(k))
    people.tick()
    assert not started


def test_a_question_during_the_work_gets_an_ordinary_reply(world, tmp_path, monkeypatch):
    seen = _at_their_computer(tmp_path, monkeypatch, ("reply", {"message": "It worked at ten."}))
    number = tickets.create("ravi", "no internet")
    people.take(number, "ravi")
    asked = tickets.note(number, "Service desk", "desk", "Did it work earlier?")
    tickets._set(number, waiting_note=asked)
    people._busy.add(str(number))
    people._answer(str(number), tickets.get(number), asked, "ravi")
    assert "reply" in seen[0][1] and "last_message" not in seen[0][1] and tickets.notes_of(number, private=False)[-1]["body"] == "It worked at ten."


def test_something_they_could_not_try_is_not_a_failure():
    assert "have not tried or cannot check" in tickets._DID_IT_WORK


def test_a_person_told_to_decide_cannot_carry_on_trying(world, tmp_path, monkeypatch):
    _at_their_computer(tmp_path, monkeypatch, ("open_website", {"site": "x"}), ("open_website", {"site": "y"}), ("reply", {"message": "It will not load."}))
    tried = []
    monkeypatch.setattr(people, "_try_site", lambda *_a: tried.append(1) or "It stalled.")
    monkeypatch.setattr(people, "MOST_TRIES", 1)
    ended = people._sit_down(tickets.person("ravi"), "go", ("open_website", "reply"), ("reply",))
    assert ended == {"tool": "reply", "message": "It will not load."} and len(tried) == 1


def test_running_short_of_time_still_gets_their_answer(world, tmp_path, monkeypatch):
    monkeypatch.setattr(people, "STATE", tmp_path / "people.json")
    monkeypatch.setattr(people, "DIARY", tmp_path / "diary.jsonl")
    monkeypatch.setattr(people, "DECIDE_BY", 0)
    offered = []

    def chat(_messages, definitions, **_k):
        offered.append(_names(definitions))
        return {"text": "It still will not load.", "calls": [], "message": {"role": "assistant", "content": "It still will not load."}}
    monkeypatch.setattr(people.llm, "chat", chat)
    ended = people._sit_down(tickets.person("ravi"), "go", ("open_website", "reply"), ("reply",))
    assert ended == {"tool": "reply", "message": "It still will not load."} and offered == [{"reply"}]


def test_the_investigation_outwaits_anyone_at_their_computer():
    assert tickets.ASK_WAIT > people.LONGEST_SITTING


def test_support_cannot_ask_what_an_earlier_round_already_asked(world, monkeypatch):
    session, asked = asking(monkeypatch)
    number = tickets.create("ravi", "no internet")
    tickets.note(number, "Service desk", "desk", "Which page fails?")
    session.record = {"event": {"ticket": number}}
    assert tools._ask_customer(session, {"message": "which page fails?"})["outcome"] == "refused" and not asked


def test_history_search_finds_a_run_by_its_ticket(world):
    store.save({"id": "r1", "event": {"time": 1000.0, "device": "h6", "subject": "internet", "title": "h6 cannot reach the internet", "event": "internet.unreachable", "source": "ticket", "ticket": 9},
                "result": {"kind": "stop", "label": "", "text": "found it"}, "rule": {"id": "none", "title": "none"}, "steps": [], "finished": 1010.0}, world)
    store._cache.clear()
    assert store.page({"q": "ticket"}, {}, world)["total"] == 1 and store.page({"q": "T-9".lower()}, {}, world)["total"] == 1 and store.page({"q": "t-8"}, {}, world)["total"] == 0
