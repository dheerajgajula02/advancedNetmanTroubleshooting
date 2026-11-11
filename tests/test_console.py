"""What the new console depends on: the failures list, the live progress of the AI, and the guards on anything that writes."""

from __future__ import annotations

import collections
import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ts import agent, history, llm, overview, review, store, web
from ts.ruleset import load_ruleset

REPO = Path(__file__).resolve().parent.parent


def stored(root, run_id, when, device="r3", kind="cause", label="x.y", **extra):
    store.save({"id": run_id, "event": {"time": when, "device": device, "subject": "port", "title": f"{device} broke"}, "result": {"kind": kind, "label": label, "text": "It broke.", "step": 1}, "rule": {"id": "some-rule"},
                "steps": [{"phase": "investigate"}], "finished": when + 10, **extra}, root)


@pytest.fixture
def runs(tmp_path):
    root = tmp_path / "incidents"
    stored(root, "old", 1000.0, "s1")
    stored(root, "mid", 2000.0, "s2", repair={"status": "fixed"})
    stored(root, "new", 3000.0, "s1", agent={"status": "done", "outcome": "fixed", "calls": [{}], "changes": [], "proposals": []})
    store._cache.clear()
    return root


def test_the_time_filter_arrives_as_text(runs):
    assert [r["id"] for r in store.page({"since": "1500"}, {}, runs)["items"]] == ["new", "mid"]


def test_filters_narrow_and_the_counts_say_what_each_choice_would_show(runs):
    page = store.page({"device": "s1"}, {"new": ["routing/ospf"]}, runs)
    assert page["total"] == 2 and page["all"] == 3
    assert page["facets"]["device"] == {"s1": 2, "s2": 1}  # a filter does not hide its own alternatives
    assert page["facets"]["tag"] == {"routing/ospf": 1}


def test_a_tag_filter_takes_a_whole_area_or_one_tag(runs):
    tags = {"new": ["routing/ospf"], "mid": ["routing/bgp"], "old": ["link/port"]}
    assert store.page({"tag": "routing"}, tags, runs)["total"] == 2
    assert store.page({"tag": "routing/bgp"}, tags, runs)["total"] == 1


@pytest.mark.parametrize("sort,order", [("time", ["new", "mid", "old"]), ("device", ["mid", "new", "old"])])
def test_sorting(runs, sort, order):
    assert [r["id"] for r in store.page({"sort": sort}, {}, runs)["items"]] == order


def test_paging_and_a_page_past_the_end(runs):
    first = store.page({"size": "5", "page": "1"}, {}, runs)
    assert first["pages"] == 1 and len(first["items"]) == 3
    assert store.page({"page": "99"}, {}, runs)["page"] == 1


def test_where_a_run_stands(runs):
    states = {r["id"]: (r["state"], r["by"]) for r in store.summaries(runs)}
    assert states == {"old": ("needs_you", "rules"), "mid": ("fixed", "rules"), "new": ("fixed", "ai")}


def test_the_ai_saves_as_it_goes_so_the_page_can_follow(tmp_path, monkeypatch, rules_dir):
    monkeypatch.setattr(history, "DB", tmp_path / "h.sqlite")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "incidents")
    monkeypatch.setattr(agent, "_still_there", lambda r, rs: True)
    seen = []
    turns = [[("list_tags", {})], [("finish", {"outcome": "needs_person", "root_cause": "x", "summary": "x", "assessment": "unknown", "why": "x", "next": "x"})]]

    def model(messages, definitions, model=None, **_kw):
        if len(turns) == 1:
            seen.append(store.load("r1", tmp_path / "incidents"))
        calls = [{"id": "c", "name": n, "args": a} for n, a in turns.pop(0)]
        return {"text": "", "reasoning": "", "calls": calls, "message": {"role": "assistant", "content": ""}, "model": model, "seconds": 0}

    monkeypatch.setattr(agent.llm, "chat", model)
    rec = {"id": "r1", "event": {"time": 1.0, "device": "h1", "subject": "x", "title": "t"}, "result": {"kind": "escalate", "text": "x", "step": "end"}, "rule": {"id": "x", "position": "main", "tags": []}, "steps": [{"n": 1, "ask": "x?", "outcome": "y", "saw": "z", "phase": "investigate"}], "values": {}}
    history.add_run(rec)
    agent.take_over(rec, load_ruleset(rules_dir).parsed, lambda _m: None, root=rules_dir)
    assert seen[0]["agent"]["status"] == "working" and [c["tool"] for c in seen[0]["agent"]["calls"]] == ["list_tags"]


