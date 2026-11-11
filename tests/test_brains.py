"""Each agent's own memory, and the working day that fills it."""

import pytest

from drills import office, people
from ts import agent, brain, cards, desk as desk_site, llm, store, tickets, tools


def fake_meaning(monkeypatch, close_to):
    """Meaning, without the GPU: two texts are alike when they share one of the words in close_to."""
    def vector(text):
        return [1.0 if word in text.lower() else 0.0 for word in close_to] + [0.1]

    monkeypatch.setattr(brain.embed, "enabled", lambda: True)
    monkeypatch.setattr(brain.embed, "model", lambda: "m")
    monkeypatch.setattr(brain.embed, "vectors", lambda texts, _purpose="search_document": [vector(t) for t in texts])


def test_an_agent_only_ever_finds_its_own_memories():
    brain.remember("priya", "did", "The payroll system was not working on Friday.")
    brain.remember("marcus", "did", "The payroll system was fine for me.")
    found = brain.recall("priya", "payroll system working")
    assert len(found) == 1 and "Friday" in found[0]["body"]
    assert brain.recall("aiko", "payroll system working") == []


def test_only_its_own_meaning_counts_too(monkeypatch):
    fake_meaning(monkeypatch, ["connection"])
    brain.remember("priya", "did", "The connection kept cutting out at lunch.")
    brain.remember("marcus", "did", "My connection was fine.")
    assert [m["body"] for m in brain.recall("priya", "problems with the internet connection")] == ["The connection kept cutting out at lunch."]


def test_found_by_meaning_when_the_words_differ(monkeypatch):
    fake_meaning(monkeypatch, ["slow"])
    brain.remember("priya", "did", "It was so slow I gave up.")
    assert brain.recall("priya", "everything crawling slowly")


def test_one_common_word_is_not_a_memory():
    brain.remember("priya", "did", "The website for lunch orders changed its menu.")
    assert brain.recall("priya", "website payroll failing timeout since morning") == []


def test_the_oldest_memories_make_room(monkeypatch):
    monkeypatch.setattr(brain, "KEEP", 3)
    for n in range(5):
        brain.remember("priya", "did", f"thing number{n}", when=1000.0 + n)
    assert [m["body"] for m in brain.recent("priya")] == ["thing number4", "thing number3", "thing number2"]
    assert brain.recall("priya", "number0") == []


def test_a_broken_memory_never_stops_the_work(monkeypatch, tmp_path):
    monkeypatch.setattr(brain, "DB", tmp_path)  # a folder, so it can never be opened
    brain.remember("priya", "did", "anything")
    assert brain.recall("priya", "anything") == [] and brain.recent("priya") == [] and brain.context_for("priya", "anything") == ""


def test_long_runs_of_the_same_result_are_one_stretch():
    assert brain.used("sam", "the portal", "fine", 100.0) is None
    assert brain.used("sam", "the portal", "fine", 200.0) is None
    ended = brain.used("sam", "the portal", "not working", 300.0)
    assert ended["how"] == "fine" and ended["last"] == 200.0 and ended["times"] == 2
    assert [s["how"] for s in brain.stretches("sam")["the portal"]] == ["fine", "not working"]


@pytest.mark.parametrize("times, how", [([0.1, 0.2, 0.1], "fine"), ([0.1, 3.5, 0.2], "slow"), ([0.1, None, 0.1], "dropping out"), ([None, None, None], "not working")])
def test_how_a_go_felt(times, how):
    assert office.how_it_went(times) == how


def test_a_go_is_real_work_and_needs_no_model(monkeypatch):
    monkeypatch.setattr(office, "sh", lambda *_a, **_k: "200 0.05")
    monkeypatch.setattr(llm, "chat", lambda *_a, **_k: pytest.fail("the working day must not spend model calls"))
    assert office.one_go("sam", "h6") == "fine"
    assert sum(s[-1]["times"] for s in brain.stretches("sam", office.BRAINS).values()) == 1


