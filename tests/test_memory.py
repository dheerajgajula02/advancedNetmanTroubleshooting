"""Long memory: what goes in, what a run finds again, and what reaches the model."""

from __future__ import annotations

import sqlite3

import pytest

from ts import analyst, history, memory, report, sources

NOW = 1_800_000_000.0
DAY = 86400


def doc(source="prometheus", age_days=3.0, device="r3", title="r3 eth3: port status changed 2 times", text=""):
    return {"source": source, "time": NOW - age_days * DAY, "device": device, "title": title, "text": text, "url": ""}


def run(device="s3", interface="Ethernet1", label="interface.far_end_shut", text="The far end, eth3 on r3, was shut down by hand.", when=NOW):
    return {"id": "this-run", "event": {"time": when, "device": device, "interface": interface, "subject": interface, "title": f"{device} {interface} went down"},
            "result": {"kind": "cause", "label": label, "text": text, "params": {"far_device": "r3", "far_interface": "eth3"}},
            "rule": {"id": "interface-down"}, "steps": [{"n": 1, "ask": "Is it down?", "outcome": "down", "saw": "It is down."}], "values": {"peer_device": "r3", "peer_interface": "eth3"}}


@pytest.fixture
def db(tmp_path):
    return tmp_path / "history.sqlite"


def found(db, rec=None, **kw):
    return memory.recall(rec or run(), path=db, **kw)[0]


def test_short_device_names_are_searched_and_filler_is_not():
    assert history.terms("r3 was the eth3 on s3") == ["r3", "eth3", "s3"]


def test_the_most_telling_words_survive_the_limit():
    assert history.terms("r3 eth3", "zebra " * 3 + " ".join(f"word{n}" for n in range(60)), limit=3)[:2] == ["r3", "eth3"]


def test_it_finds_an_older_record_about_the_same_place(db):
    history.add_docs([doc()], db)
    assert [m["title"] for m in found(db)] == ["r3 eth3: port status changed 2 times"]


def test_word_stems_match(db):
    history.add_docs([doc(title="Commit abc: stop r3 dropping packets", source="github", device="")], db)
    words = history.terms("dropped")
    assert history.search_docs(words, before=NOW, path=db)


def test_what_is_happening_right_now_is_left_to_the_live_context(db):
    history.add_docs([doc(age_days=(sources.BEFORE - 60) / DAY)], db)
    assert found(db) == []


def test_records_after_the_incident_are_not_used(db):
    history.add_docs([doc(age_days=-1)], db)
    assert found(db) == []


def test_sharing_a_generic_word_is_not_a_connection(db):
    history.add_docs([doc(device="r1", title="r1 Ethernet9: link is down")], db)
    assert found(db) == []


def test_a_change_that_names_the_device_is_found_even_without_a_device_field(db):
    history.add_docs([doc(source="github", device="", title="Commit 1a2b3c4 by dheeraj: raise OSPF cost on r3 eth3")], db)
    assert [m["source"] for m in found(db)] == ["github"]


def test_repeats_collapse_into_one_record_with_a_count(db):
    history.add_docs([doc(age_days=n + 1, title=f"r3 eth3: port status changed {n + 2} times") for n in range(5)], db)
    items = found(db)
    assert len(items) == 1 and items[0]["times_seen"] == 5


def test_different_devices_do_not_collapse_together(db):
    history.add_docs([doc(), doc(device="s3", title="s3 Ethernet1: port status changed 2 times")], db)
    assert len(found(db)) == 2


def test_one_source_cannot_fill_the_list(db):
    history.add_docs([doc(title=f"r3 eth3: counter{chr(97 + n)} rose") for n in range(9)] + [doc(source="jenkins", device="", title="Job deploy #1: FAILURE", text="failed on r3")], db)
    items = found(db)
    assert sum(m["source"] == "prometheus" for m in items) == memory.PER_SOURCE
    assert any(m["source"] == "jenkins" for m in items)


