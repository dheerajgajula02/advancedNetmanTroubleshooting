"""What gets kept for fine-tuning: nothing is lost, no secret is stored, and none of it can get in the way of the work."""

from __future__ import annotations

import json

import psycopg
import pytest

from ts import capture, dataset, tools


@pytest.fixture
def rows(tmp_path, monkeypatch):
    written = []
    monkeypatch.setattr(capture, "ENABLED", True)
    monkeypatch.setattr(capture, "WAITING", tmp_path / "waiting.jsonl")
    monkeypatch.setattr(capture, "PROBLEMS", tmp_path / "problems.log")
    monkeypatch.setattr(capture, "_away_until", 0.0)
    monkeypatch.setattr(capture, "_insert", lambda table, row: written.append((table, row)))
    capture._local.episode = None
    yield written
    capture._local.episode = None


def of(written, table):
    return [row for name, row in written if name == table]


def reply(content="", thinking="", calls=None):
    return {"choices": [{"message": {"role": "assistant", "content": content, "reasoning_content": thinking, "tool_calls": calls}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


@pytest.mark.parametrize("line,gone", [
    ("username admin secret sha512 $6$abcdefgh$ijklmnop", "ijklmnop"),
    ("neighbor 10.0.0.1 password 7 094F471A1A0A", "094F471A1A0A"),
    ("ip ospf message-digest-key 1 md5 7 1234ABCD", "1234ABCD"),
    ("snmp-server community hunter2 ro", "hunter2"),
    ('{"password": "topsecretvalue"}', "topsecretvalue"),
    ("Authorization: Bearer abcdefghijklmnop12345", "abcdefghijklmnop12345"),
    ("the key is sk-abcdefghijklmnopqrstuv", "sk-abcdefghijklmnopqrstuv"),
])
def test_secrets_never_get_stored(line, gone):
    assert gone not in capture.clean(line)
    assert gone not in json.dumps(capture.clean({"deep": [line]}))


def test_ordinary_text_is_left_alone():
    text = "Ethernet3 is up, line protocol is up. iptables -S shows -A FORWARD -o eth1 -j DROP"
    assert capture.clean(text) == text


def test_the_prompt_is_cut_at_its_headings():
    prompt = "INCIDENT at 2026\nh1 lost it\n\nCHECKS\n1. a -> b\n\nFROM MEMORY\n[M1] x\n\nAROUND THE SAME TIME\n10:00 y\n\nTHIS INCIDENT IS TAGGED: a\n\nSUGGESTIONS A PERSON ALREADY REJECTED FOR SOMETHING LIKE THIS.\n- #1"
    parts = capture.split_prompt(prompt)
    assert [p["section"] for p in parts] == ["The alarm", "Checks the rules ran", "From memory", "Around the same time", "Tags", "Rejected by a person"]
    assert [p["source"] for p in parts] == ["incident", "rules", "retrieved", "live", "incident", "retrieved"]
    assert "[M1] x" in parts[2]["body"]


def test_rag_results_are_snapshotted_even_when_the_prompt_was_cut_short():
    record = {"retrieved": [{"ref": "M1", "title": "t", "score": 3.2}], "similar": [], "context": [{"title": "c"}], "retrieval": {"terms": ["a"]}, "event": {}, "agent": {"prompt": "dropped"}}
    parts = capture.sections("sys", "INCIDENT at x", [], record)
    kept = {p["section"]: p for p in parts}
    assert kept["Everything retrieval returned"]["sent"] is False
    assert kept["Everything retrieval returned"]["items"]["memory"][0]["score"] == 3.2
    assert "agent" not in kept["The whole run so far"]["items"]


def test_was_cut_only_looks_at_the_part_with_a_limit():
    assert capture.was_cut("x" * capture.BRIEF_LIMIT)
    assert not capture.was_cut("x" * 100 + "\n\nTHIS INCIDENT IS TAGGED: y" + "z" * capture.BRIEF_LIMIT)


def test_the_conversation_is_stored_once_in_order_with_uncut_thinking(rows):
    long_thought = "because " * 2000
    episode = capture.start("agent", "run1", "m")
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    capture.llm_call({"model": "m", "messages": messages, "tools": [{"a": 1}]}, reply(thinking=long_thought, calls=[{"id": "1"}]), 1.5)
    messages += [{"role": "assistant", "content": "", "reasoning_content": long_thought, "tool_calls": [{"id": "1"}]}, {"role": "tool", "tool_call_id": "1", "content": "answer"}]
    capture.llm_call({"model": "m", "messages": messages, "tools": [{"a": 1}]}, reply(content="done"), 0.5)
    capture.close(episode, "done", {"outcome": "fixed"}, messages + [{"role": "assistant", "content": "done"}, {"role": "tool", "tool_call_id": "2", "content": "the last result"}])
    stored = of(rows, "messages")
    assert [m["idx"] for m in stored] == sorted({m["idx"] for m in stored})
    assert stored[2]["thinking"] == long_thought
    assert [m["role"] for m in stored][:4] == ["system", "user", "assistant", "tool"]
    assert stored[-1]["content"] == "the last result"
    calls = of(rows, "llm_calls")
    assert calls[0]["thinking"] == long_thought and calls[0]["messages_sent"] == 2 and calls[1]["messages_sent"] == 4
    assert calls[0]["raw_response"]["usage"]["prompt_tokens"] == 10
    assert of(rows, "episodes")[-1]["status"] == "done"
    assert of(rows, "labels")[0]["label"] == {"outcome": "fixed"}
    assert capture.current() is None


def test_a_model_call_outside_any_episode_is_still_recorded(rows):
    capture.llm_call({"model": "m", "messages": [{"role": "user", "content": "u"}]}, reply(content="a"), 0.1)
    assert of(rows, "episodes")[0]["kind"] == "other" and len(of(rows, "llm_calls")) == 1 and capture.current() is None


def test_a_step_keeps_the_reason_and_the_network_around_a_change(rows):
    capture.start("agent", "run1", "m")
    capture.llm_call({"model": "m", "messages": [{"role": "user", "content": "u"}]}, reply(thinking="hm"), 0.1)
    entry = {"n": 1, "tool": "try_command", "args": {"device": "r3"}, "kind": "change", "reason": "The drop line is the cause.", "outcome": "kept", "saw": "ok", "output": "full", "ran": ["iptables"], "ms": 40, "started": 1000.0}
    capture.step(entry, '{"outcome": "kept"}', {"status": "kept", "problems_before": ["a", "b"], "healed": ["a"], "new": ["c"]})
    row = of(rows, "steps")[0]
    assert row["reason"] == "The drop line is the cause." and row["llm_call"] == 0 and row["given_back"] == '{"outcome": "kept"}' and row["validation"]["change"]["status"] == "kept"
    states = {s["phase"]: s["state"]["problems"] for s in of(rows, "state_snapshots")}
    assert states == {"pre_change": ["a", "b"], "post_change": ["b", "c"]}


def test_rows_wait_while_the_database_is_away_and_go_in_order_afterwards(rows, monkeypatch):
    real, broken = capture._insert, [True]

    def insert(table, row):
        if broken[0]:
            raise psycopg.OperationalError("gone")
        real(table, row)
    monkeypatch.setattr(capture, "_insert", insert)
    capture.label("r", "system", {"n": 1})
    capture.label("r", "system", {"n": 2})
    assert not rows and len(capture.WAITING.read_text().splitlines()) == 2
    broken[0] = False
    monkeypatch.setattr(capture, "_away_until", 0.0)
    capture.label("r", "system", {"n": 3})
    assert [row["label"]["n"] for row in of(rows, "labels")] == [1, 2, 3]
    assert not capture.WAITING.exists()


def test_a_database_that_is_away_is_not_asked_again_for_a_minute(rows, monkeypatch):
    calls = []

    def insert(table, row):
        calls.append(table)
        raise psycopg.OperationalError("gone")
    monkeypatch.setattr(capture, "_insert", insert)
    for n in range(5):
        capture.label("r", "system", {"n": n})
    assert len(calls) == 1


def test_a_bad_row_is_logged_and_never_raises(rows, monkeypatch):
    def insert(table, row):
        raise psycopg.DataError("bad")
    monkeypatch.setattr(capture, "_insert", insert)
    capture.label("r", "system", {"n": 1})
    assert "could not store" in capture.PROBLEMS.read_text()
    assert not capture.WAITING.exists()


def test_nothing_is_written_when_switched_off(rows, monkeypatch):
    monkeypatch.setattr(capture, "ENABLED", False)
    assert capture.start("agent", "r", "m") is None
    capture.run({"id": "x"})
    assert not rows


def test_every_tool_but_finish_asks_for_a_reason():
    for item in tools.definitions():
        function = item["function"]
        if function["name"] == "finish":
            assert "reason" not in function["parameters"]["properties"]
        else:
            assert "reason" in function["parameters"]["properties"] and "reason" in function["parameters"]["required"]


def test_the_reason_is_kept_apart_from_the_arguments(monkeypatch):
    session = tools.Session({"id": "r", "event": {"device": "h1", "time": 1.0}, "result": {"kind": "stop"}, "steps": []}, [])
    monkeypatch.setattr(tools.history, "audit", lambda *a, **k: None)
    tools.call(session, "list_tags", {"reason": "To see which tag fits."})
    assert session.calls[0]["reason"] == "To see which tag fits."
    assert "reason" not in session.calls[0]["args"]


def test_a_tool_call_without_a_reason_still_runs(monkeypatch):
    session = tools.Session({"id": "r", "event": {"device": "h1", "time": 1.0}, "result": {"kind": "stop"}, "steps": []}, [])
    monkeypatch.setattr(tools.history, "audit", lambda *a, **k: None)
    result = tools.call(session, "list_tags", {})
    assert result["outcome"] != "invalid" and session.calls[0]["reason"] == ""


def test_training_examples_use_the_hackathons_layout():
    steps = [{"action": {"tool": "show", "args": {"device": "r3"}}, "reason": "look", "tool_output": "out" * 300, "saw": ""} for _ in range(8)]
    query = dataset.render_query("Fix it", steps)
    assert query.startswith("Task: Fix it\nEarlier calls (2 total, last 2 shown):")
    assert '1. show(device="r3") - look' in query
    assert "Latest calls and results:" in query and "...[cut]" in query
    assert dataset.render_query("Fix it", []) == "Task: Fix it\nNo calls made yet."


def test_the_split_is_stable():
    assert dataset._fraction("run-1") == dataset._fraction("run-1") and 0 <= dataset._fraction("run-1") <= 1


def test_a_change_that_was_put_back_ends_what_is_worth_imitating():
    steps = [{"outcome": "up", "validation": {}}, {"outcome": "reverted", "validation": {"change": {"status": "reverted"}}}, {"outcome": "up", "validation": {}}]
    assert dataset._first_mistake(steps) == 1