def test_when_a_system_stops_working_it_is_remembered_as_a_change(monkeypatch):
    monkeypatch.setattr(office.random, "choice", lambda items: items[0])
    monkeypatch.setattr(office, "sh", lambda *_a, **_k: "200 0.05")
    office.one_go("sam", "h6", 1000.0)
    monkeypatch.setattr(office, "sh", lambda *_a, **_k: "000 0.00")
    office.one_go("sam", "h6", 2000.0)
    memory = brain.recent("sam", path=office.BRAINS)[0]["body"]
    assert memory.startswith("The carrier booking site was fine") and memory.endswith("then it was not working.")


def test_a_person_sits_down_knowing_how_their_day_has_gone():
    brain.used("sam", "the portal", "fine", 1000.0, office.BRAINS)
    brain.used("sam", "the portal", "fine", 2000.0, office.BRAINS)
    brain.used("sam", "the portal", "not working", 3000.0, office.BRAINS)
    words = office.remembered("sam", "the portal")
    assert "The portal: fine from" in words and "then not working at" in words


def test_everyone_has_their_own_work():
    assert {p["id"] for p in tickets.PEOPLE} == set(office.USES)
    assert len({office.uses_of(p)[0] for p in office.USES}) == len(office.USES)


def turns(monkeypatch, *made):
    plan = list(made)

    def chat(messages, tools_given, **_k):
        chat.seen.append((list(messages), tools_given))
        name, args = plan.pop(0)
        return {"message": {"role": "assistant", "content": ""}, "reasoning": "", "text": "", "calls": [{"id": "1", "name": name, "args": args}]}
    chat.seen = []
    monkeypatch.setattr(people.llm, "chat", chat)
    return chat


SAM = {"id": "sam", "name": "Sam Okafor", "team": "Warehouse", "where": "Warehouse office", "host": "h6"}


def test_thinking_back_reaches_only_their_own_memory(monkeypatch):
    brain.remember("sam", "did", "The desk told you a cable would be replaced.", path=office.BRAINS)
    brain.remember("ravi", "did", "The desk told you a laptop would be replaced.", path=office.BRAINS)
    chat = turns(monkeypatch, ("think_back", {"about": "what did the desk say about being replaced"}), ("reply", {"message": "They said a cable."}))
    people._sit_down(SAM, "They asked.", ("think_back", "reply"), ("reply",))
    answer = chat.seen[1][0][-1]["content"]
    assert "cable" in answer and "laptop" not in answer


def test_what_they_do_and_say_goes_into_their_memory(monkeypatch):
    monkeypatch.setattr(people, "_type", lambda *_a: "64 bytes from 8.8.8.8")
    turns(monkeypatch, ("run_command", {"command": "ping 8.8.8.8"}), ("raise_ticket", {"message": "Nothing loads."}))
    people._sit_down(SAM, "Go.", ("run_command", "raise_ticket"), ("raise_ticket",))
    remembered = [m["body"] for m in brain.recent("sam", path=office.BRAINS)]
    assert remembered == ["You wrote to the service desk: Nothing loads.", "You typed ping 8.8.8.8 and saw: 64 bytes from 8.8.8.8"]


def test_going_back_over_memory_cannot_go_on_forever(monkeypatch):
    chat = turns(monkeypatch, *[("think_back", {"about": "x"})] * (people.MOST_THINKING + 1), ("all_fine", {}))
    people._sit_down(SAM, "Go.", ("think_back", "all_fine"), ("all_fine",))
    assert "enough" in chat.seen[-1][0][-1]["content"]


@pytest.mark.parametrize("text", [people._WHO, people._NOTICE, people._ANSWER, *[t[0] for t in people._TOOLS.values()]])
def test_nobody_is_told_how_their_day_is_made(text):
    for word in ("simulat", "staged", "drill", " AI ", "scenario", "agent"):
        assert word not in text


def test_the_support_side_cannot_reach_what_people_remember():
    brain.remember("sam", "did", "Something only Sam knows.", path=office.BRAINS)
    assert brain.recall("sam", "something only knows") == []  # the support side's file, which is the only one it opens
    assert "office" not in open(tools.__file__).read() and "BRAINS" not in open(agent.__file__).read()


