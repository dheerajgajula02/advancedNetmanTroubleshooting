"""Everything the AI can do, as typed tools: one per check, one per action, and a few for reading, changing and suggesting.

Because each check is its own tool with a fixed set of answers, what the AI did while
working a problem can be written down as steps of a rule and run again without it.

Every call is recorded on the run and in the audit table. A change is one transaction:
prove the problem is there, take the lock, note the health of the whole network, apply,
then keep it only if it did some good and broke nothing. Otherwise it comes back out.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ts import brain, cards, configs, engine, fixer, history, jenkins_index, lab, memory, pipelines, rag, review, rulebook, safety, scrub, store, sweep, tags, tickets
from ts.lint import errors
from ts.registry import ACTIONS, CHECKS, Block, load_blocks
from ts.ruleset import DEFAULT_RULES_DIR, load_ruleset
from ts.schema import PIPELINE, Outcome, RuleLoadError
from ts.vocab import DEVICES, FORBIDDEN_DEVICES

load_blocks()

AI_CHANGES_OFF = Path(os.environ.get("TS_AI_CHANGES_OFF", Path(__file__).resolve().parent.parent / "data" / "ai-changes.off"))
MAX_READS = 30             # looking is limited on its own, so it can never use up the chance to act on what it found
MAX_READS_ON_A_TICKET = 50 # nothing has been checked before a ticket reaches the agent, and what comes and goes takes longer to pin down
MAX_ACTS = 12              # changes and suggestions, including ones that get refused
MAX_CHANGES = 3            # per run
MAX_CHANGES_PER_HOUR = 6
VERIFY_FOR = 60            # seconds a change gets to show it worked
VERIFY_EVERY = 5
SETTLE = 5
RESTORES = ("action_converge_config", "retire_earlier_change")
SECOND_LOOK = 20           # a flapping port comes and goes, so for a stop-gap only what is still wrong this much later counts as broken by it
MAX_ASKS = 6               # questions to the person, per run
LLM_OUTPUT = 2500          # how much of a command's output goes back to the model
RULE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,55}$")
PIPELINE_NAME = re.compile(PIPELINE)


def health() -> set[str]:
    """Everything the sweep currently finds wrong, across the whole network."""
    return {finding.key for finding in sweep.look(lambda _m: None) if not finding.ok}


@dataclass
class Session:
    record: dict
    rules: list
    root: Path = DEFAULT_RULES_DIR
    model: str = ""
    calls: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    observed: dict = field(default_factory=dict)  # (check, args) -> the first thing that check said, before anything was changed
    shown: dict = field(default_factory=dict)     # (device, command) -> the first output
    result: dict | None = None
    waited: float = 0                             # seconds spent waiting for the person, which do not count against the time limit

    @property
    def run_id(self) -> str:
        return self.record["id"]

    @property
    def event(self) -> dict:
        return self.record["event"]


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    run: Callable[[Session, dict], dict]
    kind: str = "read"  # read | change | propose | finish


def _object(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


STR, STRS = {"type": "string"}, {"type": "array", "items": {"type": "string"}}
_VERIFY = {"type": "object", "description": "A check that fails now and should pass once this has worked.",
           "properties": {"check": STR, "args": {"type": "object"}, "expect": {**STRS, "description": "The answers that mean it worked"}}, "required": ["check", "args", "expect"]}


def _key(check: str, args: dict) -> tuple[str, str]:
    return check, json.dumps(args, sort_keys=True)


def _block_schema(block: Block, extra: dict | None = None, required: tuple = ()) -> dict:
    props = {p.name: (STRS if p.many else STR) for p in block.params}
    return _object({**props, **(extra or {})}, [*block.required, *required])


def _result(res: lab.Result) -> dict:
    output = "\n".join(e.output for e in res.evidence if e.output)
    return {"outcome": res.outcome, "saw": res.saw, "values": {k: str(v) for k, v in res.values.items()}, "output": output, "ran": [e.ran for e in res.evidence]}


def _problem_with_device(args: dict) -> str | None:
    for key in ("device", "peer_device"):
        if args.get(key) in FORBIDDEN_DEVICES:
            return f"{args[key]} is the ISP's router, which is out of scope."
        if key in args and args[key] not in DEVICES:
            return f"There is no device called {args[key]!r}."
    return None


def _run_block(session: Session, block: Block, args: dict) -> lab.Result:
    problem = _problem_with_device(args)
    if problem:
        return lab.Result("error", problem)
    device = args.get("device")
    if block.device_bound and device and DEVICES[device][1] not in block.platforms:
        return lab.Result("unsupported", f"{block.name} does not work on {device}.")
    context = engine.Context(event_time=float(session.event["time"]), event=session.event)
    try:
        return block.func(context, **args)
    except Exception as exc:  # a failed check is an answer too
        return lab.Result("error", f"The check failed: {type(exc).__name__}: {exc}")


def _check_tool(block: Block) -> Tool:
    def run(session: Session, args: dict) -> dict:
        res = _run_block(session, block, args)
        session.observed.setdefault(_key(block.name, args), {"outcome": res.outcome, "values": {k: str(v) for k, v in res.values.items()}})
        return _result(res)

    gives = f" Also gives: {', '.join(block.produces)}." if block.produces else ""
    return Tool(f"check_{block.name}", f"{block.description}. Answers one of: {', '.join(block.all_outcomes)}.{gives} Read-only.", _block_schema(block), run)


# --- reading --------------------------------------------------------------

def _show(session: Session, args: dict) -> dict:
    device, command = args["device"], args["command"]
    try:
        shown, out, rc = safety.read(device, command)
    except ValueError as exc:
        return {"outcome": "refused", "saw": str(exc)}
    session.shown.setdefault((device, command.strip()), out)
    return {"outcome": "ok" if rc == 0 else "error", "saw": f"Ran on {device}.", "output": out, "ran": [shown]}


def _search_memory(session: Session, args: dict) -> dict:
    found = memory.search(args["text"], 8)
    return {"outcome": "ok", "saw": f"{len(found)} records.", "output": "\n".join(f"{time.strftime('%m-%d %H:%M', time.localtime(m['time']))} [{m['source']}] {m['title']}" + (f" | {m['text']}" if m.get("text") else "") + (f" (seen {m['times_seen']} times)" if m["times_seen"] > 1 else "") for m in found)}


def _search_jenkins(session: Session, args: dict) -> dict:
    text, job, result = args.get("text") or "", args.get("job") or "", args.get("result") or ""
    if not (text or job or result):
        rows = jenkins_index.jobs()
        return {"outcome": "ok", "saw": f"{len(rows)} jobs.", "output": "\n".join(
            f"{r['job']}: latest #{r['last']} {r['result'] or ''} {time.strftime('%m-%d %H:%M', time.localtime(r['start'])) if r['start'] else ''}" + (" (runs many times a day)" if r["frequent"] else "") for r in rows)}
    found = jenkins_index.search(text, job, result, 12)
    if not found:
        return {"outcome": "ok", "saw": "No build matches that.", "output": "No Jenkins build matches. Try other words, a shorter job name, or no filters to list the jobs."}
    lines = [f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(b['start']))} {b['job']} #{b['number']} {b['result']}, {b['seconds']:.0f} s" + (f", {b['started_by']}" if b["started_by"] else "")
             + f"\n  {' '.join(b['text'].split())[:400]}" for b in found]
    return {"outcome": "ok", "saw": f"{len(found)} builds.", "output": "\n".join(lines)}


def _read_build(session: Session, args: dict) -> dict:
    try:
        got = jenkins_index.read(str(args.get("job") or ""), int(args.get("number")), int(args.get("part") or 1))
    except (TypeError, ValueError):
        got = None
    if not got:
        return {"outcome": "invalid", "saw": "There is no such build. search_jenkins lists the jobs and their builds."}
    return {"outcome": "ok", "saw": f"{got['job']} #{got['number']}, part {got['part']} of {got['parts']}.",
            "output": f"{got['summary']}\n{got['url']}\nPart {got['part']} of {got['parts']} of what it printed" + (", ask for the next part to read on" if got["part"] < got["parts"] else "") + f":\n\n{got['console']}"}


def _search_runs(session: Session, args: dict) -> dict:
    hits, _by_meaning = rag.find(args["text"], rag.names_in(args["text"]), exclude={session.run_id}, k=10)
    hits = [hit for hit in hits if hit["close"]]
    if not hits:
        return {"outcome": "ok", "saw": "Nothing in past runs matches that.", "output": "No past run has this. Try other words, or treat it as new."}
    notes = cards.notes_for([hit["run_id"] for hit in hits])
    lines = []
    for hit in hits:
        lines.append(f"{hit['run_id']}  cause {hit['key']}  ended {hit['ended'] or 'unknown'}" + ("  FIXED" if hit["fixed"] else "") + (f"  (+{len(hit['like'])} more like it: {', '.join(hit['like'][:3])})" if hit["like"] else "") + f"\n  tags: {','.join(history.tags_of(hit['run_id'])) or '-'}\n  note: {notes.get(hit['run_id']) or 'no note yet; open_run reads it'}")
        lines += [f"  matched in {w['facet']}: {w['text']}" for w in hit["why"]]
    return {"outcome": "ok", "saw": f"{len(hits)} past runs.", "output": "\n".join(lines)}


def _open_run(session: Session, args: dict) -> dict:
    record = store.load(args["run_id"], store.DATA_DIR)
    if record is None:
        return {"outcome": "refused", "saw": f"There is no run called {args['run_id']}."}
    return {"outcome": "ok", "saw": f"Read run {args['run_id']} in full.", "output": cards.account(record, outputs=True)[:38000]}


def _recall_notes(session: Session, args: dict) -> dict:
    found = brain.recall("support", args["text"], 8)
    return {"outcome": "ok", "saw": f"{len(found)} notes.", "output": "\n".join(brain.lines(found)) or "Nothing in your notes about that."}


def _search_rejected(session: Session, args: dict) -> dict:
    found = review.rejected_like(args["text"])
    return {"outcome": "ok", "saw": f"{len(found)} rejected suggestions.", "output": "\n".join(f"#{r['id']} {r['kind']}: {r['title']}. Rejected because: {r['reason']}" for r in found)}


def _list_tags(session: Session, args: dict) -> dict:
    return {"outcome": "ok", "saw": "The full list of tags.", "output": "\n".join(f"{tag}: {text}" for tag, text in tags.known().items())}


def _list_rules(session: Session, args: dict) -> dict:
    wanted = args.get("tags") or []
    lines = []
    for rule in load_ruleset(session.root).parsed:
        if rule.table == "diagnose" and (not wanted or tags.covers(wanted, rule.tags) or tags.covers(rule.tags, wanted)):
            causes = sorted({o.target for s in rule.steps for o in s.then.values() if o.kind == "cause"})
            lines.append(f"{rule.id} [{rule.position}, {rule.mode}] pipeline {pipelines.name_of(rule)} tags {','.join(rule.tags)}: checks {', '.join(s.check.name for s in rule.steps)}; finds {', '.join(causes) or 'nothing named'}")
    taken = sorted(pipelines.name_of(r) for r in load_ruleset(session.root).parsed if r.table == "fix")
    lines.append(f"Repair pipelines already taken: {', '.join(taken)}")
    return {"outcome": "ok", "saw": f"{len(lines) - 1} rules.", "output": "\n".join(lines)}


# --- changing -------------------------------------------------------------

def _why_no_changes(session: Session, device: str) -> str | None:
    if AI_CHANGES_OFF.exists():
        return f"Changes by the AI are switched off ({AI_CHANGES_OFF.name} exists). Investigate and suggest a rule instead."
    if fixer.OFF_SWITCH.exists():
        return f"Automatic repair is switched off ({fixer.OFF_SWITCH.name} exists)."
    if device in FORBIDDEN_DEVICES:
        return f"{device} is the ISP's router, which is out of scope."
    if len([c for c in session.changes if c["status"] in ("kept", "reverted")]) >= MAX_CHANGES:
        return f"You have already made {MAX_CHANGES} changes on this run."
    if history.ai_changes_lately() >= MAX_CHANGES_PER_HOUR:
        return f"The AI has made {MAX_CHANGES_PER_HOUR} changes in the last hour, so no more for now."
    return fixer._job_in_the_way()


def _bare(name) -> str:
    """The tools are called check_ping and the checks ping. Models mix them up, and it is obvious which was meant."""
    name = str(name or "")
    return name[6:] if name.startswith("check_") and name[6:] in CHECKS else name


def _verify_call(verify: dict | None) -> tuple[Block, dict, list[str]]:
    if isinstance(verify, dict):
        verify = {**verify, "check": _bare(verify.get("check"))}
    if not isinstance(verify, dict) or verify.get("check") not in CHECKS:
        raise ValueError(f"verify must name a check: one of the check_* tools, without the check_ prefix. Got {verify!r}.")
    block = CHECKS[verify["check"]]
    args, expect = {k: str(v) for k, v in (verify.get("args") or {}).items()}, [str(x) for x in verify.get("expect") or []]
    missing = [n for n in block.required if n not in args]
    wrong = [e for e in expect if e not in block.all_outcomes]
    if missing or not expect or wrong:
        raise ValueError(f"{block.name} needs {block.required}, and expect must be some of {list(block.outcomes)}.")
    return block, args, expect


def _note_change(session: Session, change: dict) -> dict:
    change["finished"] = time.time()
    session.changes.append(change)
    return {"outcome": change["status"], "saw": change["saw"], "healed": change.get("healed", []), "new_problems": change.get("new", [])}


def _transact(session: Session, tool: str, device: str, what: list[str], args: dict, apply: Callable) -> dict:
    """Apply one change, keep it only if it did some good and broke nothing."""
    temporary = args.get("temporary") is True
    restoring = tool in RESTORES  # going back to known-good is worth keeping even when nothing else got better
    change = {"tool": tool, "device": device, "what": what, "why": args.get("why", ""), "verify": args.get("verify"), "started": time.time(), "steps": [], "temporary": temporary, "was_working": str(args.get("was_working") or ""), "what_changed": str(args.get("what_changed") or ""), "earlier_change": str(args.get("earlier_change") or "")}

    def stop(status: str, saw: str) -> dict:
        return _note_change(session, {**change, "status": status, "saw": saw})

    refused = _why_no_changes(session, device)
    if refused:
        return stop("refused", refused)
    if not change["was_working"].strip() or not change["what_changed"].strip():
        return stop("refused", "Before changing anything, say what you concluded from the first look, in was_working and what_changed.")
    try:
        block, verify_args, expect = _verify_call(args.get("verify"))
    except ValueError as exc:
        return stop("refused", str(exc))
    first = _run_block(session, block, verify_args)
    session.observed.setdefault(_key(block.name, verify_args), {"outcome": first.outcome, "values": {k: str(v) for k, v in first.values.items()}})
    change["verify_before"] = first.outcome
    if first.outcome in expect:
        return stop("refused", f"{block.name} already answers {first.outcome}, so it could not prove the change did anything. Pick a check that fails now.")
    context = fixer.FixContext(event_time=float(session.event["time"]), event=session.event)
    try:
        with lab.change_lock():
            before = health()
            change["problems_before"] = sorted(before)
            res = apply(context)
            change["evidence"] = [e.as_dict() for e in res.evidence]
            if res.outcome != "done":
                fixer._undo(context, change["steps"], lambda _m: None)
                return stop("failed", res.saw)
            change["applied"] = res.saw
            if temporary and tool == "try_config":
                configs.hold(device, what, session.run_id, session.event.get("ticket"), args.get("why", ""))
            deadline, passed, after = time.time() + VERIFY_FOR, False, set(before)
            while True:
                last = _run_block(session, block, verify_args)
                passed = last.outcome in expect
                if passed:
                    after = health()
                    if (before - after) or not before or time.time() >= deadline:
                        break
                elif time.time() >= deadline:
                    after = health()
                    break
                time.sleep(VERIFY_EVERY)
            if (temporary or restoring) and after - before:
                time.sleep(SECOND_LOOK)
                after = (after & before) | ((after - before) & health())
            healed, new = sorted(before - after), sorted(after - before)
            change.update(verify_after=last.outcome, healed=healed, new=new)
            if passed and not new and (healed or not before or temporary or restoring):
                for changed in context.changed:  # what is running now is the new starting point for spotting the next change
                    _shown, lines = configs.running(changed)
                    if lines is not None:
                        configs.save_baseline(changed, lines)
                time.sleep(SETTLE)
                return stop("kept", f"{res.saw} {block.name} now answers {last.outcome}." + (f" Cleared: {', '.join(healed)}." if healed else "") + " It stays.")
            fixer._undo(context, change["steps"], lambda _m: None)
            configs.release(run_id=session.run_id, device=device)
            time.sleep(SETTLE)
            why = (f"it broke {', '.join(new)}" if new else f"{block.name} still answers {last.outcome}" if not passed else "nothing that was wrong got better")
            return stop("reverted", f"Taken back out, because {why}. {' '.join(s['saw'] for s in change['steps'])}")
    except lab.LockBusy:
        return stop("refused", "Another job holds the network change lock, so nothing was changed.")


def _named(items: list[dict]) -> str:
    return "; ".join(f"{' '.join(item['lines'])} ({'ticket T-' + str(item['ticket']) if item.get('ticket') else 'run ' + item['run']}), because: {item.get('why') or 'no reason recorded'}" for item in items)


def _earlier_problem(session: Session, device: str, lines: list[str], args: dict) -> str | None:
    """Something left in place for a reason is only ever changed by someone who has weighed that reason."""
    theirs = configs.touching(device, lines, skip_run=session.run_id)
    if not theirs:
        return None
    removing = configs.takes_out(lines, theirs)
    if removing:
        return f"{', '.join(removing)} would take out an earlier change: {_named(theirs)}. If the reason for it is gone, use retire_earlier_change and say what shows that."
    if not str(args.get("earlier_change") or "").strip():
        return f"This touches an earlier change: {_named(theirs)}. Say in earlier_change whether the reason for it is still there, and how this change still does what it was for."
    if args.get("temporary") is not True:
        return f"This goes on top of an earlier change, so it can only be temporary: {_named(theirs)}. Set temporary true."
    return None


def _try_config(session: Session, args: dict) -> dict:
    device, lines = args["device"], [str(x) for x in args.get("lines") or []]
    problem = safety.config_problem(device, lines) or _earlier_problem(session, device, lines, args)
    if problem:
        return _note_change(session, {"tool": "try_config", "device": device, "what": lines, "why": args.get("why", ""), "started": time.time(), "status": "refused", "saw": problem})
    return _transact(session, "try_config", device, lines, args, lambda ctx: ACTIONS["config_lines"].func(ctx, device, lines))


def _try_command(session: Session, args: dict) -> dict:
    device, argv, undo = args["device"], [str(x) for x in args.get("argv") or []], [str(x) for x in args.get("undo_argv") or []]
    problem = safety.command_problem(device, argv) or safety.command_problem(device, undo) if undo else "Every command needs undo_argv, the exact command that reverses it."
    if problem:
        return _note_change(session, {"tool": "try_command", "device": device, "what": [" ".join(argv)], "why": args.get("why", ""), "started": time.time(), "status": "refused", "saw": problem})
    return _transact(session, "try_command", device, [" ".join(argv)], args, lambda ctx: ACTIONS["linux_change"].func(ctx, device, argv, undo))


def _retire(session: Session, args: dict) -> dict:
    """Take a stop-gap back out, whole, on every device it touched. If that does not hold up, it goes back as it was, reasons included."""
    run = str(args.get("run") or "")
    held = [item for item in configs.holds() if item.get("run") == run]
    note = lambda status, saw: _note_change(session, {"tool": "retire_earlier_change", "device": "", "what": [run], "why": args.get("why", ""), "started": time.time(), "status": status, "saw": saw})
    if not held:
        return note("refused", f"Nothing is being kept for run {run!r}. Being kept now: {', '.join(sorted({item['run'] for item in configs.holds()})) or 'nothing'}.")
    if run == session.run_id:
        return note("refused", "That is your own change from this run. If it did not help it would already have been taken back out.")
    if not str(args.get("earlier_change") or "").strip():
        return note("refused", "Say in earlier_change what you saw that shows the reason for this change is gone.")
    devices, released = sorted({item["device"] for item in held}), []

    def apply(ctx):
        released.extend(configs.release(run_id=run))
        try:
            results = [ACTIONS["converge_config"].func(ctx, device) for device in devices]
        except Exception as exc:  # the transaction undoes what did go through
            return lab.Result("failed", f"Taking it out failed: {type(exc).__name__}: {exc}")
        saw, evidence = " ".join(r.saw for r in results), [e for r in results for e in r.evidence]
        return lab.Result("done" if any(r.outcome == "done" for r in results) and not any(r.outcome == "failed" for r in results) else "failed", saw, evidence=evidence)

    try:
        result = _transact(session, "retire_earlier_change", ", ".join(devices), [f"{item['device']}: take out {'; '.join(item['lines'])}" for item in held], args, apply)
    except Exception:
        configs.put_back(released)
        raise
    if result["outcome"] != "kept":
        configs.put_back(released)
    elif held[0].get("ticket"):
        tickets.note(held[0]["ticket"], "Service desk", "desk", f"The temporary change made for this ticket was taken out on {', '.join(devices)}, during run {session.run_id}: {args['earlier_change']}", private=True)
    return result


def _action_tool(block: Block) -> Tool:
    def run(session: Session, args: dict) -> dict:
        given = {p.name: args[p.name] for p in block.params if p.name in args}
        device = str(given.get("device") or session.event.get("device") or "")
        return _transact(session, f"action_{block.name}", device, [block.name + " " + " ".join(f"{k}={v}" for k, v in given.items())], args, lambda ctx: block.func(ctx, **given))

    extra = {"why": STR, "verify": _VERIFY, **_FIRST}
    return Tool(f"action_{block.name}", f"{block.description}. Applied as a transaction: taken back out automatically unless it helps and breaks nothing.", _block_schema(block, extra, ("why", "verify", "was_working", "what_changed")), run, "change")


# --- suggesting -----------------------------------------------------------

def _replay(rule, session: Session) -> dict:
    """Walk the rule using what was actually seen on this run. Proves it would have reached its cause, without touching anything."""
    values = {k: str(v) for k, v in session.event.items() if isinstance(v, (str, int, float)) and k != "raw"}
    values["alarm_device"] = values.get("device", "")
    index = {s.id: i for i, s in enumerate(rule.steps) if s.id}
    path, i = [], 0
    while i < len(rule.steps):
        step = rule.steps[i]
        args = engine.fill_args(step.check.args, values)
        if step.check.name == "output_has":
            seen_text = session.shown.get((args.get("device"), (args.get("command") or "").strip()))
            if seen_text is None:
                return {"ok": False, "why": f"Step {i + 1} reads {args.get('command')!r} on {args.get('device')}, but you never ran exactly that with show on this incident."}
            seen = {"outcome": "found" if args["text"] in seen_text else "absent", "values": {}}
        else:
            seen = session.observed.get(_key(step.check.name, args))
            if seen is None:
                return {"ok": False, "why": f"Step {i + 1} is {step.check.name} with {args}, but you never ran exactly that on this incident, so the rule cannot be tested."}
        values.update(seen["values"])
        chosen = step.then.get(seen["outcome"]) or Outcome(kind="next")
        path.append({"step": i + 1, "check": step.check.name, "answered": seen["outcome"], "then": chosen.render()})
        if chosen.kind == "next":
            i += 1
        elif chosen.kind == "jump":
            i = index[chosen.target]
        elif chosen.kind == "cause":
            return {"ok": True, "reached": chosen.target, "path": path, "values": {**values, **{k: engine.fill(str(v), values) for k, v in chosen.params.items()}}}
        else:
            return {"ok": False, "why": f"On what you saw, step {i + 1} ends in '{chosen.render()}', so the rule would not reach a cause."}
    return {"ok": False, "why": "On what you saw, the rule runs off the end without reaching a cause."}


def _where_it_works(fix: dict) -> list[str]:
    """The platforms a fix can both act on and check on the same device. Picked anywhere else, it would only answer unsupported."""
    acts_on = (fix.get("args") or {}).get("device")
    blocks = [ACTIONS.get(fix["action"])] + [CHECKS.get(v["check"]) for v in fix["verify"] if (v.get("args") or {}).get("device") == acts_on]
    limited = [set(b.platforms) for b in blocks if b and "any" not in b.platforms]
    return sorted(set.intersection(*limited)) if limited else []


def _build(session: Session, a: dict) -> list[dict]:
    wanted, position = list(a["trigger"]["tags"]), a["position"]
    match = {"tags": wanted, **{k: a["trigger"][k] for k in ("platform", "role", "event") if a["trigger"].get(k)}}
    source = {"by": "ai", "incident": session.run_id, "note": session.model or "AI"}
    steps = [{"id": f"s{n}", "ask": s["ask"], "check": {"name": s["check"], "args": s.get("args") or {}}, "then": s.get("then") or {}} for n, s in enumerate(a["steps"], 1)]
    rules = [{"id": a["id"], "pipeline": a["pipeline"], "title": a["title"], "table": "diagnose", "priority": 100, "description": a["description"], "trigger": f"Runs for {', '.join(wanted)}, at the {position} of the checks.",
              "match": match, "mode": "proposed", "position": position, "tags": wanted, "source": source, "steps": steps, "causes": a["causes"],
              "end": "stop This rule's checks found nothing, so the others carry on."}]
    fix = a.get("fix")
    if fix:
        do = {"name": fix["action"], "args": fix.get("args") or {}}
        rules.append({"id": f"{a['id']}-fix", "pipeline": fix.get("pipeline") or f"{a['pipeline']}-fix", "title": fix["title"], "table": "fix", "priority": 100, "description": fix.get("description") or fix["title"] + " (suggested by the AI)", "match": {"cause": sorted(a["causes"]), **({"platform": where} if (where := _where_it_works(fix)) else {})},
                      "mode": "proposed", "position": "main", "tags": wanted, "source": source, "require": ["change_window_open == closed"], "action": {"do": do},
                      "verify": [{"check": {"name": v["check"], "args": v.get("args") or {}}, "expect": v["expect"]} for v in fix["verify"]], "risk": fix.get("risk", "medium")})
    return rules


def _meaning(raws: list[dict]) -> str:
    """Two rules that run the same checks for the same tags and reach the same answer are the same suggestion, whatever they are called."""
    diagnose = raws[0]
    fix = next((r for r in raws[1:]), None)
    return review.fingerprint("rule", sorted(diagnose["match"]["tags"]), [(s["check"]["name"], sorted(s["check"]["args"].items())) for s in diagnose["steps"]], sorted(diagnose["causes"]), fix and fix["action"]["do"])


def _said_before(seen: dict) -> dict:
    status = seen["status"]
    if status == review.REJECTED:
        return {"outcome": "refused", "saw": f"A person already rejected this (#{seen['id']}): {seen['reason']} Do not suggest it again."}
    return {"outcome": "refused", "saw": f"This was already suggested (#{seen['id']}) and is {status}."}


def _fix_problem(session: Session, a: dict, rule, values: dict) -> str | None:
    """A repair has to act where the fault was found, and be judged by something that can actually fail."""
    fix = a.get("fix")
    if not fix:
        return None
    found_on = {o.params["device"] for s in rule.steps for o in s.then.values() if o.kind == "cause" and "device" in o.params}
    target = str((fix.get("args") or {}).get("device", ""))
    if target and "{" not in target and found_on and found_on != {target}:
        return f"This rule can find the fault on {', '.join(sorted(found_on))}, but the fix always runs on {target}. Use {{device}} so it acts where the fault was found."
    for v in fix.get("verify") or []:
        filled = engine.fill_args(v.get("args") or {}, values)
        seen = session.observed.get(_key(v["check"], filled))
        said = f"{v['check']} {' '.join(str(x) for x in filled.values())}"
        if seen is None:
            return (f"After the fix it would check {said}, which you never ran while the fault was there, so nothing shows that check can tell broken from fixed. "
                    "A cause with device=X makes {device} mean X in the fix. {alarm_device} is always the device the alarm came from.")
        if seen["outcome"] in v["expect"]:
            return f"{said} already answered {seen['outcome']} while the fault was there, so passing it afterwards proves nothing. Pick a check that fails while it is broken."
    return None


def _propose_rule(session: Session, a: dict) -> dict:
    a = {**a, "steps": [{**s, "check": _bare(s.get("check"))} for s in a.get("steps") or []]}
    if isinstance(a.get("fix"), dict):
        a["fix"] = {**a["fix"], "verify": [{**v, "check": _bare(v.get("check"))} for v in a["fix"].get("verify") or []]}
    if not RULE_ID.match(a.get("id", "")):
        return {"outcome": "invalid", "saw": "id must be lowercase words joined by hyphens, 3 to 56 characters."}
    a["pipeline"] = a.get("pipeline") or a["id"]
    named = [a["pipeline"]] + ([a["fix"]["pipeline"]] if isinstance(a.get("fix"), dict) and a["fix"].get("pipeline") else [])
    if not all(PIPELINE_NAME.match(n) and len(n) <= 60 for n in named):
        return {"outcome": "invalid", "saw": "pipeline (and fix.pipeline) must be lowercase words joined by single hyphens, 3 to 60 characters, such as ospf-hello-timer-mismatch."}
    for name in named:
        taken = pipelines.clash(load_ruleset(session.root).parsed, name, "")
        if taken:
            return {"outcome": "invalid", "saw": f"{taken} Pick another name for the Jenkins pipeline."}
    if a.get("position") not in ("beginning", "end"):
        return {"outcome": "invalid", "saw": "position must be beginning or end. The main rule for a trigger is not replaced by a suggestion."}
    if not a.get("trigger", {}).get("tags"):
        return {"outcome": "invalid", "saw": "trigger.tags must name at least one tag from list_tags."}
    if load_ruleset(session.root).get(a["id"]):
        return {"outcome": "invalid", "saw": f"A rule called {a['id']} already exists."}
    if any(s.get("check") not in CHECKS for s in a.get("steps") or [{}]):
        return {"outcome": "invalid", "saw": f"Each step's check must be a registered check, without the check_ prefix. Known: {', '.join(sorted(CHECKS))}."}
    try:
        raws = _build(session, a)
        parsed, issues = rulebook.check(session.root, raws)
    except (RuleLoadError, KeyError, TypeError, ValueError) as exc:
        return {"outcome": "invalid", "saw": f"Not saved. {exc}"}
    if errors(issues):
        return {"outcome": "invalid", "saw": "Not saved: " + "; ".join(i.message for i in errors(issues))}
    replayed = _replay(parsed[0], session)
    if not replayed["ok"]:
        return {"outcome": "invalid", "saw": "Not saved: " + replayed["why"]}
    problem = _fix_problem(session, a, parsed[0], replayed.pop("values"))
    if problem:
        return {"outcome": "invalid", "saw": "Not saved: " + problem}
    print_ = _meaning(raws)
    seen = review.earlier(print_)
    if seen:
        return _said_before(seen)
    proof = {"lint": [str(i) for i in issues], "replay": replayed, "events": sorted(tags.events_for(a["trigger"]["tags"]))}
    number = review.add("rule", a["title"], a.get("why", ""), {"rules": raws, "position": a["position"], "trigger": a["trigger"]}, proof, session.run_id, session.model, print_)
    session.proposals.append(number)
    near = review.rejected_like(f"{a['title']} {a.get('why', '')} {' '.join(a['causes'])}", 3)
    warn = (" Close to something already rejected: " + "; ".join(f"#{r['id']} {r['title']}: {r['reason']}" for r in near)) if near else ""
    return {"outcome": "saved", "saw": f"Waiting for review as #{number}. It replayed cleanly on what you saw and reached {replayed['reached']}.{warn}"}


def _past_runs(a: dict, tag: str) -> tuple[list[dict], list[str]]:
    """The runs that can really take this tag, and what was dropped and why."""
    good, dropped, refused = [], [], review.rejected_runs(tag)
    for item in a.get("runs") or []:
        run_id = str(item.get("run_id", "")) if isinstance(item, dict) else ""
        if not (store.exists(run_id) or history.run_exists(run_id)):
            dropped.append(f"{run_id or item}: no such run")
        elif run_id in refused:
            dropped.append(f"{run_id}: a person already said this tag does not belong there")
        elif tag in history.tags_of(run_id):
            dropped.append(f"{run_id}: already has it")
        else:
            good.append({"run_id": run_id, "why": str(item.get("why", ""))})
    return good, dropped


def _propose_tag(session: Session, a: dict) -> dict:
    name, area = str(a.get("name", "")).lower(), str(a.get("area", "")).lower()
    problem = tags.name_problem(name, area)
    if problem:
        return {"outcome": "invalid", "saw": problem}
    tag = f"{area}/{name}"
    print_ = review.fingerprint("tag", tag)
    seen = review.earlier(print_)
    if seen:
        return _said_before(seen)
    runs, dropped = _past_runs(a, tag)
    events = [e for e in a.get("events") or [] if e in tags.EVENT_TAGS]
    number = review.add("tag", f"New tag {tag}", a.get("why", ""), {"tag": tag, "area": area, "name": name, "description": a.get("description", ""), "events": events, "runs": runs}, {"dropped": dropped}, session.run_id, session.model, print_)
    session.proposals.append(number)
    return {"outcome": "saved", "saw": f"Waiting for review as #{number}, with {len(runs)} past runs to tag." + (f" Left out: {'; '.join(dropped)}" if dropped else "")}


def _propose_backdate(session: Session, a: dict) -> dict:
    tag = str(a.get("tag", ""))
    if tag not in tags.known():
        return {"outcome": "invalid", "saw": f"{tag!r} is not a tag. Use list_tags, or propose_tag for a new one."}
    runs, dropped = _past_runs(a, tag)
    if not runs:
        return {"outcome": "invalid", "saw": "No past runs left to tag. " + "; ".join(dropped)}
    print_ = review.fingerprint("backdate", tag, sorted(r["run_id"] for r in runs))
    seen = review.earlier(print_)
    if seen:
        return _said_before(seen)
    number = review.add("backdate", f"Add {tag} to {len(runs)} past runs", a.get("why", ""), {"tag": tag, "runs": runs}, {"dropped": dropped}, session.run_id, session.model, print_)
    session.proposals.append(number)
    return {"outcome": "saved", "saw": f"Waiting for review as #{number}."}


_ASKS_SOMETHING = re.compile(r"\?|\b(please|can you|could you|try|run|tell us|let us know)\b", re.I)


def _reads_allowed(session: Session) -> int:
    return MAX_READS_ON_A_TICKET if session.event.get("ticket") else MAX_READS


def _ask_customer(session: Session, args: dict) -> dict:
    number = session.event.get("ticket")
    if not number:
        return {"outcome": "refused", "saw": "Nobody raised this one, so there is no one to ask."}
    if sum(c["tool"] == "ask_customer" for c in session.calls) >= MAX_ASKS:
        return {"outcome": "refused", "saw": f"You have asked {MAX_ASKS} times on this run. Work with what you have, or finish."}
    message = str(args["message"]).strip()
    if any(c.get("status") == "kept" for c in session.changes):
        return {"outcome": "refused", "saw": "You have already changed something. Call finish: they are asked whether it is working as soon as you do."}
    if not _ASKS_SOMETHING.search(message):
        return {"outcome": "refused", "saw": "This is only for asking them something. Do not send thanks or updates."}
    if any(str((c.get("args") or {}).get("message", "")).strip().lower() == message.lower() for c in session.calls if c["tool"] == "ask_customer"):
        return {"outcome": "refused", "saw": "You have already asked exactly that. Use what they said, or ask something else."}
    if any(n["role"] == "desk" and n["body"].strip().lower() == message.lower() for n in tickets.notes_of(number, private=False)):
        return {"outcome": "refused", "saw": "That has already been asked on this ticket, and what they said is in the conversation. Use it, or ask something else."}
    began = time.time()
    try:
        said = tickets.ask(number, message)
    finally:
        session.waited += time.time() - began
    if said is None:
        return {"outcome": "no_reply", "saw": "They have not answered yet. Carry on with what you can see from the network, or finish."}
    return {"outcome": "answered", "saw": "They replied.", "output": said}


def _finish(session: Session, a: dict) -> dict:
    if a.get("outcome") not in ("fixed", "workaround", "partly", "needs_person", "nothing_wrong"):
        return {"outcome": "invalid", "saw": "outcome must be fixed, workaround, partly, needs_person or nothing_wrong."}
    kept = [c for c in session.changes if c["status"] == "kept"]
    if a["outcome"] == "fixed" and not kept:
        return {"outcome": "invalid", "saw": "You cannot say fixed: no change of yours was kept. Use needs_person, partly or nothing_wrong."}
    if a["outcome"] == "workaround" and not any(c.get("temporary") for c in kept):
        return {"outcome": "invalid", "saw": "You cannot say workaround: no temporary change of yours was kept. Use try_config with temporary true first, or use needs_person."}
    session.result = {k: a.get(k) for k in ("outcome", "root_cause", "summary", "assessment", "why", "next", "related", "used", "tags")}
    return {"outcome": "done", "saw": "Finished."}


_STEP = _object({"ask": {**STR, "description": "The question this step answers, in plain words"}, "check": {**STR, "description": "A check name, without check_"},
                 "args": {"type": "object", "description": "Use {device} and other event fields so the rule works elsewhere"},
                 "then": {"type": "object", "description": "Answer -> 'cause area.what', 'cause area.what device=r3', 'stop message', 'escalate message'. Answers left out go on to the next step."}}, ["ask", "check", "args"])
_FIX = _object({"title": STR, "action": {**STR, "description": "An action name, such as config_lines, linux_change or converge_config"}, "args": {"type": "object"},
                "verify": {"type": "array", "items": _object({"check": STR, "args": {"type": "object"}, "expect": STRS}, ["check", "args", "expect"])}, "risk": {"type": "string", "enum": ["low", "medium"]},
                "pipeline": {**STR, "description": "The Jenkins pipeline this repair becomes. Leave it out to use the rule's pipeline name with -fix on the end."}}, ["title", "action", "args", "verify"])
_PIPELINE = {**STR, "description": "The name of the Jenkins pipeline this rule becomes, in the troubleshooting-pipelines folder. Every run of the rule is a build of it. "
                                   "Lowercase words joined by hyphens, 3 to 60 characters, saying what it looks for, such as ospf-hello-timer-mismatch. It must not be one list_rules already shows."}
_TEMPORARY = {"type": "boolean", "description": "True for a stop-gap that keeps people working until someone can do the real repair, such as raising OSPF cost to move traffic off a flapping link. It is kept if its verify passes and nothing new breaks, and stays through the automatic return to known-good config until the work is closed."}
_FIRST = {"was_working": {**STR, "description": "Your answer to the first question, from the first look in your brief: was this working before, and until when?"},
          "what_changed": {**STR, "description": "Your answer to the second question: what differs from known-good or changed recently, and does it explain this?"}}
_EARLIER = {"earlier_change": {**STR, "description": "Only when this touches something listed under CHANGED HERE EARLIER in your brief: is the reason for that change still there, and how does this one still do what it was for?"}}
_RUNS = {"type": "array", "items": _object({"run_id": STR, "why": STR}, ["run_id", "why"]), "description": "Past runs this should be applied to. search_runs finds them."}


def _fixed_tools() -> list[Tool]:
    return [
        Tool("show", "Run one read-only command on a device: a show command on routers and switches, or ip, ss, ps, iptables -S, tc qdisc show, sysctl, cat of /proc/net on Linux routers and hosts.", _object({"device": STR, "command": STR}, ["device", "command"]), _show),
        Tool("search_memory", "Search everything the monitoring, jobs and git have recorded over the past weeks.", _object({"text": STR}, ["text"]), _search_memory),
        Tool("search_jenkins", "Search every Jenkins build of this network's jobs: tests, deployments, config changes, password rotation, housekeeping, and the troubleshooting-pipelines builds of the rules themselves. Finds what ran, when, who or what started it, how it ended and what its console printed. Narrow it with job (all or part of a job name) and result (SUCCESS, FAILURE, UNSTABLE, ABORTED). With nothing at all it lists every job and its latest build. read_build reads one build whole.",
             _object({"text": {**STR, "description": "Words to look for, such as an error, a device or a test name. Empty lists the latest builds."}, "job": STR, "result": STR}, []), _search_jenkins),
        Tool("read_build", "Read one Jenkins build whole: what started it, its parameters, and everything its console printed, a part at a time.", _object({"job": {**STR, "description": "The job's full name, as search_jenkins shows it"}, "number": {"type": "integer"}, "part": {"type": "integer", "description": "Which part of a long console, from 1"}}, ["job", "number"]), _read_build),
        Tool("search_runs", "Search inside every past investigation, by meaning and by exact words: symptoms, causes, every command and what it printed, every change, what people decided. A cause name or an exact command or error text works as well as a description. Gives each run's id, cause and note, and the passage that matched. Says so when nothing matches. open_run reads one in full, and propose_tag and propose_backdate need the ids.", _object({"text": STR}, ["text"]), _search_runs),
        Tool("open_run", "Read one past investigation in full: every check, every command with its output, every change and what people decided.", _object({"run_id": STR}, ["run_id"]), _open_run),
        Tool("recall_my_notes", "Search your own notes: what you concluded on earlier work, what you changed, and whether it held up once the person answered.", _object({"text": STR}, ["text"]), _recall_notes),
        Tool("search_rejected", "Search suggestions a person already turned down, and why.", _object({"text": STR}, ["text"]), _search_rejected),
        Tool("list_tags", "The full list of tags.", _object({}, []), _list_tags),
        Tool("list_rules", "The rules that already exist, optionally only those for some tags.", _object({"tags": STRS}, []), _list_rules),
        Tool("try_config", "Apply a few config lines to one router or switch, as a transaction: it is taken back out automatically unless it helps and breaks nothing.", _object({"device": STR, "lines": STRS, "why": STR, "verify": _VERIFY, "temporary": _TEMPORARY, **_EARLIER, **_FIRST}, ["device", "lines", "why", "verify", "was_working", "what_changed"]), _try_config, "change"),
        Tool("try_command", "Run one Linux-level change on a Linux router or host (ip route/link/addr, tc qdisc, iptables one rule, sysctl forwarding, dnsmasq), as a transaction. undo_argv is the exact command that reverses it.", _object({"device": STR, "argv": STRS, "undo_argv": STRS, "why": STR, "verify": _VERIFY, "temporary": _TEMPORARY, **_FIRST}, ["device", "argv", "undo_argv", "why", "verify", "was_working", "what_changed"]), _try_command, "change"),
        Tool("retire_earlier_change", "Take back out something listed under CHANGED HERE EARLIER, every line of it on every device, returning those parts of the config to known-good. Only when the reason it was made is really gone. Applied as a transaction: if it does not hold up, the earlier change goes back exactly as it was.",
             _object({"run": {**STR, "description": "The run that made it, as listed in your brief"}, "earlier_change": {**STR, "description": "What you saw that shows the reason for it is gone. Evidence over time, not one good test."}, "why": STR, "verify": {**_VERIFY, "description": "A check showing the trouble you are working on, failing now because of this earlier change, that should pass once it is out."}, **_FIRST},
                     ["run", "earlier_change", "why", "verify", "was_working", "what_changed"]), _retire, "change"),
        Tool("propose_rule", "Suggest a rule so this becomes a predictable step next time. A person reviews it. Every step must be something you actually ran on this incident: it is replayed against what you saw and must reach its cause.",
             _object({"id": STR, "pipeline": _PIPELINE, "title": STR, "description": STR, "why": STR,
                      "trigger": _object({"tags": {**STRS, "description": "What kind of trouble it runs for: an area such as routing, or a tag such as routing/ospf"}, "platform": STR, "role": STR}, ["tags"]),
                      "position": {"type": "string", "enum": ["beginning", "end"], "description": "beginning: before the normal checks, for something cheap that settles it. end: only once the normal checks have run out."},
                      "steps": {"type": "array", "items": _STEP}, "causes": {"type": "object", "description": "Cause label (area.what) -> one plain sentence"}, "fix": _FIX},
                     ["id", "pipeline", "title", "description", "why", "trigger", "position", "steps", "causes"]), _propose_rule, "propose"),
        Tool("propose_tag", "Suggest a new tag, only when no existing tag fits. Areas are fixed. Include the past runs it should go on.", _object({"name": STR, "area": STR, "description": STR, "why": STR, "events": {**STRS, "description": "Event types that should carry this tag, if any"}, "runs": _RUNS}, ["name", "area", "description", "why"]), _propose_tag, "propose"),
        Tool("propose_backdate", "Suggest adding an existing tag to past runs that should have it.", _object({"tag": STR, "why": STR, "runs": _RUNS}, ["tag", "why", "runs"]), _propose_backdate, "propose"),
        Tool("ask_customer", "Write to the person who reported this and wait for their answer: what they see, whether it is happening right now, or the result of a command you ask them to run on their computer. One request at a time, in plain words, with the exact command to type. No device names or jargon.", _object({"message": STR}, ["message"]), _ask_customer, "ask"),
        Tool("finish", "End your work on this incident. Call it exactly once, last.",
             _object({"outcome": {"type": "string", "enum": ["fixed", "workaround", "partly", "needs_person", "nothing_wrong"], "description": "workaround: a temporary change of yours is keeping people working, and a person still has to do the real repair."}, "root_cause": STR, "summary": {**STR, "description": "Two or three plain sentences for a tired on-call engineer"},
                      "assessment": {"type": "string", "enum": ["none", "scheduled_job", "bad_hardware", "tampering", "misconfiguration", "software", "unknown"]}, "why": STR, "next": {**STR, "description": "What a person should do, or 'nothing'"},
                      "related": STRS, "used": {**STRS, "description": "[R1] or [M1] labels you relied on"}, "tags": {**STRS, "description": "Tags from list_tags that describe this incident"}},
                     ["outcome", "root_cause", "summary", "assessment", "why", "next"]), _finish, "finish"),
    ]


def registry() -> dict[str, Tool]:
    tools = [*(_check_tool(b) for b in CHECKS.values()), *(_action_tool(b) for b in ACTIONS.values() if b.platforms != frozenset({"any"}) and b.name not in ("config_lines", "linux_change") and b.func.__module__.startswith("ts.")), *_fixed_tools()]
    return {t.name: t for t in tools}


REASON = {"type": "string", "description": "One sentence: why you are running this, and what you expect it to tell you or change."}


def definitions(someone_asked: bool = False) -> list[dict]:
    """The tools in the shape a chat model takes them. Asking the person is only offered when a person raised it."""
    return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema if t.kind == "finish" else _with_reason(t.schema)}} for t in registry().values() if someone_asked or t.name != "ask_customer"]


def _with_reason(schema: dict) -> dict:
    return {**schema, "properties": {**schema.get("properties", {}), "reason": REASON}, "required": [*schema.get("required", []), "reason"]}


def _used(session: Session, *kinds: str) -> int:
    return sum(c["kind"] in kinds for c in session.calls)


def call(session: Session, name: str, args: dict | None) -> dict:
    """Run one tool, record it, and return what goes back to the model."""
    tools, started = registry(), time.time()
    tool = tools.get(name)
    reason = ""
    if isinstance(args, dict) and "reason" in args and "reason" not in (tool.schema.get("properties", {}) if tool else {}):  # kept apart, so a rule learned from this call never carries it
        reason = str(args.get("reason") or "").strip()
        args = {k: v for k, v in args.items() if k != "reason"}
    if tool is None:
        result = {"outcome": "invalid", "saw": f"There is no tool called {name}."}
    elif not isinstance(args, dict):
        result = {"outcome": "invalid", "saw": "The arguments were not valid JSON."}
    elif tool.kind == "read" and _used(session, "read") >= _reads_allowed(session):
        result = {"outcome": "refused", "saw": f"You have used all {_reads_allowed(session)} reads. Change what you found, suggest a rule, or finish."}
    elif tool.kind in ("change", "propose") and _used(session, "change", "propose") >= MAX_ACTS:
        result = {"outcome": "refused", "saw": f"You have used all {MAX_ACTS} changes and suggestions. Call finish now."}
    else:
        missing = [key for key in tool.schema.get("required", []) if key not in args]
        if missing:
            result = {"outcome": "invalid", "saw": f"Missing: {', '.join(missing)}."}
        else:
            try:
                result = tool.run(session, args)
            except Exception as exc:  # a broken tool is reported to the model, never fatal
                result = {"outcome": "error", "saw": f"{type(exc).__name__}: {exc}"}
    result = scrub.deep(result)
    kind = tool.kind if tool else "read"
    entry = {"n": len(session.calls) + 1, "tool": name, "kind": kind, "args": scrub.deep(args if isinstance(args, dict) else {}), "reason": scrub.text(reason)[:1000], "outcome": result.get("outcome"), "saw": result.get("saw"),
             "output": result.get("output", ""), "ran": result.get("ran", []), "started": started, "ms": int((time.time() - started) * 1000)}
    session.calls.append(entry)
    history.audit(session.run_id, name, kind, json.dumps(entry["args"])[:4000], str(result.get("outcome")), str(result.get("saw"))[:2000], str(result.get("output", ""))[:6000])
    back = {k: v for k, v in result.items() if k != "ran"}
    back["reads_left"] = max(0, _reads_allowed(session) - _used(session, "read"))
    if back.get("output"):
        back["output"] = back["output"][:LLM_OUTPUT]
    return back
