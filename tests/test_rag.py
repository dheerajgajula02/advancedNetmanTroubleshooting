"""How past runs are stored, found, and given to the AI as there come to be more of them."""

import http.client
import json
import time

import pytest

from ts import cards, embed, history, llm, rag, store, tools


def run(root, run_id, when, title="r3 broke", device="r3", cause_key="firewall.forward_blocked", calls=None, changes=None, subject="eth3", kind="link.degraded", **extra):
    record = {"id": run_id, "event": {"time": when, "device": device, "subject": subject, "title": title, "event": kind}, "result": {"kind": "cause", "label": cause_key, "text": "found it"},
              "rule": {"id": "r", "title": "r"}, "steps": [{"n": 1, "ask": "ping", "outcome": "ok", "saw": "replied"}], "finished": when + 1,
              "agent": {"status": "done", "outcome": "fixed" if changes else "needs_person", "calls": calls or [], "changes": changes or [], "root_cause": ""}, **extra}
    store.save(record, root)
    return store.load(run_id, root)


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "incidents"
    root.mkdir()
    monkeypatch.setattr(store, "DATA_DIR", root)
    monkeypatch.setattr(history, "DB", tmp_path / "history.sqlite")
    monkeypatch.setattr(cards.llm, "SECRETS", tmp_path / "none")
    rag._matrices.clear()
    return root


def remember(root, *args, **kwargs):
    record = run(root, *args, **kwargs)
    cards.write_note(record)
    return record


SHOW = {"tool": "show", "args": {"device": "r3", "command": "tc qdisc show dev eth3"}, "reason": "look for shapers", "outcome": "ok", "saw": "Ran on r3.", "output": "qdisc netem 8004: root limit 1000 loss 30%"}


def test_a_word_only_a_command_printed_is_found_without_the_gpu(world):
    remember(world, "a", 1000.0, calls=[SHOW])
    hits, by_meaning = rag.find("netem", before=2000.0)
    assert [hit["run_id"] for hit in hits] == ["a"] and not by_meaning
    assert any("netem 8004" in why["text"] for why in hits[0]["why"]) and hits[0]["why"][0]["facet"] == "command"


def test_a_long_output_comes_back_as_the_part_that_matched(world):
    filler = "\n".join(f"line {n} of output" for n in range(200))
    remember(world, "a", 1000.0, calls=[{**SHOW, "output": f"{filler}\n-A FORWARD -o eth1 -j DROP\n{filler}"}])
    hits, _ = rag.find("FORWARD DROP eth1", before=2000.0)
    assert any("-A FORWARD -o eth1 -j DROP" in why["text"] and len(why["text"]) < rag.EXCERPT + 10 for why in hits[0]["why"])


def test_finding_by_meaning_and_by_words_are_combined(world, monkeypatch):
    remember(world, "by-words", 1000.0, title="a qdisc thing", subject="x")
    remember(world, "by-meaning", 1001.0, title="something else", subject="y", cause_key="other.cause")
    monkeypatch.setattr(rag, "nearest", lambda *a, **k: [("by-meaning|symptom|1", 0.9)])
    hits, by_meaning = rag.find("qdisc", before=2000.0)
    assert {hit["run_id"] for hit in hits} == {"by-words", "by-meaning"} and by_meaning


def test_the_named_device_decides_between_two_equal_matches(world):
    remember(world, "on-r4", 1000.0, title="link trouble with loss", device="r4", cause_key="one.cause")
    remember(world, "on-r3", 1000.0, title="link trouble with loss", device="r3", cause_key="two.cause")
    hits, _ = rag.find("link trouble with loss", ["r3"], before=2000.0)
    assert hits[0]["run_id"] == "on-r3"


def test_a_future_run_cannot_be_found(world):
    remember(world, "later", 5000.0, calls=[SHOW])
    assert rag.find("netem", before=2000.0)[0] == []


def test_this_run_is_never_its_own_answer(world):
    remember(world, "a", 1000.0, calls=[SHOW])
    assert rag.find("netem", exclude={"a"})[0] == []


def test_repeats_of_the_same_thing_fold_into_one_with_a_count(world):
    for n in range(4):
        remember(world, f"flap{n}", 1000.0 + n, title="s1 Ethernet4 went down", device="s1", subject="Ethernet4", cause_key="unknown", kind="interface.down")
    hits, _ = rag.find("s1 Ethernet4 went down", before=2000.0)
    assert len(hits) == 1 and len(hits[0]["like"]) == 3


