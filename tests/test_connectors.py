"""ServiceNow and Salesforce: their tickets come to our desk, our replies and status go back, our own finds open tickets there."""

import json
import time

import pytest

from ts import connectors, tickets
from ts.connectors import salesforce, servicenow, sync


class FakeSystem:
    """A ticketing system in memory, with the same face as the real connectors."""
    name, label = "fake", "FakeDesk"

    def __init__(self):
        self.tickets, self.comments_of, self.states, self.opened = [], {}, {}, []

    def link(self, ext_id):
        return f"https://fake.example/{ext_id}"

    def file(self, caller, subject, body="", created=None):
        ext = f"x{len(self.tickets) + 1}"
        self.tickets.append({"id": ext, "number": f"INC{len(self.tickets) + 1:04}", "created": created or time.time(), "caller_name": caller, "caller_email": "", "subject": subject, "body": body})
        self.comments_of[ext] = []
        return ext

    def new_tickets(self, since):
        return [t for t in self.tickets if t["created"] >= since]

    def comments(self, ext_id):
        return list(self.comments_of[ext_id])

    def add_comment(self, ext_id, body, public):
        self.comments_of[ext_id].append({"id": f"c{sum(map(len, self.comments_of.values())) + 1}", "time": time.time(), "body": body, "author": "integration", "public": public})

    def they_write(self, ext_id, body):
        self.comments_of[ext_id].append({"id": f"c{sum(map(len, self.comments_of.values())) + 1}", "time": time.time(), "body": body, "author": "caller"})

    def set_state(self, ext_id, status, note=""):
        self.states[ext_id] = (status, note)

    def open_ticket(self, subject, body, fixed):
        self.opened.append({"subject": subject, "body": body, "fixed": fixed})
        return {"id": f"o{len(self.opened)}", "number": f"INC9{len(self.opened):03}", "url": "https://fake.example/o"}


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setattr(tickets.history, "DB", tmp_path / "history.sqlite")
    return {"db": tmp_path / "connectors.sqlite", "system": FakeSystem()}


def _pass(desk, pending=None):
    return sync.sync_once([desk["system"]], log=lambda *_: None, path=desk["db"], pending=pending or [])


def test_only_tickets_made_after_it_was_switched_on_come_in(desk):
    fake = desk["system"]
    fake.file("Priya Raman", "old printer trouble", created=time.time() - 3600)
    _pass(desk)
    assert tickets.listing() == []
    fake.file("Priya Raman", "Internet is down", "Nothing loads since 9am.")
    assert _pass(desk)["fake"]["new"] == 1
    t = tickets.listing()[0]
    assert t["person"] == "priya" and "Nothing loads since 9am." in t["body"] and t["host"] == "h1"
    assert any(n["body"].startswith(sync.CAME_IN + "FakeDesk INC0002") and n["private"] for n in tickets.notes_of(t["id"]))
    assert _pass(desk)["fake"]["new"] == 0  # never twice


def test_a_caller_the_desk_does_not_know_is_left_alone(desk):
    _pass(desk)
    desk["system"].file("Someone Else", "VPN broken")
    _pass(desk)
    assert tickets.listing() == []


def test_replies_go_back_and_their_answers_come_in_without_echoes(desk):
    fake = desk["system"]
    _pass(desk)  # switched on
    ext = fake.file("Marcus Bell", "Slow internet")
    _pass(desk)
    number = tickets.listing()[0]["id"]
    sent = fake.comments_of[ext]
    assert [c["body"] for c in sent] == ["We have this and are looking into it. We will write here if we need anything from you."]
    assert sent[0]["public"] is True
    tickets.note(number, "Service desk", "desk", "We made some changes. Can you try again?")
    tickets.note(number, "Support", "agent", "r3 eth1 flapped; held OSPF cost", private=True)
    _pass(desk)
    bodies = [(c["body"], c["public"]) for c in fake.comments_of[ext]]
    assert ("We made some changes. Can you try again?", True) in bodies
    assert ("[Support] r3 eth1 flapped; held OSPF cost", False) in bodies
    fake.they_write(ext, "Yes, it works now, thanks")
    _pass(desk)
    theirs = [n for n in tickets.notes_of(number) if n["role"] == "person"]
    assert theirs[-1]["body"] == "Yes, it works now, thanks"
    assert len(theirs) == 2  # the first report and this answer; our own comments never come back as theirs


