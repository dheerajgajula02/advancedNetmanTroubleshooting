#!/usr/bin/env python
"""Simulated faults, run one at a time, each scored against what was really done.

This is the only place that knows a fault was staged. The troubleshooter does not
import it, and the answers are written where the troubleshooter never reads.
Every drill restores what it broke and then waits for the network to be healthy.

    python drills/run.py list
    python drills/run.py BATCH [BATCH ...]    a batch is one name, or names joined by commas that run together
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ts import capture, lab, review, store, sweep  # noqa: E402
from ts.sources import jenkins  # noqa: E402

RESULTS = ROOT / "data" / "drills"
PREFIX = "clab-lab1-"


def sh(device: str, *argv: str, check: bool = True) -> str:
    proc = subprocess.run(["docker", "exec", PREFIX + device, *argv], capture_output=True, text=True, timeout=60)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{device} {' '.join(argv)}: {proc.stdout}{proc.stderr}")
    return proc.stdout


def eos(device: str, *lines: str) -> str:
    return sh(device, "Cli", "-p", "15", "-c", "\n".join(["configure", *lines]))


def addresses(device: str, port: str) -> list[str]:
    data = json.loads(sh(device, "ip", "-j", "addr", "show", port))[0]
    return sorted(f"{a['local']}/{a['prefixlen']}" for a in data["addr_info"] if a.get("scope") == "global")


def restore_addresses(device: str, port: str, wanted: list[str]) -> None:
    """A Linux link-down can flush a port's IPv6 addresses, so put back any that went missing."""
    have = addresses(device, port)
    for address in wanted:
        if address not in have:
            sh(device, "ip", "addr", "add", address, "dev", port)


@dataclass
class Drill:
    name: str
    about: str
    device: str                # the run we expect to judge
    subject: str               # regular expression over the run's subject
    want: set                  # acceptable causes
    repair: str | None = None  # the repair status we expect, or None for none at all
    wait: int = 420
    pattern: str | None = None
    runs: int = 1              # how many matching runs to wait for
    ai: str | None = None      # how the AI should finish, or None if that is not being judged
    ai_tag: bool = False       # the AI should suggest a new tag

    def inject(self) -> None: raise NotImplementedError
    def restore(self) -> None: raise NotImplementedError


class Loss(Drill):
    def inject(self):
        sh("r3", "tc", "qdisc", "add", "dev", "eth3", "root", "netem", "loss", "30%")

    def restore(self):
        sh("r3", "tc", "qdisc", "del", "dev", "eth3", "root", check=False)


class Silent(Drill):
    def inject(self):
        for d in ("r3", "r4"):
            sh(d, "tc", "qdisc", "add", "dev", "eth2", "root", "netem", "loss", "100%")

    def restore(self):
        for d in ("r3", "r4"):
            sh(d, "tc", "qdisc", "del", "dev", "eth2", "root", check=False)


class CablePull(Drill):
    node, port = "r3", "eth3"

    def inject(self):
        self.snapshot()
        sh(self.node, "ip", "link", "set", self.port, "down")

    def snapshot(self):
        self.saved = addresses(self.node, self.port)
        shown = sh(self.node, "ip", "-6", "route", "show", "dev", self.port)
        self.routes = [line.split() for line in shown.splitlines() if line.strip() and "proto" not in line and "nhid" not in line]  # set by the deploy script, so nothing else brings them back

    def put_back(self):
        restore_addresses(self.node, self.port, self.saved)
        for route in self.routes:
            sh(self.node, "ip", "-6", "route", "replace", *route, "dev", self.port)

    def restore(self):
        sh(self.node, "ip", "link", "set", self.port, "up")
        time.sleep(2)
        self.put_back()


class Flap(CablePull):
    node, port = "r4", "eth3"

    def inject(self):
        self.snapshot()
        for n in range(3):
            sh(self.node, "ip", "link", "set", self.port, "down")
            time.sleep(4)
            sh(self.node, "ip", "link", "set", self.port, "up")
            time.sleep(2)
            self.put_back()
            if n < 2:
                time.sleep(45)


class HandShut(Drill):
    def inject(self):
        eos("s1", "interface Ethernet4", "shutdown")

    def restore(self):
        eos("s1", "interface Ethernet4", "no shutdown")


class Tamper(Drill):
    def inject(self):
        eos("s3", "interface Ethernet3", "ip ospf cost 500")

    def restore(self):
        eos("s3", "interface Ethernet3", "no ip ospf cost 500")