@pytest.fixture
def desk(tmp_path, monkeypatch):
    root = tmp_path / "incidents"
    root.mkdir()
    monkeypatch.setattr(store, "DATA_DIR", root)
    monkeypatch.setattr(tickets.history, "DB", tmp_path / "history.sqlite")
    return root


def test_the_desk_remembers_how_it_sorted_things_and_uses_that(desk, monkeypatch):
    asked = []
    monkeypatch.setattr(tickets.llm, "complete", lambda messages, **_k: asked.append(messages[1]["content"]) or {"text": '{"kind": "internet", "title": "No internet", "priority": "P1"}'})
    number = tickets.create("priya", "the internet stopped working on my computer")
    tickets.triage(tickets.get(number))
    assert "sorted it as internet" in brain.recent("desk")[0]["body"]
    tickets.triage(tickets.get(tickets.create("marcus", "the internet stopped working for me")))
    assert "FROM YOUR OWN EARLIER WORK" in asked[-1] and "sorted it as internet" in asked[-1]


def test_support_remembers_whether_a_conclusion_held_up(desk, monkeypatch):
    number = tickets.create("priya", "no internet")
    tickets._keep_how_it_turned_out(tickets.get(number), "it is still down", False)
    assert "did not hold up" in brain.recent("support")[0]["body"]
    assert "still not working" in brain.recent("desk")[0]["body"]
    assert "hold up" in tools.call(tools.Session({"id": "r", "event": {"time": 1.0, "device": "h1"}}, []), "recall_my_notes", {"text": "still down conclusion"})["output"]


def test_support_sees_what_this_person_wrote_before(desk):
    first = tickets.create("ravi", "the connection keeps cutting out")
    tickets._set(first, status="workaround")
    second = tickets.create("ravi", "it is slow now")
    tickets.create("sam", "something else entirely")
    lines = tickets.earlier_from(second)
    assert len(lines) == 1 and f"T-{first}" in lines[0] and "workaround" in lines[0]


def test_the_notes_agent_keeps_its_own_notes(desk, monkeypatch):
    monkeypatch.setattr(cards.llm, "SECRETS", desk)  # any path that exists
    asked = []
    monkeypatch.setattr(cards.llm, "complete", lambda messages, **_k: asked.append(messages[1]["content"]) or {"text": '{"symptom": "no internet", "cause": "a filter", "cause_key": "firewall.drop"}', "model": "m"})
    monkeypatch.setattr(cards, "account", lambda *_a, **_k: "the account of the run")
    monkeypatch.setattr(cards.rag, "set_parts", lambda *_a: None)
    monkeypatch.setattr(cards.rag, "save_pieces", lambda *_a: None)
    monkeypatch.setattr(cards.rag, "pieces_of", lambda *_a: [])
    monkeypatch.setattr(cards.rag, "_decisions", lambda *_a: [])
    monkeypatch.setattr(cards.store, "verdict", lambda _r: ("fixed", ""))
    rec = {"id": "r1", "event": {"time": 1.0, "device": "h1", "title": "no internet on h1"}, "result": {"text": "a filter dropped it", "label": "firewall.drop"}}
    cards.write_note(rec, desk / "h.sqlite")
    assert brain.recent("notes")[0]["kind"] == "note"
    cards.write_note({**rec, "id": "r2"}, desk / "h.sqlite")
    assert "NOTES YOU WROTE EARLIER" in asked[-1]


def test_a_memory_can_be_rewritten_and_is_found_by_its_new_words():
    brain.remember("sam", "did", "The label portal broke on Tuesday.")
    memory = brain.recent("sam")[0]["id"]
    assert brain.change("sam", memory, "The carrier site broke on Wednesday.")
    assert brain.recall("sam", "carrier site wednesday") and not brain.recall("sam", "label portal tuesday")


def test_one_agent_cannot_change_anothers_memory():
    brain.remember("sam", "did", "Something of mine.")
    memory = brain.recent("sam")[0]["id"]
    assert not brain.change("ravi", memory, "Overwritten.") and not brain.forget("ravi", memory)
    assert brain.recent("sam")[0]["body"] == "Something of mine."


