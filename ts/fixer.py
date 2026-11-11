"""After an investigation names a cause: put things back, carefully, and prove it worked.

A fix rule says which cause it answers, what must be true first, what to do, and
how to tell it worked. The fixer takes the network change lock so it never
collides with a job, refuses to fight a job or keep fighting a person, and
undoes its own change if the result does not verify.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from ts import configs, engine, firstlook, history, lab, scrub
from ts.sources import jenkins
from ts.vocab import DEVICES, FORBIDDEN_DEVICES

OFF_SWITCH = Path(os.environ.get("TS_CONVERGE_OFF", Path(__file__).resolve().parent.parent / "data" / "converge.off"))
MAX_FIXES_PER_HOUR = 3
VERIFY_FOR = 40    # seconds to wait for a repair to take effect
VERIFY_EVERY = 5
SETTLE = 5         # keep holding the lock this long, so the traps from our own change arrive while it is held


@dataclass
class FixContext(engine.Context):
    undo: list = field(default_factory=list)
    changed: list = field(default_factory=list)


def find_rule(rules, record: dict, values: dict):
    device = values.get("device", "")
    role, platform = DEVICES.get(device, ("", ""))
    facts = {"cause": record["result"]["label"], "device": device, "role": role, "platform": platform}
    for rule in sorted((r for r in rules if r.table == "fix" and r.mode in ("active", "shadow")), key=lambda r: -r.priority):
        if all(any(fnmatchcase(str(facts.get(k, "")), p) for p in (v if isinstance(v, list) else [v])) for k, v in rule.match.items()):
            return rule
    return None


def _run_call(call, ctx, values: dict) -> tuple[dict, object]:
    args = engine.fill_args(call.args, values)
    try:
        res = call.block.func(ctx, **args)
    except Exception as exc:  # a failed step is recorded, and treated as not having worked
        res = lab.Result("error", f"It failed: {type(exc).__name__}: {exc}")
    return args, res


def _step(kind: str, ask: str, call, args: dict, res) -> dict:
    return {"kind": kind, "ask": ask, "check": call.render(), "ran_with": args, "outcome": res.outcome, "saw": res.saw,
            "evidence": [e.as_dict() for e in res.evidence], "started": time.time()}


def _conditions_hold(conditions, ctx, values: dict, steps: list, kind: str, ask: str) -> tuple[bool, str]:
    for cond in conditions:
        args, res = _run_call(cond.check, ctx, values)
        steps.append(_step(kind, ask, cond.check, args, res))
        if res.outcome not in cond.expect:
            return False, res.saw
    return True, ""


def _guard(device_names: list[str], label: str) -> str | None:
    """Why this must not run now, or None if it may."""
    if OFF_SWITCH.exists():
        return f"Automatic repair is switched off ({OFF_SWITCH.name} exists)."
    if FORBIDDEN_DEVICES & set(device_names):
        return "That is the ISP's router, which is never touched."
    done = max((history.fixes_lately(d, label) for d in device_names), default=0)
    if done >= MAX_FIXES_PER_HOUR:
        return f"I have already put this back {done} times in the last hour and it keeps coming back, so I stopped rather than fight whoever or whatever is doing it."
    return None


def _job_in_the_way() -> str | None:
    """A job is changing the network, so whatever just happened is probably its doing."""
    jobs = jenkins.running_change_jobs()
    if jobs:
        return f"A job that changes the network is running ({', '.join(jobs[:3])}), so I left it alone."
    deploys = jenkins.recent_deploys()
    if deploys:
        return f"A deployment just finished ({', '.join(deploys[:3])}), and what it changed may be intended."
    return None


def _left_in_place(rule, values: dict) -> str:
    """What a config repair would overwrite that is being kept on purpose. Going back to known-good already leaves those alone."""
    if rule.action.do.name != "config_lines":
        return ""
    args = engine.fill_args(rule.action.do.args, values)
    lines = args.get("lines") or []
    return "; ".join(" ".join(item["lines"]) for item in configs.touching(str(args.get("device", "")), lines if isinstance(lines, list) else [str(lines)]))


def attempt(record: dict, rules, log=print) -> dict | None:
    """Try to repair what this investigation found. None means no fix rule covers the cause."""
    result = record["result"]
    if result["kind"] != "cause":
        return None
    values = dict(record.get("values") or {})
    values["alarm_device"] = values.get("device", "")  # a cause can move {device} to where the fault really is
    values.update({k: engine.fill(str(v), values) for k, v in (result.get("params") or {}).items()})
    rule = find_rule(rules, record, values)
    if rule is None:
        return None
    ctx = FixContext(event_time=float(record["event"]["time"]), event=record["event"])
    steps: list[dict] = []
    repair = {"rule": rule.id, "mode": rule.mode, "risk": rule.risk, "title": rule.title, "started": time.time(), "steps": steps}

    def finish(status: str, why: str = "", expected: bool = False) -> dict:
        repair.update(status=status, why=why, expected=expected, finished=time.time())
        return scrub.deep(repair)

    named = [engine.fill(v, values) for k, v in rule.action.do.args.items() if k in ("device", "also")]
    why = _guard(named or [values.get("device", "")], result["label"])
    if why:
        return finish("held", why)
    why = _job_in_the_way()
    if why:
        return finish("held", why, expected=True)
    held, saw = _conditions_hold(rule.require, ctx, values, steps, "require", "Is it safe to change this now?")
    if not held:
        return finish("held", saw)
    if rule.mode == "shadow":
        return finish("would_fix", f"The fix \"{rule.title}\" is in observe-only mode, so it did not run.")
    earlier = _left_in_place(rule, values)
    if earlier:
        repair["in_the_way"] = True
        return finish("held", f"This repair would alter something earlier work left in place on purpose: {earlier}. That needs weighing up, which a rule cannot do.")
    look = firstlook.take(record["event"], run_id=record.get("id", ""))
    record["first_look"] = look
    if look["working"]["verdict"] == "no":
        return finish("held", "There is no record of this ever working, so there is nothing to put back. A person should decide what it is meant to do.")
    try:
        with lab.change_lock():
            ask = rule.title
            args, res = _run_call(rule.action.do, ctx, values)
            steps.append(_step("do", ask, rule.action.do, args, res))
            if res.outcome == "nothing":
                return finish("already", res.saw)
            if res.outcome != "done":
                _undo(ctx, steps, log)
                return finish("failed", res.saw)
            deadline = time.time() + VERIFY_FOR
            while True:
                attempt_steps: list[dict] = []
                ok, saw = _conditions_hold(rule.verify, ctx, values, attempt_steps, "verify", "Did it work?")
                if ok or time.time() >= deadline:
                    steps.extend(attempt_steps)
                    break
                time.sleep(VERIFY_EVERY)
            if not ok:
                _undo(ctx, steps, log)
                return finish("failed", f"It did not take effect: {saw}")
            for device in ctx.changed:  # what is running now is the new starting point for spotting the next change
                _shown, lines = configs.running(device)
                if lines is not None:
                    configs.save_baseline(device, lines)
            time.sleep(SETTLE)
            return finish("fixed", res.saw)
    except lab.LockBusy:
        return finish("held", "Another job held the network change lock, so I left it alone.", expected=True)


def _undo(ctx: FixContext, steps: list, log) -> None:
    for undo in reversed(ctx.undo):
        try:
            saw = undo()
        except Exception as exc:  # keep going, the other devices still need undoing
            saw = f"Undo failed: {type(exc).__name__}: {exc}"
            log(saw)
        steps.append({"kind": "undo", "ask": "Undo the change", "check": "", "ran_with": {}, "outcome": "done", "saw": saw, "evidence": [], "started": time.time()})
