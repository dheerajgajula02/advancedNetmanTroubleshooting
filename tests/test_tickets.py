"""Tickets: who raised them, how they are worked, and what the person is told."""

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ts import desk, store, tickets
from ts.vocab import EVENT_TYPES, SOURCES


def stored(root, run_id, device="h1", **extra):
    store.save({"id": run_id, "event": {"time": 1000.0, "device": device, "subject": "internet", "title": "h1 cannot reach the internet", "event": "internet.unreachable"},
                "result": {"kind": "cause", "label": "x.y", "text": "found it"}, "rule": {"id": "r", "title": "r"}, "steps": [], "finished": 1010.0, **extra}, root)
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
    monkeypatch.setattr(tickets, "_internet_target", lambda _host: ("8.8.8.8", "0.0.0.0/0"))
    return root


def test_a_ticket_needs_a_person_and_some_words(world):
    with pytest.raises(ValueError, match="who"):
        tickets.create("nobody", "my internet is down")
    with pytest.raises(ValueError, match="what is going wrong"):
        tickets.create("priya", "  ")


def test_a_ticket_starts_from_that_persons_computer(world):
    ticket = tickets.get(tickets.create("lena", "I'm not getting internet"))
    event = tickets.event_from(ticket, {"kind": "internet", "title": "No internet", "priority": "P2"})
    assert event["device"] == "h5" and event["source"] == "ticket" and event["ticket"] == ticket["id"] and event["event"] == "ticket.report"
    assert "not getting internet" in event["detail"] and "Lena Novak" in event["detail"]


def test_trouble_with_no_rule_goes_to_an_investigation_as_a_ticket_event(world):
    ticket = tickets.get(tickets.create("marcus", "Everything is slow"))
    event = tickets.event_from(ticket, {"kind": "other", "title": "Slow network", "priority": "P3"})
    assert event["event"] == "ticket.report" and event["event"] in EVENT_TYPES and event["source"] in SOURCES


def finish_with(world, run_id, **extra):
    def investigate(event):
        stored(world, tickets.engine.incident_id(event), device=event["device"], **extra)
        return "done"
    return investigate


def test_a_fixed_run_asks_the_person_before_it_counts_as_solved(world):
    number = tickets.create("priya", "no internet")
    outcome = tickets.work_one(tickets.get(number), finish_with(world, "x", repair={"status": "fixed"}), lambda _m: None, {"kind": "internet", "title": "No internet", "priority": "P1"})
    found = tickets.get(number)
    assert outcome == tickets.CONFIRMING and found["status"] == tickets.CONFIRMING and found["then_status"] == tickets.SOLVED and found["checked"] == 1 and found["run_id"] and found["waiting_note"]
    assert any("see it working" in n["body"] for n in tickets.notes_of(number, private=False))


def test_fixed_but_still_broken_from_their_computer_goes_to_a_person(world, monkeypatch):
    monkeypatch.setattr(tickets, "_works_from_their_computer", lambda *_a: False)
    number = tickets.create("priya", "no internet")
    assert tickets.work_one(tickets.get(number), finish_with(world, "x", repair={"status": "fixed"}), lambda _m: None, {"kind": "internet", "title": "t", "priority": "P1"}) == tickets.NEEDS_AGENT


def test_a_fix_that_works_from_their_computer_counts_even_if_the_ai_was_cut_off(world):
    number = tickets.create("priya", "no internet")
    done = tickets.work_one(tickets.get(number), finish_with(world, "x", agent={"status": "failed", "outcome": "needs_person", "calls": [], "changes": [{"status": "kept", "device": "h1", "what": ["ip route add x"]}]}), lambda _m: None, {"kind": "internet", "title": "t", "priority": "P1"})
    assert done == tickets.CONFIRMING and tickets.get(number)["then_status"] == tickets.SOLVED


def test_what_a_person_cannot_see_stays_with_the_team(world):
    number = tickets.create("priya", "no internet")
    tickets.note(number, "Service desk", "desk", "root cause notes", private=True)
    seen = tickets.view(number, for_person=True)
    assert all("root cause" not in n["body"] for n in seen["notes"]) and "run" not in seen and "host" not in seen


def test_a_ticket_joins_a_run_already_working_on_it(world, monkeypatch):
    stored(world, "busy", agent={"status": "working", "calls": []})
    monkeypatch.setattr(tickets, "_wait_for", lambda run_id, _n: store.load(run_id, store.DATA_DIR))
    called = []
    number = tickets.create("priya", "no internet")
    tickets.work_one(tickets.get(number), called.append, lambda _m: None, {"kind": "internet", "title": "t", "priority": "P1"})
    assert not called and tickets.get(number)["run_id"] == "busy"


def test_still_broken_goes_back_in_line_for_a_fresh_look(world):
    number = tickets.create("priya", "no internet")
    tickets.work_one(tickets.get(number), finish_with(world, "x", repair={"status": "fixed"}), lambda _m: None, {"kind": "internet", "title": "t", "priority": "P1"})
    tickets.reopen(number, "Priya", "it broke again")
    found = tickets.get(number)
    assert found["status"] == tickets.NEW and found["run_id"] == ""


def test_tickets_are_worked_oldest_first(world):
    first, second = tickets.create("priya", "no internet"), tickets.create("marcus", "slow")
    seen = tickets.snapshot()
    assert {t["id"]: t["place_in_line"] for t in seen["tickets"]} == {first: 1, second: 2}


def test_a_rule_that_cannot_see_the_problem_does_not_close_a_persons_ticket():
    from ts import agent
    record = {"event": {"source": "ticket"}, "result": {"kind": "stop", "step": 1}, "steps": [{"phase": "confirm", "n": 1}], "rule": {"id": "dhcp"}}
    assert "still says" in agent.needed(record)
    assert agent.needed({**record, "event": {"source": "trap"}}) is None
    assert agent._still_there(record, None) is True


def send(url, body=None, headers=None):
    request = urllib.request.Request(url, body, {"Content-Type": "application/json", **(headers or {})} if body is not None else {})
    try:
        with urllib.request.urlopen(request, timeout=5) as answer:
            return answer.status, json.load(answer)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


@pytest.fixture
def site(world):
    server = ThreadingHTTPServer(("127.0.0.1", 0), desk.Desk)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_the_desk_files_a_ticket_and_hides_the_team_notes_from_the_person(site):
    status, made = send(f"{site}/api/tickets", json.dumps({"person": "aiko", "body": "my computer cannot get online"}).encode())
    assert status == 200
    assert send(f"{site}/api/tickets/{made['id']}/reply", json.dumps({"who": "Sam", "body": "looking", "private": True}).encode())[0] == 200
    assert any(n["private"] for n in send(f"{site}/api/tickets/{made['id']}")[1]["notes"])
    assert not any(n["private"] for n in send(f"{site}/api/my/{made['id']}")[1]["notes"])


def test_the_desk_refuses_other_sites_and_bad_input(site):
    assert send(f"{site}/api/tickets", b'{"person":"aiko","body":"help me"}', {"Origin": "http://evil.example"})[0] == 403
    assert send(f"{site}/api/tickets", b'{"person":"nobody","body":"help me"}')[0] == 400


def test_the_desk_page_loads_nothing_from_elsewhere():
    page = (desk.STATIC / "desk.html").read_text()
    assert not re.search(r"""(?:src|href)=["']https?://""", page) and "url(http" not in page