def test_forgetting_is_for_good():
    brain.remember("sam", "did", "Something forgettable.")
    assert brain.forget("sam", brain.recent("sam")[0]["id"])
    assert brain.recall("sam", "something forgettable") == [] and brain.recent("sam") == []


def test_an_empty_memory_is_refused():
    brain.remember("sam", "did", "Keep me.")
    with pytest.raises(ValueError):
        brain.change("sam", brain.recent("sam")[0]["id"], "   ")


def act(path, data=None):
    return desk_site.Desk._act(None, path, data or {})


def test_the_dashboard_lists_every_ai_agent_and_opens_the_right_memory():
    ids = [a["id"] for a in desk_site._agents()]
    assert set(ids) == {*office.USES, "support", "desk", "notes"}
    act("/api/brains/sam", {"body": "Sam's own thought."})
    act("/api/brains/support", {"body": "Support's own thought."})
    assert [m["body"] for m in desk_site._brain_view("sam", {})["memories"]] == ["Sam's own thought."]
    assert [m["body"] for m in desk_site._brain_view("support", {})["memories"]] == ["Support's own thought."]
    assert brain.recent("sam", path=office.BRAINS) and not brain.recent("sam")  # a person's memory stays in the people's file


def test_the_dashboard_edits_and_forgets():
    act("/api/brains/ravi", {"body": "A first draft."})
    memory = brain.recent("ravi", path=office.BRAINS)[0]["id"]
    act(f"/api/brains/ravi/{memory}/edit", {"body": "A better draft."})
    assert brain.recent("ravi", path=office.BRAINS)[0]["body"] == "A better draft."
    act(f"/api/brains/ravi/{memory}/forget")
    assert brain.recent("ravi", path=office.BRAINS) == []


@pytest.mark.parametrize("path, data", [("/api/brains/nobody", {"body": "x"}), ("/api/brains/sam", {"body": "  "}), ("/api/brains/sam/999/forget", {})])
def test_the_dashboard_refuses_what_makes_no_sense(path, data):
    with pytest.raises(ValueError):
        act(path, data)


# --- a dataset for every agent ------------------------------------------------

from ts import capture, dataset, training  # noqa: E402


class FakeDb:
    """Answers the few queries the export asks, from a list of made-up conversations."""
    def __init__(self, episodes):
        self.episodes, self.result = episodes, []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql, values=None):
        key = values[0] if isinstance(values, tuple) else (values or {}).get("id")
        found = next((e for e in self.episodes if e["id"] == key), None)
        if "FROM episodes e WHERE e.status" in sql and "ORDER BY e.started_at" in sql:
            self.result = self.episodes
        elif "FROM messages" in sql:
            self.result = found["messages"]
        elif "FROM steps" in sql:
            self.result = found["steps"]
        elif "FROM labels" in sql:
            self.result = []
        elif "FROM inputs" in sql:
            self.result = [{"items": found["tools"]}] if found.get("tools") else []
        return self

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0] if self.result else None


def episode(number, kind, messages, steps=(), settings=None, tools=None, outcome=None):
    return {"id": f"e{number}", "episode_id": f"e{number}", "run_id": "", "kind": kind, "capture": "live", "model": "m", "status": "done", "outcome": outcome, "settings": settings or {}, "started": 1.0, "messages": list(messages), "steps": list(steps), "tools": tools}


def said(system, user, answer):
    return [{"role": "system", "content": system, "thinking": "", "tool_calls": None, "tool_call_id": None}, {"role": "user", "content": user, "thinking": "", "tool_calls": None, "tool_call_id": None}, {"role": "assistant", "content": answer, "thinking": "because", "tool_calls": None, "tool_call_id": None}]


def step(n, tool, args, kind="read", output="saw this"):
    return {"step_idx": n, "reason": "", "action": {"tool": tool, "args": args}, "outcome": "ok", "saw": "", "tool_output": output, "given_back": output, "kind": kind, "validation": {}, "thinking": ""}


PERSON_TOOLS = [{"type": "function", "function": {"name": "open_website", "parameters": {"properties": {"site": {}}}}}, {"type": "function", "function": {"name": "raise_ticket", "parameters": {"properties": {"message": {}}}}}]


