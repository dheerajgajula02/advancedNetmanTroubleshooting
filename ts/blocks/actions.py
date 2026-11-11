"""Actions: the only ways a fix rule may change the network.

Each one runs while the fixer holds the network change lock, and says what it did.
A config action registers how to reverse itself, so a repair that fails its
verification is put back exactly as it was found.
"""

from __future__ import annotations

from ts import configs, lab
from ts.blocks import services
from ts.lab import Evidence, Result
from ts.registry import action

MAX_REPAIR_LINES = 40  # more than this is not a small correction, and a person should look first


def _converge_one(ctx, device: str) -> Result:
    good = configs.known_good(device)
    if good is None:
        return Result("failed", f"There is no known-good copy of {device}'s config to go back to.")
    source, want = good
    shown, before = configs.running(device)
    if before is None:
        return Result("failed", f"{device} did not return its running config.", evidence=[Evidence("device", shown)])
    lines = configs.repair_lines(lab.platform(device), configs.without_held(device, want), configs.without_held(device, before))
    if not lines:
        return Result("nothing", f"{device} already matches {source}, so there was nothing to change.", evidence=[Evidence("device", shown, output="(no differences)")])
    if len(lines) > MAX_REPAIR_LINES:
        return Result("failed", f"Getting {device} back to {source} would change {len(lines)} lines, which is too much to do without a person.",
                      evidence=[Evidence("device", shown, output="\n".join(lines[:60]))])
    worked, out = lab.configure(device, lines)
    ev = Evidence("device", f"{device}(config)# applying {len(lines)} lines from {source}", output="\n".join(lines) + ("\n\n" + lab._trim(out, 800) if out.strip() else ""))
    if not worked:
        return Result("failed", f"{device} would not take the change back to {source}.", evidence=[ev])

    def undo() -> str:
        _shown, after = configs.running(device)
        if after is None:
            return f"{device} did not answer, so the change could not be undone."
        reverse = configs.repair_lines(lab.platform(device), before, after)
        put_back, _out = lab.configure(device, reverse) if reverse else (True, "")
        return f"Undid the change on {device}." if put_back else f"Could not undo the change on {device}."

    ctx.undo.append(undo)
    ctx.changed.append(device)
    return Result("done", f"Put {device} back to {source}, changing {len(lines)} lines.", evidence=[ev])


@action("converge_config", ["device", "also?"], undo="auto", description="Puts a device's routing and interface config back to its known-good copy")
def converge_config(ctx, device, also=None):
    results = [_converge_one(ctx, device)] + ([_converge_one(ctx, also)] if also else [])
    outcomes = {r.outcome for r in results}
    outcome = "failed" if "failed" in outcomes else "done" if "done" in outcomes else "nothing"
    return Result(outcome, " ".join(r.saw for r in results), evidence=[e for r in results for e in r.evidence])


@action("recover_port", ["device", "interface"], undo="not_needed", description="Bounces a port to clear an error-disabled state")
def recover_port(ctx, device, interface):
    worked, out = lab.configure(device, [f"interface {interface}", "shutdown", "no shutdown"])
    ev = Evidence("device", f"{device}(config-if-{interface})# shutdown, no shutdown", output=lab._trim(out, 800))
    return Result("done" if worked else "failed", f"Bounced {interface} on {device}." if worked else f"{device} refused to bounce {interface}.", evidence=[ev])


@action("start_dhcp_service", [], undo="not_needed", platforms=("linux",), description="Starts dnsmasq on the DHCP server")
def start_dhcp_service(ctx):
    shown, out, rc = lab.run_change(services.DHCP_SERVER, ["dnsmasq"])
    ev = Evidence("device", shown, output=lab._trim(out, 800) or "(started)")
    return Result("done" if rc == 0 else "failed", "Started dnsmasq on the DHCP server." if rc == 0 else "dnsmasq would not start.", evidence=[ev])


@action("restore_nat", ["device"], undo="not_needed", platforms=("frr",), description="Puts the outbound NAT rule back on a border router")
def restore_nat(ctx, device):
    outside = services.NAT_SOURCE.get(device)
    if outside is None:
        return Result("failed", f"{device} is not a border router, so it has no NAT rule to restore.")
    shown, out, rc = lab.run_change(device, ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "10.0.0.0/8", "-o", "eth1", "-j", "SNAT", "--to-source", outside])
    ev = Evidence("device", shown, output=lab._trim(out, 800) or "(added)")
    return Result("done" if rc == 0 else "failed", f"Restored the NAT rule on {device}." if rc == 0 else f"{device} refused the NAT rule.", evidence=[ev])


@action("restore_border_routes", ["device"], undo="not_needed", platforms=("frr",),
        description="Puts back only the return routes a border router's startup script adds. It never re-runs the script, which would flush the firewall.")
def restore_border_routes(ctx, device):
    _script, wanted = services.boot_routes(device)
    if not wanted:
        return Result("failed", f"{device}'s startup script lists no return routes to restore.")
    table, absent = services.absent_routes(device, wanted)
    if absent is None:
        return Result("failed", f"{device} did not return its routes, so nothing was changed.", evidence=[table])
    evidence, refused = [], []
    for prefix, gateway, port in absent:
        shown, out, rc = lab.run_change(device, ["ip", "-6", "route", "replace", prefix, "via", gateway, "dev", port])
        evidence.append(Evidence("device", shown, output=lab._trim(out, 400) or "(added)"))
        if rc != 0:
            refused.append(prefix)
    if refused:
        return Result("failed", f"{device} refused these return routes: {', '.join(refused)}.", evidence=evidence)
    return Result("done", f"Put {len(absent)} return routes back on {device}: {', '.join(p for p, _g, _d in absent)}.", evidence=evidence)