def test_a_person_can_mark_a_run_handled_and_take_it_back(runs):
    assert store.verdict(store.load("old", runs))[0] == "needs_you"
    assert store.set_handled("old", True, root=runs) == "handled"
    assert {r["id"]: r["state"] for r in store.summaries(runs)}["old"] == "handled"
    assert store.set_handled("old", False, root=runs) == "needs_you"


def test_only_a_run_that_needs_you_can_be_marked(runs):
    with pytest.raises(ValueError, match="needs you"):
        store.set_handled("mid", True, root=runs)
    with pytest.raises(ValueError, match="no such"):
        store.set_handled("gone", True, root=runs)


def test_the_map_lights_the_device_and_the_cable_that_are_failing(monkeypatch):
    bad = {"link:r3:eth2": {"ok": False}, "device:r3": {"ok": True}, "internet:h1": {"ok": False}, "dhcp:server": {"ok": True}, "_tick": {}}
    monkeypatch.setattr(overview.sweep, "load_state", lambda: bad)
    monkeypatch.setattr(overview.lab, "links", lambda: (("r3", "eth2", "r4", "eth2"), ("r3", "eth3", "s3", "eth1")))
    picture = overview._map([{"device": "h1", "state": "working", "time": 90.0}, {"device": "r4", "state": "needs_you", "time": 90.0}], 100.0)
    nodes = {n["id"]: n for n in picture["nodes"]}
    assert nodes["r3"]["total"] == 2 and nodes["r3"]["bad"] == ["Links"] and nodes["h1"]["working"] and nodes["r4"]["waiting"] == 1 and "dhcp-Server" in nodes
    assert [(link["a"], link["b"], link["bad"]) for link in picture["links"]] == [("r3", "r4", True), ("r3", "s3", False)]


def test_a_finding_the_sweep_does_not_watch_is_never_called_fine():
    swept, trap = {"device": "r3", "subject": "link eth2", "source": "health"}, {"device": "s3", "subject": "Ethernet1", "source": "trap"}
    assert overview._still(swept, {("r3", "link eth2")}) == "failing" and overview._still(swept, set()) == "clear" and overview._still(trap, set()) is None