def test_our_status_becomes_theirs(desk):
    fake = desk["system"]
    _pass(desk)  # switched on
    ext = fake.file("Aiko Tanaka", "No network")
    _pass(desk)
    number = tickets.listing()[0]["id"]
    tickets._set(number, status=tickets.SOLVED)
    tickets.note(number, "Service desk", "desk", "It was a cable at the edge of our network; fixed and checked.")
    _pass(desk)
    assert fake.states[ext] == (tickets.SOLVED, "It was a cable at the edge of our network; fixed and checked.")


def test_runs_worth_an_email_open_a_ticket_there_once(desk, monkeypatch):
    from ts import report
    monkeypatch.setattr(report, "compose", lambda rec: ("[FIXED BY AI] r3 stopped forwarding", "WHAT HAPPENED ..."))
    monkeypatch.setattr(report, "tag", lambda rec: "FIXED BY AI")
    record = {"id": "20261114-025400-r3", "event": {"source": "sweep"}}
    _pass(desk, [record])
    _pass(desk, [record])
    assert desk["system"].opened == [{"subject": "[FIXED BY AI] r3 stopped forwarding", "body": "WHAT HAPPENED ...", "fixed": True}]


def test_a_ticket_run_is_not_sent_out_again(monkeypatch):
    from ts import report
    monkeypatch.setattr(report, "should_send", lambda rec: True)
    monkeypatch.setattr(sync, "_RUNNING", {"on": True})
    while not sync.OUTBOUND.empty():
        sync.OUTBOUND.get_nowait()
    sync.after_run({"id": "a", "event": {"source": "ticket"}})
    assert sync.OUTBOUND.empty()
    sync.after_run({"id": "b", "event": {"source": "trap"}})
    assert sync.OUTBOUND.get_nowait()["id"] == "b"


def test_people_match_by_name_or_email():
    assert connectors.match_person("priya raman", "", tickets.PEOPLE)["id"] == "priya"
    assert connectors.match_person("", "sam.okafor@example.com", tickets.PEOPLE)["id"] == "sam"
    assert connectors.match_person("Nobody", "who@example.com", tickets.PEOPLE) is None


def test_login_files_switch_connectors_on(tmp_path, monkeypatch):
    monkeypatch.setattr(connectors, "SECRETS", tmp_path)
    assert connectors.configured() == []
    (tmp_path / "servicenow.env").write_text("SN_INSTANCE=dev1\nSN_USER=netman.integration\nSN_PASSWORD=x\n")
    (tmp_path / "salesforce.env").write_text("SF_DOMAIN=acme.my.salesforce.com\nSF_CLIENT_ID=a\n")  # incomplete
    assert [c.name for c in connectors.configured()] == ["servicenow"]


# --- the two real connectors, against a stand-in of their APIs ---------------------------

class Wire:
    """Records requests and answers them from a script, like requests.Session."""
    def __init__(self, answers):
        self.answers, self.sent, self.headers, self.auth = answers, [], {}, None

    def request(self, method, url, **kw):
        self.sent.append((method, url, kw))
        for (m, part), answer in self.answers.items():
            if m == method and part in url:
                status, body = answer.pop(0) if isinstance(answer, list) else answer
                return Answer(status, body)
        return Answer(404, {"error": "unexpected"})

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


class Answer:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def test_servicenow_resolves_with_a_close_code_from_the_instance():
    wire = Wire({("GET", "table/sys_choice"): (200, {"result": [{"value": {"value": "Duplicate"}, "label": {"display_value": "Duplicate"}},
                                                                 {"value": {"value": "Solution provided"}, "label": {"display_value": "Solution provided"}}]}),
                 ("PATCH", "table/incident/abc"): (200, {"result": {}})})
    c = servicenow.Connector({"SN_INSTANCE": "dev1", "SN_USER": "u", "SN_PASSWORD": "p"}, session=wire)
    c.set_state("abc", "solved", "fixed it")
    method, url, kw = wire.sent[-1]
    assert url == "https://dev1.service-now.com/api/now/table/incident/abc"
    assert kw["json"] == {"state": "6", "close_code": "Solution provided", "close_notes": "fixed it"}
    c.set_state("abc", "confirming")
    assert wire.sent[-1][2]["json"] == {"state": "3", "hold_reason": "1"}