def test_one_cause_cannot_fill_every_slot(world):
    for n in range(6):
        remember(world, f"fw{n}", 1000.0 + n, title="no internet at the border", device=f"h{n}", cause_key="firewall.x")
    remember(world, "nat", 1000.0, title="no internet at the border", device="h9", cause_key="nat.missing")
    hits, _ = rag.find("no internet at the border", before=2000.0, k=5)
    assert len([hit for hit in hits if hit["key"] == "firewall.x"]) == rag.SAME_CAUSE_SHOWN and "nat" in [hit["run_id"] for hit in hits]


def test_a_fix_that_worked_ranks_above_a_run_that_found_nothing(world):
    remember(world, "nothing", 1000.0, title="link trouble", cause_key="unknown", device="r5")
    remember(world, "fixed", 1000.0, title="link trouble", cause_key="link.bad", device="r6", changes=[{"status": "kept", "device": "r6", "what": ["tc qdisc del dev eth3 root"], "saw": "better"}])
    assert rag.find("link trouble", before=2000.0)[0][0]["run_id"] == "fixed"


def test_the_map_keeps_every_run_in_a_space_that_does_not_grow(world, monkeypatch):
    monkeypatch.setattr(rag, "MAP_CHARS", 500)
    for n in range(40):
        remember(world, f"run{n}", 1000.0 + n, title=f"fault {n}", cause_key=f"area.cause{n}")
    text, count = rag.history_map()
    assert count == 40 and "(40 runs in 40 groups" in text and len(text) < 3000
    assert "Older, by count" in text and "area.cause0 x1" in text  # the oldest still named, not dropped


def test_a_group_shows_what_worked_and_a_daily_habit(world):
    day = 86400.0
    for n in range(3):
        remember(world, f"d{n}", 1_700_000_000.0 + n * day, title="flap", cause_key="flap.nightly", changes=[{"status": "kept", "device": "r3", "what": ["fix"], "saw": "ok"}])
    text, _ = rag.history_map()
    assert "3 runs" in text and "on 3 different days" in text and "fixed 3 times" in text and "Latest: d2" in text


def test_a_run_whose_cause_was_never_found_groups_by_place(world):
    for name, device in (("a", "s1"), ("b", "s1"), ("c", "s2")):
        remember(world, name, 1000.0, title="down", device=device, cause_key="unknown", kind="interface.down")
    text, _ = rag.history_map()
    assert "cause never found: interface.down on s1 eth3 (2 runs" in text and "cause never found: interface.down on s2 eth3 (1 run" in text


def test_every_finished_run_adds_to_the_store_and_changes_are_picked_up(world):
    remember(world, "a", 1000.0, calls=[SHOW])
    before = rag.status()
    assert before["runs"] == 1 and before["pieces"] > 3
    remember(world, "b", 1100.0, calls=[SHOW, SHOW])
    assert rag.status()["runs"] == 2 and rag.status()["pieces"] > before["pieces"]
    with history._open() as db:
        db.execute("INSERT INTO proposals(kind, status, created, decided, run_id, title, why, body, proof, model, fingerprint, reason, decided_by) VALUES ('rule','rejected',1,?,'a','drop the qdisc','because','','','','','it hid the fault','me')", (time.time(),))
    assert rag.sync()["runs"] == 1
    assert rag.find("hid the fault", before=2000.0)[0][0]["run_id"] == "a"
    assert rag.sync()["runs"] == 0  # the second pass finds nothing left to do


def test_vectors_arrive_later_when_the_gpu_comes_back(world, monkeypatch):
    remember(world, "a", 1000.0, calls=[SHOW])
    monkeypatch.setattr(embed, "enabled", lambda: True)
    monkeypatch.setattr(embed, "model", lambda: "m1")
    monkeypatch.setattr(embed, "vectors", lambda *a, **k: None)
    assert rag.sync()["vectors"] == 0
    asked = []
    monkeypatch.setattr(embed, "vectors", lambda texts, purpose="search_document": asked.append(len(texts)) or [[1.0, 0.0] for _ in texts])
    made = rag.sync()["vectors"]
    assert made == sum(asked) and made > 0 and rag.sync()["vectors"] == 0
    monkeypatch.setattr(embed, "model", lambda: "m2")  # a new model has nothing stored, so it starts over
    assert rag.sync()["vectors"] == made