class VrrpTamper(Drill):
    def inject(self):
        shown = sh("s1", "Cli", "-p", "15", "-c", "show running-config interfaces Vlan10")
        self.line = re.search(r"^\s*(vrrp \d+ priority-level \d+)", shown, re.M).group(1)
        self.vrid = self.line.split()[1]
        eos("s1", "interface Vlan10", f"vrrp {self.vrid} priority-level 5")

    def restore(self):
        eos("s1", "interface Vlan10", self.line)


class NatGone(Drill):
    ARGS = ("POSTROUTING", "-s", "10.0.0.0/8", "-o", "eth1", "-j", "SNAT", "--to-source", "50.50.50.1")

    def inject(self):
        sh("r3", "iptables", "-t", "nat", "-D", *self.ARGS)

    def restore(self):
        if "50.50.50.1" not in sh("r3", "iptables", "-t", "nat", "-S", "POSTROUTING"):
            sh("r3", "iptables", "-t", "nat", "-A", *self.ARGS)


class ReturnRoutesGone(Drill):
    PREFIXES = ("2001:db8:10::/64", "2001:db8:20::/64", "2001:db8:30::/64")

    def inject(self):
        for prefix in self.PREFIXES:
            sh("r3", "ip", "-6", "route", "del", prefix)

    def restore(self):
        for prefix in self.PREFIXES:
            sh("r3", "ip", "-6", "route", "replace", prefix, "via", "2001:db8:100:33::1", "dev", "eth3")


class BorderPortShut(CablePull):
    """Someone shuts the port on the router, which also wipes the routes only the startup script adds."""

    def inject(self):
        self.snapshot()
        sh(self.node, "vtysh", "-c", "configure terminal", "-c", f"interface {self.port}", "-c", "shutdown")

    def restore(self):
        sh(self.node, "vtysh", "-c", "configure terminal", "-c", f"interface {self.port}", "-c", "no shutdown")
        time.sleep(2)
        self.put_back()


class DnsmasqKilled(Drill):
    def inject(self):
        sh("dhcp-Server", "pkill", "-x", "dnsmasq")

    def restore(self):
        if not sh("dhcp-Server", "pgrep", "-x", "dnsmasq", check=False).strip():
            sh("dhcp-Server", "dnsmasq")


class ForwardBlocked(Drill):
    """Nothing the rules look at notices this: the borders reach the internet, NAT and routes are fine, but nothing gets through them."""
    LINE = ("FORWARD", "-o", "eth1", "-j", "DROP")
    node = "r3"

    def inject(self):
        sh(self.node, "iptables", "-I", "FORWARD", "1", "-o", "eth1", "-j", "DROP")

    def restore(self):
        while "-A FORWARD -o eth1 -j DROP" in sh(self.node, "iptables", "-S", "FORWARD"):
            sh(self.node, "iptables", "-D", *self.LINE)


class ForwardBlockedOtherSide(ForwardBlocked):
    node = "r4"


class ForwardingOff(Drill):
    """The kernel stops routing. Routes, NAT, filters and the daemons all look right, the border still reaches the internet itself, and no tag describes a kernel setting."""
    node = "r3"

    def inject(self):
        sh(self.node, "sysctl", "-w", "net.ipv4.ip_forward=0")

    def restore(self):
        sh(self.node, "sysctl", "-w", "net.ipv4.ip_forward=1")