def test_servicenow_reads_new_network_incidents_and_skips_old_ones():
    now = time.time()
    stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))
    rows = [{"sys_id": {"value": "a"}, "number": {"value": "INC1"}, "sys_created_on": {"value": stamp(now - 7200)}, "caller_id": {"value": "u1", "display_value": "Priya Raman"}},
            {"sys_id": {"value": "b"}, "number": {"value": "INC2"}, "sys_created_on": {"value": stamp(now - 30)}, "short_description": {"display_value": "No internet"},
             "caller_id": {"value": "u2", "display_value": "Lena Novak"}, "caller_id.email": {"display_value": "lena.novak@example.com"}, "sys_created_by": {"value": "admin"}}]
    wire = Wire({("GET", "table/incident"): (200, {"result": rows})})
    c = servicenow.Connector({"SN_INSTANCE": "https://dev1.service-now.com", "SN_USER": "u", "SN_PASSWORD": "p"}, session=wire)
    found = c.new_tickets(now - 600)
    assert [(t["number"], t["caller_name"], t["subject"]) for t in found] == [("INC2", "Lena Novak", "No internet")]
    assert "category=network" in wire.sent[0][2]["params"]["sysparm_query"]


def test_salesforce_signs_in_again_when_the_token_expires():
    wire = Wire({("POST", "oauth2/token"): [(200, {"access_token": "t1", "instance_url": "https://acme.my.salesforce.com", "id": "https://login/id/00D/005ME"}),
                                            (200, {"access_token": "t2", "instance_url": "https://acme.my.salesforce.com", "id": "https://login/id/00D/005ME"})],
                 ("PATCH", "sobjects/Case/500X"): [(401, [{"errorCode": "INVALID_SESSION_ID"}]), (204, {})]})
    c = salesforce.Connector({"SF_DOMAIN": "acme.my.salesforce.com", "SF_CLIENT_ID": "a", "SF_CLIENT_SECRET": "b"}, session=wire)
    c.set_state("500X", "needs_agent")
    patches = [s for s in wire.sent if s[0] == "PATCH"]
    assert len(patches) == 2 and patches[-1][2]["json"] == {"Status": "Escalated"}
    assert patches[-1][2]["headers"]["Authorization"] == "Bearer t2" and c.me == "005ME"


def test_a_refused_status_change_sends_nothing_twice_and_reads_nothing_back(desk):
    """Their system refusing the resolve once used to resend every note on every pass, and read them back as the person's."""
    fake = desk["system"]
    _pass(desk)  # switched on
    ext = fake.file("Priya Raman", "Can't reach the intranet")
    _pass(desk)
    number = tickets.listing()[0]["id"]
    refused = {"n": 0}

    def refuse(ext_id, status, note=""):
        refused["n"] += 1
        raise connectors.ConnectorError("403 Operation against file 'incident' was aborted by Business Rule")
    fake.set_state = refuse
    tickets.note(number, "Service desk", "desk", "Found it: the DNS server was unreachable. Fixed.")
    tickets._set(number, status=tickets.SOLVED)
    for _ in range(3):
        result = _pass(desk)
    assert "403" in result["fake"]["error"] and refused["n"] == 3  # tried again each pass, and said why
    bodies = [c["body"] for c in fake.comments_of[ext]]
    assert bodies.count("Found it: the DNS server was unreachable. Fixed.") == 1
    assert [n["body"] for n in tickets.notes_of(number) if n["role"] == "person"] == [tickets.get(number)["body"]]
    del fake.set_state  # they accept it now
    _pass(desk)
    assert fake.states[ext][0] == tickets.SOLVED


def test_servicenow_never_resolves_with_a_close_code_that_needs_another_record():
    wire = Wire({("GET", "table/sys_choice"): (200, {"result": [{"value": {"value": "Known error"}, "label": {"display_value": "Known error"}},
                                                                 {"value": {"value": "Duplicate"}, "label": {"display_value": "Duplicate"}},
                                                                 {"value": {"value": "Workaround provided"}, "label": {"display_value": "Resolved – Workaround Applied"}},
                                                                 {"value": {"value": "Resolved – Permanent Fix Applied"}, "label": {"display_value": "Resolved – Permanent Fix Applied"}}]}),
                 ("PATCH", "table/incident/abc"): (200, {"result": {}})})
    c = servicenow.Connector({"SN_INSTANCE": "dev1", "SN_USER": "u", "SN_PASSWORD": "p"}, session=wire)
    c.set_state("abc", "solved", "fixed it")
    assert wire.sent[-1][2]["json"]["close_code"] == "Resolved – Permanent Fix Applied"
    c.set_state("abc", "workaround")
    assert wire.sent[-1][2]["json"] == {"state": "2"}  # a workaround keeps it open
