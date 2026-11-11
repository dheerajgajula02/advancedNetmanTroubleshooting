"""What each new run is given about the past."""

import pytest

from ts import cards, history, store


def run(root, run_id, when, title, cause, **extra):
    store.save({"id": run_id, "event": {"time": when, "device": "r3", "subject": "port", "title": title, "event": "internet.unreachable"}, "result": {"kind": "cause", "label": "a.b", "text": cause},
                "rule": {"id": "r", "title": "r"}, "steps": [{"n": 1, "ask": "ping", "outcome": "ok", "saw": "replied"}], "finished": when + 1, **extra}, root)
    store._cache.clear()
    return store.load(run_id, root)


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "incidents"
    root.mkdir()
    monkeypatch.setattr(store, "DATA_DIR", root)
    monkeypatch.setattr(history, "DB", tmp_path / "history.sqlite")
    monkeypatch.setattr(cards.llm, "SECRETS", tmp_path / "none")
    return root


def test_the_full_account_keeps_what_the_ai_did_and_every_command(world):
    rec = run(world, "a", 1000.0, "r3 broke", "cause", agent={"status": "done", "outcome": "fixed", "root_cause": "forwarding off", "why": "w", "calls": [{"tool": "show", "args": {"device": "r3"}, "reason": "look", "outcome": "ok", "saw": "seen", "output": "RAW-OUTPUT"}], "changes": [{"status": "kept", "device": "r3", "what": ["sysctl -w x"], "saw": "worked"}]})
    text = cards.account(rec)
    assert "forwarding off" in text and "sysctl -w x" in text and "kept" in text and "RAW-OUTPUT" not in text
    assert "RAW-OUTPUT" in cards.account(rec, outputs=True)


def test_without_the_model_a_run_still_gets_a_plain_note(world):
    rec = run(world, "a", 1000.0, "r3 broke", "the cable")
    assert "the cable" in cards.write_note(rec)


def test_every_past_run_is_in_the_map_but_not_this_one(world):
    for n in range(3):
        cards.write_note(run(world, f"r{n}", 1000.0 + n, f"title {n}", "thing"))
    current = run(world, "r2", 1002.0, "title 2", "thing")
    info = cards.prepare(current)
    text = cards.render(current)
    assert info["notes"] == 2 and "EVERY PAST RUN, GROUPED BY CAUSE (2 runs" in text and "Latest: r1" in text and "r2" not in text


def test_the_closest_runs_arrive_as_cases(world):
    past = run(world, "old", 1000.0, "h1 cannot reach the internet", "forwarding switched off on the border")
    history.add_run(past)
    cards.write_note(past)
    now = run(world, "new", 2000.0, "h1 cannot reach the internet", "unknown")
    assert cards.prepare(now)["closest"] == ["old"]
    told = cards.render(now)
    assert "CLOSEST PAST RUNS" in told and "CASE old" in told and "forwarding switched off" in told


def test_a_run_is_not_told_about_runs_that_came_after_it(world):
    later = run(world, "later", 5000.0, "h1 cannot reach the internet", "forwarding switched off")
    cards.write_note(later)
    now = run(world, "new", 2000.0, "h1 cannot reach the internet", "unknown")
    assert cards.prepare(now)["closest"] == [] and "later" not in cards.render(now)
