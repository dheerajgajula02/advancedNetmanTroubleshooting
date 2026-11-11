"""Staged situations for the service desk: a real fault, and the ticket the person affected would write.

Like the drills, this is the only side that knows a fault was staged. The troubleshooter
never imports it and the marker file is not something it reads.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from drills import people
from drills.run import DRILLS, CablePull, Drill, _why_not_now, sh
from ts import capture


class HostRouteGone(Drill):
    """One person's computer has lost its way out; the network around it is fine."""
    def inject(self):
        sh("h5", "ip", "route", "del", "default")

    def restore(self):
        sh("h5", "ip", "route", "replace", "default", "via", "10.10.10.209", "dev", "eth1")


class WarehouseStrayRoute(Drill):
    """A route someone added by hand on a border and forgot. Replies for the warehouse go down the annex link and circle until they expire."""
    ROUTE = ("10.10.10.224/28", "via", "10.0.46.2", "dev", "eth4")

    def inject(self):
        sh("r4", "ip", "route", "add", *self.ROUTE)

    def restore(self):
        sh("r4", "ip", "route", "del", *self.ROUTE, check=False)


class KeepsFlapping(CablePull):
    """A cable or optic on its way out: the port keeps dropping for a few seconds and coming back, until someone replaces it."""
    node, port = "r4", "eth3"
    DOWN, UP = 4, 12
    SAVED = Path(__file__).resolve().parent.parent / "data" / "drills" / "flapping-port.json"

    def inject(self):
        self.snapshot()
        self.SAVED.write_text(json.dumps({"saved": self.saved, "routes": self.routes}))
        self.quit = threading.Event()
        self.flapper = threading.Thread(target=self._flap, daemon=True)
        self.flapper.start()

    def _flap(self):
        while not self.quit.is_set():
            sh(self.node, "ip", "link", "set", self.port, "down", check=False)
            if self.quit.wait(self.DOWN):
                break
            sh(self.node, "ip", "link", "set", self.port, "up", check=False)
            self.quit.wait(1)
            try:
                self.put_back()
            except RuntimeError as exc:  # the next round tries again, and restore has the last word
                print(f"flapping port: {exc}", flush=True)
            self.quit.wait(self.UP)

    def restore(self):
        if getattr(self, "quit", None):
            self.quit.set()
            self.flapper.join(timeout=30)
        if not getattr(self, "saved", None):  # the desk restarted since this began
            kept = json.loads(self.SAVED.read_text())
            self.saved, self.routes = kept["saved"], kept["routes"]
        super().restore()


CURRENT = Path(__file__).resolve().parent.parent / "data" / "drills" / "scenario-now.json"
LONGEST = 1800  # seconds before a staged fault is put back whatever has happened
_EXTRA = {"h5_route": HostRouteGone("h5_route", "h5 has no default route", "h5", "internet", {"escalate"}),
          "port_keeps_flapping": KeepsFlapping("port_keeps_flapping", "r4 eth3 keeps dropping and coming back", "h7", "ticket", {"escalate"}),
          "warehouse_stray_route": WarehouseStrayRoute("warehouse_stray_route", "a route left on r4 by hand sends replies for the warehouse down the annex link", "h6", "internet", {"escalate"})}