def test_the_checks_every_run_repeats_are_kept_for_words_only(world, monkeypatch):
    remember(world, "a", 1000.0)
    stored = []
    monkeypatch.setattr(embed, "enabled", lambda: True)
    monkeypatch.setattr(embed, "model", lambda: "m1")
    monkeypatch.setattr(embed, "vectors", lambda texts, purpose="search_document": stored.extend(texts) or [[1.0] for _ in texts])
    rag.sync()
    assert stored and not any("ping -> ok" in text for text in stored)
    assert rag.find("replied", before=2000.0)[0]


def test_pieces_are_scrubbed(world):
    remember(world, "a", 1000.0, calls=[{**SHOW, "output": "clab-lab1-r3 in containerlab"}])
    with history._open() as db:
        everything = " ".join(row["body"] + row["head"] for row in db.execute("SELECT * FROM pieces"))
    assert "clab" not in everything.lower() and "containerlab" not in everything.lower()


def test_the_search_tool_says_when_nothing_matches(world):
    session = type("S", (), {"run_id": "me"})()
    out = tools._search_runs(session, {"text": "zzznothing"})
    assert out["saw"] == "Nothing in past runs matches that." and "No past run" in out["output"]


def test_the_search_tool_gives_the_passage_that_matched(world):
    remember(world, "a", 1000.0, calls=[SHOW])
    session = type("S", (), {"run_id": "me"})()
    out = tools._search_runs(session, {"text": "netem"})
    assert "a  cause firewall.forward_blocked" in out["output"] and "matched in command" in out["output"] and "netem 8004" in out["output"]


def test_every_turn_of_one_investigation_is_one_session(monkeypatch):
    sent = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self, *_a):
            return json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}).encode()

    monkeypatch.setattr(llm.aiconfig, "_opencode", lambda: {"OPENCODE_BASE": "http://x", "OPENCODE_API_KEY": "k"})
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda request, timeout: sent.append(request.get_header("X-opencode-session")) or Answer())
    llm.start_conversation()
    for _ in range(3):
        llm.chat([{"role": "user", "content": "hi"}], [])
    llm.end_conversation()
    llm.chat([{"role": "user", "content": "hi"}], [])
    assert len(set(sent[:3])) == 1 and sent[3] != sent[0]


def test_each_model_gets_the_words_it_expects_in_front(monkeypatch):
    asked = {}
    monkeypatch.setattr(embed, "enabled", lambda: True)
    monkeypatch.setattr(embed, "_base", lambda: "http://x")
    monkeypatch.setattr(embed, "_ask", lambda _base, texts: asked.setdefault("texts", texts) or [[1.0]])
    monkeypatch.setattr(embed, "model", lambda: "nomic-embed-text")
    embed.vectors(["where"], "search_query")
    assert asked["texts"] == ["search_query: where"]
    asked.clear()
    monkeypatch.setattr(embed, "model", lambda: "bge-m3:latest")
    embed.vectors(["where"], "search_query")
    assert asked["texts"] == ["where"]


def test_a_connection_dropped_halfway_through_an_answer_is_retried(monkeypatch):
    tries = []

    def drops_once(request, timeout):
        tries.append(1)
        raise http.client.RemoteDisconnected("gone")

    monkeypatch.setattr(llm.aiconfig, "_opencode", lambda: {"OPENCODE_BASE": "http://x", "OPENCODE_API_KEY": "k"})
    monkeypatch.setattr(llm.urllib.request, "urlopen", drops_once)
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "hi"}], [], retries=2)
    assert len(tries) == 3


def test_sharing_one_common_word_is_not_a_match(world):
    remember(world, "a", 1000.0, title="default route missing on h5")
    session = type("S", (), {"run_id": "me"})()
    assert tools._search_runs(session, {"text": "bgp route reflector cluster misconfigured"})["saw"] == "Nothing in past runs matches that."
    assert tools._search_runs(session, {"text": "default route missing"})["saw"] == "1 past runs."


def test_a_finished_run_is_remembered_straight_away_when_the_watcher_is_running(world, monkeypatch):
    monkeypatch.setattr(cards, "AUTO", True)
    record = run(world, "a", 1000.0, calls=[SHOW])
    started = []
    real_thread = cards.threading.Thread

    def recording(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        started.append(worker)
        return worker

    monkeypatch.setattr(cards.threading, "Thread", recording)
    cards.remember_soon(record)
    started[0].join(20)
    assert rag.find("netem", before=2000.0)[0][0]["run_id"] == "a"
