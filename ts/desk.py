"""The service desk: python -m ts desk --host <addr> --port 8771.

A separate site from the operations console, for the people who raise tickets and the
people who answer them. It shares the console's database. It only records tickets; the
watcher is what works them.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ts import aiconfig, brain, tickets

STATIC = Path(__file__).resolve().parent / "static"
FONT = re.compile(r"^/fonts/([a-z0-9-]+\.woff2)$")
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'",
}


def _scenarios():
    from drills import scenarios  # the staged side stays out of the troubleshooter's imports
    return scenarios


def _agents() -> list[dict]:
    """Every AI agent and which memory is its own. The people's are kept apart from the support side's."""
    from drills import office  # like the scenarios, this stays out of the troubleshooter's imports
    people = [{"id": p["id"], "name": p["name"], "about": f"{p['team']}, {p['where']}, on {p['host']}", "group": "computer", "uses": office.uses_of(p["id"]), "path": office.BRAINS} for p in tickets.PEOPLE]
    return [*people, *[{"id": role, "name": aiconfig.ROLE_WORDS[role][0], "about": aiconfig.ROLE_WORDS[role][1], "group": "support", "uses": [], "path": None} for role in ("support", "desk", "notes")]]


def _agent(agent_id: str) -> dict:
    found = next((a for a in _agents() if a["id"] == agent_id), None)
    if found is None:
        raise ValueError("There is no such agent.")
    return found


def _brain_view(agent_id: str, query: dict) -> dict:
    agent, asked = _agent(agent_id), (query.get("q") or [""])[0].strip()
    found = brain.recall(agent_id, asked, 20, agent["path"]) if asked else brain.listing(agent_id, (query.get("kind") or [""])[0], path=agent["path"])
    return {"agent": {k: v for k, v in agent.items() if k != "path"}, **brain.summary(agent_id, agent["path"]), "asked": asked, "memories": found, "stretches": brain.stretches(agent_id, agent["path"])}


def _status_of(number: int):
    found = tickets.get(number)
    return found["status"] if found else "closed"


def _tidy_loop() -> None:
    while True:
        try:
            _scenarios().finished(_status_of)
            from drills import campaign, office, people
            office.tick()
            people.tick()
            campaign.tick()
        except Exception as exc:  # the desk must stay up whatever a staged fault does
            print(f"scenario upkeep: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(10)


class Desk(BaseHTTPRequestHandler):
    server_version = "desk"

    def log_message(self, *_args):
        pass

    def _send(self, code: int, body: bytes, kind: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for key, value in HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _reply(self, code: int, value) -> None:
        self._send(code, json.dumps(value).encode(), "application/json")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        font = FONT.match(path)
        if path == "/":
            return self._send(200, (STATIC / "desk.html").read_bytes(), "text/html; charset=utf-8")
        if font and (STATIC / "fonts" / font.group(1)).exists():
            return self._send(200, (STATIC / "fonts" / font.group(1)).read_bytes(), "font/woff2", "max-age=86400")
        if path == "/api/desk":
            return self._reply(200, tickets.snapshot())
        if path == "/api/people":
            return self._reply(200, {"people": tickets.PEOPLE})
        if path == "/api/scenarios":
            return self._reply(200, _scenarios().public())
        if path == "/api/brains":
            return self._reply(200, {"agents": [{**{k: v for k, v in a.items() if k != "path"}, **brain.summary(a["id"], a["path"])} for a in _agents()]})
        one = re.fullmatch(r"/api/brains/([a-z0-9-]+)", path)
        if one:
            try:
                return self._reply(200, _brain_view(one.group(1), parse_qs(urlsplit(self.path).query)))
            except ValueError as exc:
                return self._reply(404, {"error": str(exc)})
        ticket = re.fullmatch(r"/api/(tickets|my)/(\d+)", path)
        if ticket:
            found = tickets.view(int(ticket.group(2)), ticket.group(1) == "my")
            return self._reply(200, found) if found else self._reply(404, {"error": "There is no such ticket."})
        self._reply(404, {"error": "Nothing here."})

    def do_POST(self):  # noqa: N802
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._reply(415, {"error": "This needs JSON."})
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self._reply(403, {"error": "Not from this page."})
        try:
            data = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
            self._reply(200, self._act(self.path.split("?", 1)[0], data))
        except (ValueError, KeyError) as exc:
            self._reply(400, {"error": str(exc).strip("'")})

    def _act(self, path: str, data: dict) -> dict:
        if path == "/api/tickets":
            return {"id": tickets.create(data["person"], data.get("body", ""))}
        staged = re.fullmatch(r"/api/scenarios/([a-z-]+)/stage", path)
        if staged:
            scenarios = _scenarios()
            chosen = next((s for s in scenarios.SCENARIOS if s["id"] == staged.group(1)), None)
            if chosen is None:
                raise ValueError("There is no such scenario.")
            scenarios.stage(chosen["id"])
            if data.get("they_notice"):  # the person finds out at their own computer, and writes in if they do
                from drills import people
                people.notice_soon(chosen["person"], scenarios.attach, scenarios.end)
                return {"noticing": True}
            number = tickets.create(chosen["person"], data.get("body") or chosen["ticket"])
            scenarios.attach(number)
            return {"id": number}
        if path == "/api/scenarios/end":
            return {"ended": _scenarios().end()}
        adding = re.fullmatch(r"/api/brains/([a-z0-9-]+)", path)
        if adding:
            agent, body = _agent(adding.group(1)), str(data.get("body") or "").strip()
            if not body:
                raise ValueError("Write something for them to remember.")
            brain.remember(agent["id"], "added", body, path=agent["path"])
            return {"ok": True}
        memory = re.fullmatch(r"/api/brains/([a-z0-9-]+)/(\d+)/(edit|forget)", path)
        if memory:
            agent = _agent(memory.group(1))
            done = brain.change(agent["id"], int(memory.group(2)), data.get("body", ""), agent["path"]) if memory.group(3) == "edit" else brain.forget(agent["id"], int(memory.group(2)), agent["path"])
            if not done:
                raise ValueError("That memory is no longer there.")
            return {"ok": True}
        action = re.fullmatch(r"/api/tickets/(\d+)/(reply|take|close|reopen)", path)
        if not action:
            raise ValueError("Nothing here.")
        number, verb = int(action.group(1)), action.group(2)
        if verb == "reply":
            tickets.reply(number, data.get("who") or "Support", data.get("role") or "agent", data.get("body", ""), bool(data.get("private")))
        elif verb == "take":
            tickets.take(number, data.get("who") or "Support")
        elif verb == "close":
            tickets.close(number, data.get("who") or "Support")
        else:
            tickets.reopen(number, data.get("who") or "The person", data.get("body", ""))
        return {"ok": True}


def serve(host: str, port: int) -> None:
    threading.Thread(target=_tidy_loop, daemon=True).start()
    print(f"service desk on http://{host}:{port}/", flush=True)
    ThreadingHTTPServer((host, port), Desk).serve_forever()