def test_accepting_past_runs_closes_only_the_ones_ticked(runs, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", runs)
    stored(runs, "two", 1500.0, "s1")
    store._cache.clear()
    db = tmp_path / "decisions.sqlite"
    number = review.add("backdate", "Add port/x", "why", {"tag": "port/x", "runs": [{"run_id": "old", "why": "a"}, {"run_id": "two", "why": "b"}, {"run_id": "mid", "why": "c"}]}, print_="b1", path=db)
    assert review.accept(number, "me", runs=["old", "mid"], path=db)["closed"] == ["old"]
    states = {r["id"]: r["state"] for r in store.summaries(runs)}
    assert (states["old"], states["two"], states["mid"]) == ("handled", "needs_you", "fixed")


def test_accepting_a_rule_closes_the_run_that_raised_it(runs, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", runs)
    monkeypatch.setattr(review.rulebook, "install", lambda *_args: ["a-rule"])
    db = tmp_path / "decisions.sqlite"
    number = review.add("rule", "A rule", "because", {"rules": []}, {"replay": {"ok": True}}, run_id="old", print_="r1", path=db)
    assert review.accept(number, "me", "shadow", root=tmp_path, path=db)["closed"] == ["old"]
    assert store.load("old", runs)["handled"]["by"] == f"review:{number}"


def test_a_rule_whose_test_failed_leaves_the_run_waiting(runs, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", runs)
    monkeypatch.setattr(review.rulebook, "install", lambda *_args: ["a-rule"])
    db = tmp_path / "decisions.sqlite"
    number = review.add("rule", "A rule", "because", {"rules": []}, {"replay": {"ok": False}}, run_id="old", print_="r3", path=db)
    assert review.accept(number, "me", "shadow", root=tmp_path, path=db)["closed"] == []
    assert store.verdict(store.load("old", runs))[0] == "needs_you"


def test_rejecting_a_suggestion_leaves_the_run_waiting(runs, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", runs)
    db = tmp_path / "decisions.sqlite"
    review.reject(review.add("rule", "A rule", "because", {"rules": []}, run_id="old", print_="r2", path=db), "Not this one.", path=db)
    assert store.verdict(store.load("old", runs))[0] == "needs_you"


def test_a_healthy_device_is_not_painted_for_old_runs(monkeypatch):
    monkeypatch.setattr(overview.sweep, "load_state", lambda: {"device:r3": {"ok": True}})
    monkeypatch.setattr(overview.lab, "links", lambda: ())
    old = {"device": "r3", "subject": "link eth2", "state": "needs_you", "time": 90.0, "source": "health"}
    trap = {"device": "r3", "subject": "Ethernet1", "state": "needs_you", "time": 90.0, "source": "trap"}
    assert {n["id"]: n["waiting"] for n in overview._map([old], 100.0)["nodes"]}["r3"] == 0
    assert {n["id"]: n["waiting"] for n in overview._map([old], 100.0, {("r3", "link eth2")})["nodes"]}["r3"] == 1
    assert {n["id"]: n["waiting"] for n in overview._map([trap], 100.0)["nodes"]}["r3"] == 1


@pytest.fixture
def site(tmp_path, monkeypatch, rules_dir):
    monkeypatch.setattr(history, "DB", tmp_path / "h.sqlite")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(rules_dir))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def send(url, body=b"{}", headers=None, method="POST"):
    request = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status, json.loads(reply.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def waiting():
    return review.add("tag", "New tag link/mtu", "No tag fits.", {"tag": "link/mtu", "area": "link", "name": "mtu", "description": "MTU", "events": [], "runs": []}, print_="p1")


def test_writes_need_json(site):
    assert send(f"{site}/api/review/{waiting()}/reject", headers={"Content-Type": "text/plain"})[0] == 415


def test_writes_from_another_site_are_refused(site):
    number = waiting()
    assert send(f"{site}/api/review/{number}/reject", json.dumps({"reason": "nope nope"}).encode(), {"Origin": "http://evil.example"})[0] == 403
    assert review.get(number)["status"] == "pending"


def test_a_rejection_needs_a_reason_and_keeps_it(site):
    number = waiting()
    assert send(f"{site}/api/review/{number}/reject", json.dumps({"reason": ""}).encode())[0] == 400
    assert send(f"{site}/api/review/{number}/reject", json.dumps({"reason": "Covered by link/loss"}).encode())[0] == 200
    row = review.get(number)
    assert row["status"] == "rejected" and row["reason"] == "Covered by link/loss"
    assert send(f"{site}/api/review/{number}/reject", json.dumps({"reason": "Again please"}).encode())[0] == 400  # already decided


def test_accepting_a_tag_adds_it(site):
    assert send(f"{site}/api/review/{waiting()}/accept")[0] == 200
    assert "link/mtu" in {t["tag"] for t in json.loads(urllib.request.urlopen(f"{site}/api/tags").read())}


def test_a_rule_cannot_be_set_to_a_mode_that_does_not_exist(site):
    assert send(f"{site}/api/rules/whatever/mode", json.dumps({"mode": "banana"}).encode())[0] == 400


def test_marking_handled_over_the_web(site, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "incidents")
    monkeypatch.setattr(store.set_handled, "__defaults__", ("operator", tmp_path / "incidents"))
    stored(tmp_path / "incidents", "one", 1000.0)
    assert send(f"{site}/api/incidents/one/handled", b'{"handled": true}') == (200, {"state": "handled"})
    assert send(f"{site}/api/incidents/one/handled", b'{"handled": false}') == (200, {"state": "needs_you"})
    assert send(f"{site}/api/incidents/nobody/handled")[0] == 400


def test_the_page_loads_nothing_from_elsewhere():
    html = (REPO / "ts" / "static" / "index.html").read_text()
    assert re.findall(r"""(?:src|href)=["']([^"'#]+)""", html) == ["data:,", "base.css", "app.css", "/", "app.js"]
    for name in ("index.html", "app.js", "app.css", "base.css"):
        text = (REPO / "ts" / "static" / name).read_text()
        assert "https://" not in text and "url(http" not in text and "@import" not in text, name


def test_the_page_script_loads():
    """One bad line in this script takes the whole console down, not just one page."""
    script = (REPO / "ts" / "static" / "app.js").read_text()
    names = re.findall(r"^(?:const|let|function|async function)\\s+([A-Za-z_$][\\w$]*)", script, re.M)
    assert not [name for name, count in collections.Counter(names).items() if count > 1]
    node = shutil.which("node")
    if node:
        done = subprocess.run([node, "--input-type=module", "--check"], input=script, capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr


def test_no_page_calls_a_url_that_ad_blockers_stop():
    """uBlock's standard list blocks fetches to /api/collect, which left the Collect and Data pages blank with a network error."""
    for name in ("index.html", "app.js"):
        assert "api/collect" not in (Path(__file__).resolve().parent.parent / "ts" / "static" / name).read_text()


@pytest.mark.parametrize("name, kind", [("app.js", "text/javascript"), ("app.css", "text/css"), ("base.css", "text/css")])
def test_the_page_files_are_served(site, name, kind):
    """The Netman GUI fetches these through its own address to show the same pages in its sidebar."""
    with urllib.request.urlopen(f"{site}/{name}") as reply:
        assert reply.headers["Content-Type"].startswith(kind) and reply.read()