SCENARIOS = [
    {"id": "finance-offline", "person": "priya", "drill": "forwarding_off", "title": "The internet is down for Finance", "what": "A border router has stopped forwarding traffic. It still answers and every route looks right.",
     "ticket": "Hi, the internet stopped working on my computer a few minutes ago. Websites and email just spin and time out. I need to send the payroll files today, please help."},
    {"id": "engineering-offline", "person": "aiko", "drill": "forward_blocked_r4", "title": "Only one person is cut off", "what": "A firewall line on the other border router drops traffic for the internet. Only this person's path goes through it.",
     "ticket": "I can't reach anything on the internet from my workstation since just now, git pulls just hang. The person sitting next to me seems fine."},
    {"id": "sales-offline", "person": "marcus", "drill": "forward_blocked", "title": "Sales loses the internet", "what": "A firewall line left on a border router drops everything leaving for the internet. The router still reaches the internet itself and its routes look right.",
     "ticket": "The internet has just stopped on my computer. Nothing loads, the CRM just spins and I've got customer calls booked this afternoon. Could someone take a look?"},
    {"id": "sales-slow", "person": "marcus", "drill": "bad_interface", "title": "Everything is slow for Sales", "what": "A port on the border is losing 30% of packets while staying up and showing no errors.",
     "ticket": "Everything has been really slow for the last while. Websites half load and my video calls keep freezing. It is not completely down, it is just awful."},
    {"id": "support-no-address", "person": "diego", "drill": "dnsmasq_killed", "title": "A new laptop will not join", "what": "The DHCP service has died, so nothing new can get an address.",
     "ticket": "I plugged in a laptop for a new starter and it will not get on the network. It says no IP address was assigned. My own computer seems okay for now."},
    {"id": "annex-offline", "person": "lena", "drill": "h5_route", "title": "One computer in the annex", "what": "This one computer has lost its default route. The network around it is healthy.",
     "ticket": "I'm not getting internet at my desk in the annex. It was fine this morning and I haven't changed anything."},
    {"id": "warehouse-offline", "person": "sam", "drill": "warehouse_stray_route", "title": "The warehouse drops off the internet", "what": "A route left behind by hand on a border router sends the warehouse's replies down the wrong link. Everything still looks up, and the rest of the company is fine.",
     "ticket": "Nothing online is working in the warehouse office since a little while ago. The carrier booking site and the shipping label portal both just spin and time out. Ravi out on the loading dock says his PC is doing the same thing. We've got a late pickup tonight so I really need this back."},
    {"id": "dock-flapping", "person": "ravi", "drill": "port_keeps_flapping", "title": "The connection keeps cutting out", "what": "A port on a border router keeps dropping for a few seconds and coming back, as a failing cable does. Someone has to replace it. Until then, traffic can be steered around it.",
     "ticket": "The internet keeps cutting out on my PC at the loading dock. It works for a bit, drops for a few seconds, then comes back. It's been doing it for a while now and keeps kicking me off the shipping system."},
]


def _drill(name: str) -> Drill:
    return _EXTRA.get(name) or DRILLS[name]


def public() -> dict:
    return {"scenarios": SCENARIOS, "current": current(), "blocked": _why_not_now(), "last_noticed": people.last()}


def current() -> dict | None:
    return json.loads(CURRENT.read_text()) if CURRENT.exists() else None


def stage(scenario_id: str) -> dict:
    """Break it for real. Refuses while another is staged or while a job may be changing the network."""
    chosen = next((s for s in SCENARIOS if s["id"] == scenario_id), None)
    if chosen is None:
        raise ValueError("There is no such scenario.")
    if current():
        raise ValueError("Another scenario is still staged. Put the network back first.")
    why = _why_not_now()
    if why:
        raise ValueError(f"Not right now: {why}. Try again in a few minutes.")
    drill = _drill(chosen["drill"])
    drill.inject()
    injected = time.time()
    fault = capture.fault(drill.name, {"scenario": chosen["id"], "about": drill.about}, injected)
    marker = {"id": chosen["id"], "drill": drill.name, "at": injected, "fault": fault, "ticket": None}
    CURRENT.parent.mkdir(parents=True, exist_ok=True)
    CURRENT.write_text(json.dumps(marker))
    return marker


def attach(ticket: int) -> None:
    marker = current()
    if marker:
        CURRENT.write_text(json.dumps({**marker, "ticket": ticket}))
        chosen = next((s for s in SCENARIOS if s["id"] == marker["id"]), None)
        if chosen:
            people.take(ticket, chosen["person"])


def end() -> bool:
    """Put back what was broken. Safe to call when it has already been repaired."""
    marker = current()
    if not marker:
        return False
    _drill(marker["drill"]).restore()
    capture.fault(marker["drill"], {"scenario": marker["id"]}, marker["at"], time.time(), marker["fault"])
    CURRENT.unlink(missing_ok=True)
    return True


def finished(ticket_status) -> None:
    """Put the network back once the staged ticket is settled, or after too long."""
    marker = current()
    if marker and (time.time() - marker["at"] > LONGEST or (marker.get("ticket") and ticket_status(marker["ticket"]) not in ("new", "working", "confirming"))):
        end()