DRILLS = {d.name: d for d in [
    Loss("bad_interface", "30% packet loss on one direction of r3 eth3, with the port still up", "r3", r"link eth3", {"link.loss_no_errors", "link.bad_interface"}),
    CablePull("cable_pull", "the link from r3 eth3 to s3 goes dark", "s3", r"Ethernet1", {"link.physical_fault"}),
    Silent("silent_failure", "every packet between r3 and r4 is dropped, with both ports still up", "r3", r"link eth2", {"link.loss_no_errors", "link.silent_fault"}, wait=480),
    Flap("repeated_flap", "the link from r4 to s4 flaps three times", "s4", r"Ethernet1", set(), pattern="bad_hardware", runs=3, wait=540),
    HandShut("hand_shut", "someone shuts s1 Ethernet4 by hand", "s1", r"Ethernet4", {"interface.admin_shut"}, repair="fixed"),
    Tamper("config_tamper", "someone changes the OSPF cost on s3 Ethernet3 by hand", "s3", r"config", {"config.changed_by_unknown", "config.changed_by_person", "config.drift"}, repair="fixed", wait=660),
    VrrpTamper("vrrp_tamper", "someone lowers the VRRP priority on s1 Vlan10 by hand", "s1|s2", r"Vlan10|vrrp|VRRP", {"config.drift"}, repair="fixed", wait=480),
    NatGone("nat_removed", "the outbound NAT rule disappears from r3", "r3", r"NAT", {"border.nat_missing"}, repair="fixed", wait=420),
    ReturnRoutesGone("routes_removed", "the IPv6 return routes disappear from r3", "r3", r"return routes", {"border.routes_missing"}, repair="fixed", wait=420),
    BorderPortShut("border_port_shut", "someone shuts r3 eth3 by hand, which also wipes its return routes", "r3|h4", r"return routes|internet", {"border.routes_missing"}, repair="fixed", wait=780),
    DnsmasqKilled("dnsmasq_killed", "the DHCP service dies", "dhcp-Server", r"DHCP", {"dhcp.daemon_down"}, repair="fixed", wait=420),
    ForwardBlocked("forward_blocked", "a firewall line on r3 drops everything leaving for the internet, which no rule looks at", "h1|h2", r"internet", {"escalate"}, ai="fixed", wait=840),
    ForwardBlockedOtherSide("forward_blocked_r4", "the same firewall line, on r4, which only h3 goes through", "h3", r"internet", {"escalate"}, ai="fixed", wait=840),
    ForwardingOff("forwarding_off", "IPv4 forwarding is switched off in r3's kernel, which no rule looks at and no tag describes", "h1|h2", r"internet", {"escalate"}, ai_tag=True, wait=840),
]}


def say(text: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)


def _why_not_now() -> str | None:
    now = time.localtime()
    if now.tm_hour == 3 or (now.tm_hour == 4 and now.tm_min < 30):
        return "the nightly job window"
    if (now.tm_hour == 16 and now.tm_min >= 55) or (now.tm_hour == 17 and now.tm_min < 15):
        return "the password rotation"
    if 44 <= now.tm_min <= 53:
        return "the hourly health monitor is about to run"
    if lab.change_running():
        return "a job holds the change lock"
    jobs = jenkins.running_change_jobs()
    if jobs:
        return f"a job is running: {jobs[0]}"
    return None


def _unhealthy() -> list[str]:
    return [f.key for f in sweep.look(lambda _m: None) if not f.ok]


def wait_for_quiet() -> bool:
    for _ in range(90):
        why = _why_not_now() or (lambda bad: f"not healthy: {bad[:4]}" if bad else None)(_unhealthy())
        if why is None:
            return True
        say(f"waiting: {why}")
        time.sleep(30)
    return False


def matching_runs(drill: Drill, since: float) -> list[dict]:
    out = []
    for row in store.summaries():
        if row["time"] >= since - 5 and re.fullmatch(drill.device, row["device"]) and re.search(drill.subject, row.get("subject") or ""):
            out.append(store.load(row["id"]))
    return sorted(out, key=lambda r: r["event"]["time"])


def _score(drill: Drill, outcome: dict, found: list[dict], started: float) -> None:
    if found:
        last = next((r for r in reversed(found) if r.get("repair") or (r.get("agent") or {}).get("status") == "done"), found[-1])  # when several runs saw one fault, judge the one that acted
        worked = last.get("agent") or {}
        outcome.update(ai_status=worked.get("status"), ai_outcome=worked.get("outcome"), ai_steps=len(worked.get("calls") or []), ai_changes=[c.get("status") for c in worked.get("changes") or []],
                       ai_proposals=worked.get("proposals") or [], ai_root_cause=worked.get("root_cause"), ai_error=worked.get("error"))
        outcome.update(run_id=last["id"], cause=last["result"].get("label") or last["result"]["kind"], text=last["result"].get("text"),
                       repair=(last.get("repair") or {}).get("status"), repair_why=(last.get("repair") or {}).get("why"), pattern=(last.get("pattern") or {}).get("verdict"),
                       emailed=(last.get("email") or {}).get("sent"), email_subject=(last.get("email") or {}).get("subject"),
                       assessment=(last.get("analysis") or {}).get("assessment"), seconds_to_answer=round(last["finished"] - started) if last.get("finished") else None)
    right_cause = (not drill.want) or (bool(found) and outcome.get("cause") in drill.want)
    right_repair = bool(found) and outcome.get("repair") == drill.repair
    right_pattern = drill.pattern is None or outcome.get("pattern") == drill.pattern
    right_ai = drill.ai is None or outcome.get("ai_outcome") == drill.ai
    suggested = [review.get(number) for number in outcome.get("ai_proposals") or []]
    outcome["ai_new_tags"] = [p["body"]["tag"] for p in suggested if p and p["kind"] == "tag"]
    right_tag = not drill.ai_tag or bool(outcome["ai_new_tags"])
    outcome["score"] = "pass" if found and right_cause and right_repair and right_pattern and right_ai and right_tag else "FAIL"