def test_every_result_can_be_cited(db):
    history.add_docs([doc(), doc(device="s3", title="s3 Ethernet1: port status changed 2 times")], db)
    assert [m["ref"] for m in found(db)] == ["M1", "M2"]


def test_the_cause_device_counts_even_when_the_alarm_came_from_elsewhere():
    rec = run(device="h4", interface=None, label="border.routes_missing", text="r3 has lost the IPv6 routes.")
    rec["result"]["params"] = {"device": "r3"}
    rec["values"] = {}
    assert memory.involved(rec)[:2] == ["r3", "h4"]


def test_r5_is_never_stored_or_named():
    assert not memory._worth_keeping(doc(device="r5", title="r5 Ethernet1: port status changed 2 times"))
    rec = run(text="r5 and r3 are involved.")
    assert "r5" not in memory.involved(rec)


def test_builds_still_running_and_rolling_totals_are_not_stored():
    assert not memory._worth_keeping(doc(source="jenkins", device="", title="Job x #1: RUNNING"))
    assert not memory._worth_keeping(doc(title="Over the last 7 days r3 eth3 changed state 9 times"))
    assert not memory._worth_keeping(doc(title="Also around then: s3 Ethernet1 changed state 2 times"))


def test_ingest_reads_every_source_and_a_broken_one_stops_only_itself(db, monkeypatch):
    monkeypatch.setattr(history, "DB", db)
    calls = []

    def fine(start, end, device):
        calls.append((start, end))
        return [sources.Doc("good", end, f"r3 eth3: changed at {end:.0f}", device="r3")]

    def broken(start, end, device):
        raise RuntimeError("no")

    monkeypatch.setattr(sources, "SOURCES", {"good": fine, "bad": broken})
    monkeypatch.setattr(sources, "load", lambda: sources.SOURCES)
    monkeypatch.setattr(memory, "CHUNK", {"good": 1800})
    added = memory.ingest(NOW - 3600, NOW)
    assert added == {"good": 2, "bad": 0}
    assert calls == [(NOW - 3600, NOW - 1800), (NOW - 1800, NOW)]
    assert memory.ingest(NOW - 3600, NOW) == {"good": 0, "bad": 0}


def test_keep_up_picks_up_where_it_left_off(db, monkeypatch):
    monkeypatch.setattr(history, "DB", db)
    asked = []
    monkeypatch.setattr(memory, "ingest", lambda start, end, log: asked.append((start, end)) or {})
    monkeypatch.setattr(memory.time, "time", lambda: NOW)
    memory.keep_up()
    assert asked[-1] == (NOW - memory.FIRST_LOOK_BACK, NOW)
    monkeypatch.setattr(memory.time, "time", lambda: NOW + 600)
    memory.keep_up()
    assert asked[-1] == (NOW, NOW + 600)


def test_a_store_from_before_stemming_keeps_everything(db):
    old = sqlite3.connect(db)
    old.executescript("""CREATE VIRTUAL TABLE runs_fts USING fts5(id UNINDEXED, body); CREATE VIRTUAL TABLE docs_fts USING fts5(key UNINDEXED, title, text);
                         CREATE TABLE docs(key TEXT PRIMARY KEY, source TEXT, time REAL, device TEXT, title TEXT, text TEXT, url TEXT);
                         INSERT INTO runs_fts VALUES ('run1', 'r3 was dropping');
                         INSERT INTO docs VALUES ('k', 'github', 1.0, '', 'r3 dropping', '', '');""")
    old.commit()
    old.close()
    assert [row["key"] for row in history.search_docs(history.terms("dropped"), before=NOW, path=db)] == ["k"]
    with history._open(db) as conn:
        assert conn.execute("SELECT id FROM runs_fts WHERE runs_fts MATCH 'dropped'").fetchone()[0] == "run1"