@pytest.fixture
def gathered(monkeypatch):
    items = [
        episode(1, "desk", said(tickets._TRIAGE, "Priya wrote: no internet", '{"kind": "internet", "title": "No internet", "priority": "P1"}')),
        episode(2, "other", said(tickets._DID_IT_WORK, "yes it works", '{"resolved": true}')),
        episode(3, "desk", said(tickets._REPLY, "Outcome: solved", "This is fixed now.")),
        episode(4, "notes", said(cards._NOTE_SYSTEM, "the run", '{"symptom": "slow", "cause": "loss", "cause_key": "link.loss"}')),
        episode(5, "analyst", said(tickets._TRIAGE[:0] + __import__("ts.analyst", fromlist=["SYSTEM"]).SYSTEM, "the incident", '{"summary": "A port lost packets.", "next": "nothing"}')),
        episode(6, "person", said("You are Sam.", "Sit down.", ""), [step(1, "open_website", {"site": "x"}), step(2, "raise_ticket", {"message": "Nothing loads."}, "finish", "")], {"role": "h6", "person": "sam"}, PERSON_TOOLS, {"outcome": "raise_ticket"}),
        episode(7, "person", said("You are Ravi.", "Sit down.", ""), [step(1, "open_website", {"site": "y"})], {"role": "h7", "person": "ravi"}, PERSON_TOOLS),
        episode(8, "agent", said("You are the engineer.", "Task", ""), [step(1, "show", {"device": "r1", "command": "x"})], tools=[{"type": "function", "function": {"name": "show", "parameters": {"properties": {"device": {}, "command": {}}}}}], outcome={"outcome": "fixed"}),
    ]
    monkeypatch.setattr(capture, "open_for_reading", lambda: FakeDb(items))
    monkeypatch.setattr(dataset, "_fraction", lambda _k: 0.9)  # everything into training, so each test can count it
    return items


