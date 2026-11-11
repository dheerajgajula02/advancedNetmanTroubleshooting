"""Blocks that let a written rule cover what no purpose-built check does yet.

A new kind of fault should not have to wait for new code before it can become a
predictable step, so a rule can run one read-only command and look for some text
in what comes back, and a fix can make a small, checked, reversible change.
"""

from __future__ import annotations

from ts import configs, lab, safety
from ts.lab import Evidence, Result
from ts.registry import action, check

ALL_DEVICES = ("eos", "frr", "linux")


@check("output_has", ["device", "command", "text"], ["found", "absent"], platforms=ALL_DEVICES,
       description="Runs one read-only command on a device and says whether the text appears in what comes back")
def output_has(ctx, device, command, text):
    shown, out, rc = safety.read(device, command)
    ev = Evidence("device", shown, output=out)
    if rc != 0:
        return Result("error", f"{device} could not run that: {lab._trim(out, 200)}", evidence=[ev])
    return Result("found" if text in out else "absent", f"{text!r} {'appears' if text in out else 'does not appear'} in the output of {command!r} on {device}.", evidence=[ev])


@action("config_lines", ["device", "lines[]"], undo="auto", description="Applies a few config lines to one device, and can put them back exactly")
def config_lines(ctx, device, lines):
    problem = safety.config_problem(device, lines)
    if problem:
        return Result("failed", problem)
    shown, before = configs.running(device)
    if before is None:
        return Result("failed", f"{device} did not return its running config, so nothing was changed.", evidence=[Evidence("device", shown)])
    worked, out = lab.configure(device, lines)
    ev = Evidence("device", f"{device}(config)# applying {len(lines)} lines", output="\n".join(lines) + ("\n\n" + lab._trim(out, 800) if out.strip() else ""))
    if not worked:
        return Result("failed", f"{device} would not take the change, so nothing was applied.", evidence=[ev])

    def undo() -> str:
        _shown, after = configs.running(device)
        if after is None:
            return f"{device} did not answer, so the change could not be undone."
        reverse = configs.repair_lines(lab.platform(device), before, after)
        put_back, _out = lab.configure(device, reverse) if reverse else (True, "")
        return f"Undid the change on {device}, {len(reverse)} lines." if put_back else f"Could not undo the change on {device}."

    ctx.undo.append(undo)
    ctx.changed.append(device)
    return Result("done", f"Applied {len(lines)} lines on {device}.", evidence=[ev])


@action("linux_change", ["device", "argv[]", "undo_argv[]"], undo="auto", platforms=("frr", "linux"),
        description="Runs one allowed Linux-level change on a router or host, and its stated undo if the change has to come back out")
def linux_change(ctx, device, argv, undo_argv):
    problem = safety.command_problem(device, argv) or safety.command_problem(device, undo_argv)
    if problem:
        return Result("failed", problem)
    shown, out, rc = lab.run_change(device, argv)
    ev = Evidence("device", shown, output=lab._trim(out, 800) or "(done)")
    if rc != 0:
        return Result("failed", f"{device} refused: {lab._trim(out, 200)}", evidence=[ev])

    def undo() -> str:
        _shown, undo_out, undo_rc = lab.run_change(device, undo_argv)
        return f"Undid it on {device}: {' '.join(undo_argv)}" if undo_rc == 0 else f"Could not undo it on {device}: {lab._trim(undo_out, 200)}"

    ctx.undo.append(undo)
    return Result("done", f"Ran {' '.join(argv)} on {device}.", evidence=[ev])