def _ask(monkeypatch, used):
    sent = {}

    def complete(messages, **kw):
        sent["prompt"] = messages[1]["content"]
        return {"text": '{"summary": "It was shut.", "assessment": "misconfiguration", "used": %s}' % used, "model": "deepseek-v4.1-flash", "seconds": 2.0, "reasoning": "thinking"}

    monkeypatch.setattr(analyst.llm, "complete", complete)
    return sent


REMEMBERED = [{"ref": "M1", "source": "github", "time": NOW - DAY, "title": "Commit raises OSPF cost on r3", "text": "", "times_seen": 3, "last_seen": NOW - 3600}]


def test_the_model_is_shown_what_was_recalled(monkeypatch):
    sent = _ask(monkeypatch, "[]")
    analyst.analyse(run(), [], [], {}, REMEMBERED)
    assert "FROM MEMORY" in sent["prompt"] and "[M1]" in sent["prompt"] and "seen 3 times" in sent["prompt"]


def test_a_citation_of_something_it_was_never_given_is_dropped(monkeypatch):
    _ask(monkeypatch, '["M1", "M9"]')
    assert analyst.analyse(run(), [], [], {}, REMEMBERED)["used"] == ["M1"]


def test_the_run_records_when_the_ai_worked_and_what_it_saw(monkeypatch):
    _ask(monkeypatch, "[]")
    a = analyst.analyse(run(), [], [], {}, REMEMBERED)
    assert a["model"] == "deepseek-v4.1-flash" and a["started"] <= a["finished"] and "CONCLUSION" in a["prompt"]
    assert a["given"] == {"checks": 1, "repair": False, "similar": 0, "memory": 1, "live": 0}


def test_the_email_marks_where_the_ai_starts_and_what_it_recalled():
    rec = {**run(), "analysis": {"summary": "It was shut.", "model": "deepseek-v4.1-flash", "started": NOW, "used": ["M1"]}, "retrieved": REMEMBERED}
    _subject, body = report.compose(rec)
    assert "The AI starts here. deepseek-v4.1-flash read this run" in body
    assert body.index("WHAT I CHECKED") < body.index("The AI starts here")
    assert "WHAT IT FOUND IN MEMORY" in body and "Commit raises OSPF cost on r3" in body


def test_eos_short_port_names_count_as_the_same_port(db):
    history.add_docs([doc(device="s3", title="[s3] OSPF neighbor 3.3.3.3, interface Et1 adjacency dropped")], db)
    assert "et1" in memory.involved(run())
    assert found(db)


def test_another_port_on_the_same_device_ranks_below_this_one(db):
    history.add_docs([doc(device="s3", title="[s3] interface Et2 adjacency dropped"), doc(device="s3", title="[s3] interface Et1 adjacency dropped")], db)
    assert [m["title"] for m in found(db)] == ["[s3] interface Et1 adjacency dropped", "[s3] interface Et2 adjacency dropped"]


def test_messages_sent_only_to_prove_logging_works_are_ignored():
    assert not memory._worth_keeping(doc(title="[s3] CRIT: Message from root: CLAUDE-SYSLOG-PROBE-s3-sev2"))
    assert not memory._worth_keeping(doc(title="[s1] CRIT: ALERT_TEST_EOS_S1_1794047867"))


def test_common_device_names_cannot_crowd_out_what_is_on_topic(db):
    noise = [doc(age_days=2, title=f"r3 eth3 r3 eth3 chatter number{chr(97 + n)}{chr(97 + n)}") for n in range(20)]
    history.add_docs([*noise, doc(age_days=9, title="r3 lost return routes after restart", source="datalake")], db)
    rec = run(device="r3", interface="eth3", label="border.routes_missing", text="r3 has lost return routes.")
    names = memory.involved(rec)
    topic = [w for w in history.terms("return routes lost") if w not in names]
    assert [d["title"] for d in history.search_docs(topic, 1, NOW, db, among=names)] == ["r3 lost return routes after restart"]


def test_requiring_a_name_when_there_are_none_finds_nothing(db):
    history.add_docs([doc()], db)
    assert history.search_docs(["port"], 5, NOW, db, among=[]) == []