def rows(folder):
    import json
    return [json.loads(line) for line in (folder / "train.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("who, tools_used", [("desk", {"sort_ticket", "judge_reply", "write_to_person"}), ("notes", {"write_note", "write_read"}), ("computers", {"open_website", "raise_ticket"}), ("sam", {"open_website", "raise_ticket"}), ("support", {"show"})])
def test_every_agent_gets_its_own_examples(gathered, tmp_path, who, tools_used):
    stats = dataset.export(tmp_path / who, "graded", who)
    assert {r["answers"][0]["name"] for r in rows(tmp_path / who)} == tools_used and stats["who"] == who


def test_one_person_is_not_mixed_with_another(gathered, tmp_path):
    dataset.export(tmp_path / "ravi", "graded", "ravi")
    assert {r["meta"]["track"] for r in rows(tmp_path / "ravi")} == {"ravi"}


def test_each_job_keeps_its_own_instructions_for_training(gathered, tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(training, "_db", training._db)
    built = training.build_dataset("desk-one", "graded", db=tmp_path / "t.sqlite", root=tmp_path / "sets", who="desk")
    jobs = json.loads((tmp_path / "sets" / built["folder"] / "tools.json").read_text())
    assert set(jobs) == {"triage", "judge", "reply"} and jobs["judge"]["tools"] == [{"name": "judge_reply", "args": ["resolved"]}]
    assert built["who"] == "desk" and built["who_name"] == "Service desk"


def test_written_answers_are_not_cut_for_training():
    long = "word " * 200
    row = {"query": "q", "answers": [{"name": "write_to_person", "arguments": {"message": long}}], "meta": {"whole": True}}
    assert training._short_row(row)["answers"][0]["arguments"]["message"] == long


def test_a_tool_argument_is_still_cut():
    long = "word " * 200
    row = {"query": "q", "answers": [{"name": "try_config", "arguments": {"why": long}}], "meta": {}}
    assert len(training._short_row(row)["answers"][0]["arguments"]["why"]) < len(long)


def test_an_unparseable_answer_teaches_nothing(gathered, tmp_path):
    gathered[0]["messages"][-1]["content"] = "not json at all"
    dataset.export(tmp_path / "desk", "graded", "desk")
    assert "sort_ticket" not in {r["answers"][0]["name"] for r in rows(tmp_path / "desk")}


def test_a_dataset_must_name_a_real_agent(tmp_path):
    with pytest.raises(training.Refused):
        training.build_dataset("x", "graded", db=tmp_path / "t.sqlite", root=tmp_path, who="nobody")


def test_a_count_for_every_agent(monkeypatch):
    table = [{"kind": "person", "settings": {"person": "sam"}, "system": "You are Sam", "calls": 3, "thought": 1}, {"kind": "desk", "settings": {}, "system": "x", "calls": 1, "thought": 0}, {"kind": "other", "settings": {}, "system": cards._NOTE_SYSTEM, "calls": 1, "thought": 1}, {"kind": "agent", "settings": {}, "system": "x", "calls": 9, "thought": 9}]

    class Db(FakeDb):
        def execute(self, sql, values=None):
            self.result = table
            return self
    monkeypatch.setattr(capture, "open_for_reading", lambda: Db([]))
    got = {a["id"]: a for a in dataset.by_agent()}
    assert set(got) == {"support", "desk", "notes", "computers", *office.USES}
    assert got["sam"]["conversations"] == 1 and got["computers"]["calls"] == 3 and got["notes"]["conversations"] == 1 and got["support"]["calls"] == 9 and got["ravi"]["conversations"] == 0


def test_a_call_says_which_agent_made_it(monkeypatch):
    seen = []
    monkeypatch.setattr(capture, "ENABLED", True)
    monkeypatch.setattr(capture, "_write", lambda table, row: seen.append((table, row)))
    reply = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
    for role, kind in (("desk", "desk"), ("notes", "notes"), ("h6", "person"), ("", "other")):
        seen.clear()
        capture.llm_call({"messages": [{"role": "user", "content": "x"}], "model": "m"}, reply, 0.1, role)
        assert seen[0][0] == "episodes" and seen[0][1]["kind"] == kind


def test_a_person_at_a_computer_is_recorded(monkeypatch):
    events = []
    monkeypatch.setattr(people.capture, "start", lambda kind, run, model, settings: events.append(("start", kind, settings["person"])) or "episode")
    monkeypatch.setattr(people.capture, "inputs", lambda parts: events.append(("inputs", [p["source"] for p in parts])))
    monkeypatch.setattr(people.capture, "step", lambda entry, back: events.append(("step", entry["tool"], entry["kind"])))
    monkeypatch.setattr(people.capture, "close", lambda episode, status, outcome, messages: events.append(("close", status, outcome["outcome"])))
    monkeypatch.setattr(people, "_try_site", lambda *_a: "It loaded.")
    turns(monkeypatch, ("open_website", {"site": "x"}), ("raise_ticket", {"message": "Slow."}))
    people._sit_down(SAM, "Go.", ("open_website", "raise_ticket"), ("raise_ticket",))
    assert events == [("start", "person", "sam"), ("inputs", ["instructions", "tools", "incident"]), ("step", "open_website", "read"), ("step", "raise_ticket", "finish"), ("close", "done", "raise_ticket")]


def test_the_database_accepts_every_kind_of_agent():
    """The table's own rule decides what can be stored, and a new kind it does not list is silently lost."""
    import re
    from pathlib import Path
    schema = next(path for path in Path("deploy/training-db").glob("*.sql") if "episodes" in path.read_text()).read_text()
    allowed = re.search(r"CHECK \(kind IN \(([^)]*)\)\)", schema).group(1)
    assert all(f"'{kind}'" in allowed for kind in ("agent", "analyst", "other", "desk", "notes", "person", *capture.ALONE_KINDS.values()))


def test_a_closing_message_becomes_a_training_answer():
    import json
    messages = said(tickets._ASK_THEM, "the facts", "")
    messages[-1]["tool_calls"] = [{"id": "1", "type": "function", "function": {"name": "send_closing_message", "arguments": json.dumps({"message": "We changed a setting. Is it working?"})}}]
    example = dataset._single_example(episode(9, "desk", messages), "ask", messages)
    assert example["answers"] == [{"name": "send_closing_message", "arguments": {"message": "We changed a setting. Is it working?"}}]