def _record_fault(drill: Drill, earlier: tuple | None = None) -> tuple:
    """What was really broken, for the training data. The AI cannot read it."""
    spec = {"type": type(drill).__name__, **{k: getattr(drill, k) for k in dir(drill) if not k.startswith("_") and isinstance(getattr(drill, k), (str, int, float))}}
    if earlier:
        capture.fault(drill.name, spec, earlier[1], time.time(), earlier[0])
        return earlier
    injected = time.time()
    return capture.fault(drill.name, spec, injected), injected


def run_batch(drills: list[Drill]) -> list[dict]:
    say("=== batch: " + ", ".join(d.name for d in drills))
    if not wait_for_quiet():
        return [{"drill": d.name, "ran": False, "why": "the network never became quiet enough"} for d in drills]
    started = time.time()
    open_drills, outcomes, done, truth = [], {}, set(), {}
    for d in drills:
        outcomes[d.name] = {"drill": d.name, "about": d.about, "ran": True, "started": started, "want": sorted(d.want), "want_repair": d.repair}
    try:
        for d in drills:
            d.inject()
            open_drills.append(d)
            truth[d.name] = _record_fault(d)
            say(f"injected {d.name}")
        while len(done) < len(drills):
            time.sleep(10)
            jobs = jenkins.running_change_jobs()  # the troubleshooter's own repair also takes the lock, so go by the jobs
            for d in drills:
                if d.name in done:
                    continue
                found = matching_runs(d, started)
                settled = len(found) >= d.runs and all(_settled(r) for r in found)
                if settled or jobs or time.time() - started >= d.wait:
                    if jobs and not settled:
                        outcomes[d.name]["interrupted"] = f"a job started during the drill: {jobs[0]}"
                    _score(d, outcomes[d.name], found, started)
                    d.restore()
                    _record_fault(d, truth.get(d.name))
                    open_drills.remove(d)
                    done.add(d.name)
                    o = outcomes[d.name]
                    say(f"{d.name}: {o['score']}  cause={o.get('cause')} repair={o.get('repair')} pattern={o.get('pattern')} emailed={o.get('emailed')} after={o.get('seconds_to_answer')}s ai={o.get('ai_status')}/{o.get('ai_outcome')} steps={o.get('ai_steps')} changes={o.get('ai_changes')} suggestions={o.get('ai_proposals')} {o.get('interrupted', '')}")
    finally:
        for d in open_drills:
            d.restore()
            _record_fault(d, truth.get(d.name))
            say(f"restored {d.name} early")
    say("waiting for the network to be healthy again")
    healthy, bad = False, []
    for _ in range(30):
        bad = _unhealthy()
        if not bad:
            healthy = True
            break
        time.sleep(10)
    say(f"healthy again: {healthy} {bad[:8]}")
    RESULTS.mkdir(parents=True, exist_ok=True)
    for o in outcomes.values():
        o["healthy_after"] = healthy
        (RESULTS / f"{o['drill']}-{time.strftime('%Y%m%dT%H%M%S')}.json").write_text(json.dumps(o, indent=1))
    return list(outcomes.values())


def _settled(record: dict) -> bool:
    return record["result"]["kind"] in ("cause", "escalate", "stop") and (record.get("agent") or {}).get("status") not in ("queued", "working")


if __name__ == "__main__":
    names = sys.argv[1:]
    if names == ["list"]:
        for d in DRILLS.values():
            print(f"{d.name:16} {d.about}")
        sys.exit(0)
    results = [r for batch in names for r in run_batch([DRILLS[n] for n in batch.split(",")])]
    print(json.dumps([{k: r.get(k) for k in ("drill", "score", "cause", "repair", "pattern", "emailed", "healthy_after", "interrupted", "ai_status", "ai_outcome", "ai_steps", "ai_changes", "ai_proposals", "ai_root_cause", "ai_error")} for r in results], indent=1))
