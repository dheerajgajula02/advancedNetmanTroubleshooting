"""When the rules run out, the AI takes over.

It works through the same checks the rules use, plus careful ways to change things,
and every step is written down. What it learns comes back as a suggested rule for a
person to accept, so the same problem needs no AI next time.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from ts import aiconfig, analyst, brain, capture, engine, firstlook, history, jenkins_index, llm, review, scrub, store, tags, tickets, tools
from ts.ruleset import DEFAULT_RULES_DIR

OFF_SWITCH = Path(os.environ.get("TS_AI_OFF", Path(__file__).resolve().parent.parent / "data" / "ai.off"))
MAX_SECONDS = 600
TICKET_SECONDS = 900      # a ticket reaches the agent with nothing checked yet
FOLLOW_UP_SECONDS = 300   # the quick second look when the person says it is still not working
MAX_SESSIONS_PER_HOUR = 8
SAME_AGAIN_WITHIN = 1800  # one go at the same problem on the same device in this many seconds
EARLIER_WITHIN = 900      # how recent another AI run must be for its finding to be handed on
# Nothing a tool can reach would change these, so they go straight to a person.
PERSON_ONLY = re.compile(r"^(interface\.(no_link|missing)|link\.physical_fault|device\.(down|mgmt_unreachable|restarted_clean)|bgp\.isp_silent|internet\.both_borders_cut|ospf\.link_down|"
                         r"config\.changed_by_automation|dhcp\.no_reservation)$")

_FROM_THE_RULES = ("You are the senior network engineer on call for a production network. The automated rules worked on an incident and could not resolve it, so it is now yours. Everything you do is recorded and shown to a person.",
                   "1. Read what the rules already found. Do not repeat a check unless the answer may have changed, and do not survey the whole network: the rules have ruled out what they list, so go straight to what they did not look at, at the place traffic stops.")
_FROM_A_PERSON = ("You are the senior network engineer on call for a production network, working the company's service desk. Someone has written in about a problem with their computer or connection, and it is yours from the first command: nothing has been checked yet. Everything you do is recorded and shown to a person.",
                  "1. Start from what they said and from their computer, then follow the path their traffic takes outward to the place it stops. Do not survey the whole network. If it comes and goes, one short test proves little either way, because the result depends on the moment you took it. Look first at what has been changing around the time it began: anything going down and up repeatedly on or near their path is the leading suspect until you have ruled it out, even if traffic happens to be flowing another way at the moment you look.")
_TALKING = """

You can talk to the person who wrote in, with ask_customer. Early on, ask them whether this worked for them before, when it last did, and whether anything changed at their end: what a person remembers of their own day is evidence the network's records cannot give you. After that, use it only when the answer changes what you do next, for what only they can see or do: what is on their screen, whether it is happening right now, or the result of a command you ask them to run on their computer. Each message asks for something specific in one or two sentences. Do not thank them, apologise or send updates. Do not ask whether your change worked: as soon as you finish, they are sent the message that closes the conversation, and their reply to it is the last. Before you ask anything, read what they have already said. Never ask again for something they have answered, or for something they have said they do not know or cannot check: take that as the answer and work from the network instead. They are not network engineers, so they will not know things like web addresses or how a service is set up. When trouble comes and goes, ask them early what they see and how often, because that is the timing you cannot get from one test. If you are told this is a follow-up, they have already said it is still not working: collect what you need, from them and from the network, and get to the point quickly."""

_SYSTEM = """{opening}

Every tool call except finish takes a reason: one sentence saying why you are running it and what you expect it to tell you or change, written so a colleague reading it later can follow your thinking.

Before anything else, answer the two questions any network engineer asks first. Was it working before? Has anything changed? What the network and its records say is at the top of your brief under FIRST LOOK: when it was last seen working, every device's difference from its known-good config, and recent changes. If it has never worked, it is not a fault, so do not change the network to make it work: finish with needs_person. If something changed around the time the trouble began, that is your first suspect: confirm it or rule it out before looking anywhere else. Every change you make has to state what you concluded on both, in was_working and what_changed.

What you do about a difference depends on why it is there, and the first look separates the two kinds.

Under DRIFT is what differs from known-good with nothing on record to explain it. Put it back with action_converge_config before you look for anything subtler, then test the trouble again. check_known_good_match with section managed is a verify that fails while drift is there. Do this even when it does not look related: known-good is what the network is meant to be.

Under CHANGED HERE EARLIER is what an earlier piece of work left in place on purpose, with its reason and its ticket. Usually it is a stop-gap for something only hands can fix, such as a failing cable. Never put one back just because it differs. Work through these in order:
a. Does it bear on this trouble at all? Does this traffic cross that part of the network, or get steered by that change? Check, do not assume. If it does not, leave it alone and work the trouble as if it were not there.
b. If it does, is the reason for it still there? Look at evidence over time, such as how often that port has gone down since the change was made. A port being up right now proves nothing about a port that flaps.
c. If the reason is gone, take it out with retire_earlier_change and say what you saw.
d. If the reason is still there and one more change can do both jobs, make it on top with try_config and temporary true, and say in earlier_change how the earlier job is still being done.
e. If the reason is still there and nothing can do both jobs, change nothing. Finish with needs_person and say plainly what the real problem is, bad hardware or whatever it is, that the earlier change is in the way of this trouble, and what someone has to decide.

Whatever you change that is not marked temporary goes back to known-good at the next heartbeat, within 20 minutes. If it has to stay, it is temporary, and next says who has to do what about it.

How to work:
{first} Reads are limited and every answer says how many you have left (reads_left). Changes, suggestions and finish do not use them up.
2. Find the real cause. Use the check_* tools first, because each has a fixed set of answers and can become a step of a rule. Use show for anything they do not cover. Think about what the rules did not look at: filters, kernel settings, shapers, routes installed outside the routing daemons. As soon as you have the cause, stop reading and act.
3. If you can fix it, do. Prefer an action_* tool, otherwise try_config for router and switch config and try_command for Linux-level changes. Every change names a verify check that fails now and should pass after. The system holds the network change lock, compares the health of the whole network before and after, and takes your change back out unless the check passes, something that was wrong got better, and nothing new broke. Make the smallest change that addresses the cause. Do not guess: find the cause first.
4. Turn what you learned into a rule with propose_rule, so next time this needs no AI. Its steps must be checks or show commands you actually ran on this incident, with the same arguments, before you changed anything; the rule is replayed against what you saw and must reach its cause. Use output_has (device, command, text) for anything no check covers: command is the same one you ran with show. Use {device} and other event fields where it should work elsewhere, and a fixed name where it is about one device. Every rule runs as its own Jenkins pipeline in the troubleshooting-pipelines folder, and every run of it is a build people open from the run page, so name that pipeline with pipeline: a few lowercase hyphenated words saying what it looks for, such as ospf-hello-timer-mismatch, not one list_rules already shows. Its fix becomes a pipeline too, named <pipeline>-fix unless you give fix.pipeline. Say what it runs for with trigger.tags, and where: position beginning for a cheap check that settles things before the normal rules, position end for something only worth checking once the normal rules have run out. If your fix can be repeated, include it under fix, using an action name: config_lines (device, lines), linux_change (device, argv, undo_argv) or another action. When a cause says device=X, {device} in the fix means X, so use {device} and not a fixed name when the rule can find the fault in more than one place. The fix needs a verify you actually ran and saw fail while the fault was there, with the same arguments. {alarm_device} is always the device the alarm came from and exists only in a fix (in the rule's steps, {device} already is that device): for reachability, verify from there, because a router pinging for itself proves nothing about what it forwards. A cause label looks like area.what, for example firewall.forward_blocked. Do not suggest a rule when the fault cannot be recognised by a check.
5. Tags are a fixed list, from list_tags. Only propose_tag when nothing fits. When a tag belongs on past runs, include them; search_runs finds them. Only name runs you have a real reason for: the same symptom is not enough, the cause has to have been possible for that run, at that time and for that kind of traffic. An IPv4 firewall line cannot explain an IPv6 failure.
6. When the real repair needs hands on hardware and people are being hurt now, do not leave it there. A port or link that keeps flapping is the usual case. If traffic has another way round, move it off the bad link: raise the OSPF cost on both ends of that link, for IPv4 and IPv6, with try_config and temporary true. Its verify must fail now and pass once traffic has moved, such as output_has on the routing table showing the new next hop. Then finish with outcome workaround, and say in next exactly what an engineer has to repair and what has to come back out afterwards. Say needs_person only when no stop-gap is possible.
7. Call finish exactly once, last. Say fixed only if a change of yours was kept and the cause is really gone. Say workaround if a temporary change is keeping people working. Say needs_person when it needs hands on hardware or a decision. Say nothing_wrong if it had already gone away. If a change is refused because changes are switched off, still suggest the rule.

Limits you cannot get around: R5 is the ISP's router and is out of scope. Management ports, accounts, SNMP, logging and BGP configuration are never changed. A change is at most 20 config lines. Logins and commands from the automation server are routine.

You also keep your own notes: what you concluded on earlier work and whether it held up once the person answered. The most relevant are in your brief, and recall_my_notes searches the rest. If a conclusion like the one you are reaching turned out wrong before, say why this time is different.

You are given every past run grouped by cause and the closest past cases. Read them first: if this has happened before, start from what worked and do not repeat what failed. Before running a command to learn something, check whether search_runs already has it: it looks inside every past command, output, change and decision. open_run reads any past run whole. Everything Jenkins has run is there too: search_jenkins finds builds of any job by what they printed, such as a test that started failing, a deployment or a config change around the time of the trouble, and read_build reads one whole. Some of it will be irrelevant, so use what bears on this and ignore the rest. Never invent an event, a person or a cause.{talking}"""


def _system(someone_asked: bool) -> str:
    opening, first = _FROM_A_PERSON if someone_asked else _FROM_THE_RULES
    return _SYSTEM.replace("{opening}", opening).replace("{first}", first).replace("{talking}", _TALKING if someone_asked else "")


SYSTEM = _system(False)


def needed(record: dict) -> str | None:
    """Why the AI should take this over, or None if the rules have dealt with it."""
    r, fix = record["result"], record.get("repair") or {}
    if record["event"].get("source") == "ticket" and record["rule"]["id"] == "none":
        return "A person wrote in about this."
    if record["event"].get("change_running") or fix.get("expected"):
        return None
    if engine.cleared_at_confirm(record):
        return "The person still says something is wrong, but the rule could not see it from their computer." if record["event"].get("source") == "ticket" else None
    if r["kind"] == "stop":
        return "No rule covers this kind of problem yet." if record["rule"]["id"] == "none" else "The rules ran out without finding a cause."
    if r["kind"] == "escalate":
        return "The rules ran out and asked for someone to look."
    if r["kind"] != "cause" or PERSON_ONLY.match(r.get("label") or ""):
        return None
    if fix.get("status") == "failed":
        return "The rules found the cause, but their repair did not work."
    if fix.get("in_the_way"):
        return "The rules found the cause, but their repair would alter something an earlier piece of work left in place on purpose."
    if not fix:
        return "The rules found the cause, but no rule can repair it."
    return None


def blocked(record: dict) -> str | None:
    """Why the AI may not start right now, or None."""
    if OFF_SWITCH.exists():
        return f"The AI is switched off ({OFF_SWITCH.name} exists)."
    if history.ai_sessions_lately() >= MAX_SESSIONS_PER_HOUR:
        return f"The AI has already taken over {MAX_SESSIONS_PER_HOUR} incidents in the last hour, so this one goes to a person."
    if record["event"].get("source") != "ticket" and history.ai_sessions_lately(SAME_AGAIN_WITHIN, record["event"]["device"], record["result"].get("label") or record["event"].get("event", "")):  # a person coming back about it is not the same alarm firing twice
        return "The AI took this same problem on this device less than half an hour ago, so this one goes to a person."
    return None


def _still_there(record: dict, rules) -> bool:
    """It may have cleared while waiting its turn, often because the AI just fixed the same thing from another alarm."""
    if record["event"].get("source") == "ticket":  # a person said it is broken; a rule not seeing it is why this came to the AI
        return True
    chain = [r for r in engine.chain_for(rules, record["event"]) if r.position == "main"]
    return not chain or not engine.cleared_at_confirm(engine.run(chain[0], record["event"]))


def _just_found(record: dict) -> list[dict]:
    """One fault often raises several alarms. What another AI run found minutes ago on the same kind of alarm saves working it out twice."""
    now, kind, out = record["event"]["time"], record["event"].get("event"), []
    for row in store.summaries(store.DATA_DIR):
        if row["id"] != record["id"] and row["kind"] == kind and row["agent"] == "done" and abs(now - row["time"]) < EARLIER_WITHIN:
            earlier = store.load(row["id"], store.DATA_DIR) or {}
            ai = earlier.get("agent") or {}
            if ai.get("root_cause"):
                out.append({"device": row["device"], "outcome": ai.get("outcome"), "root_cause": ai["root_cause"], "changes": [f"{c.get('status')}: {' '.join(c.get('what') or [])}" for c in ai.get("changes") or []]})
    return out[:3]


def brief(record: dict) -> str:
    """What the AI starts from: the run so far, what was found in memory, and what people have already turned down."""
    text = analyst.brief(record, record.get("context") or [], record.get("similar") or [], record.get("pattern") or {}, record.get("retrieved") or [])
    r, ev = record["result"], record["event"]
    wanted = " ".join(filter(None, [ev.get("title"), r.get("label"), r.get("text"), " ".join(tags.for_record(record))]))
    refused = review.rejected_like(wanted)
    out = [*([firstlook.text(record["first_look"]), ""] if record.get("first_look") else []), text, "", f"THIS INCIDENT IS TAGGED: {', '.join(tags.for_record(record)) or 'nothing yet'}"]
    if ev.get("ticket"):
        theirs = tickets.earlier_from(ev["ticket"])
        if theirs:
            out += ["", "WHAT THIS PERSON HAS WRITTEN IN ABOUT BEFORE"] + [f"- {line}" for line in theirs]
    mine = brain.context_for("support", wanted, 5, heading="YOUR OWN NOTES FROM EARLIER WORK, including how your conclusions turned out. Some will not bear on this one:")
    if mine:
        out += ["", mine]
    tried = record.get("tried") or []
    if tried:
        out += ["OTHER RULES THAT ALSO RAN"] + [f"- {t['rule']} ({t['position']}): {t['result'].get('text') or t['result']['kind']}" for t in tried]
    builds = jenkins_index.of_run(record)
    if builds:
        out += ["", "THIS RUN IN JENKINS. Each rule and repair ran as a build of its pipeline; read_build reads one whole, search_jenkins finds any other build."] + builds
    earlier = _just_found(record)
    if earlier:
        out += ["", "ANOTHER AI RUN JUST WORKED ON THE SAME KIND OF ALARM. Start from this: confirm it still holds and act on it rather than investigating from the beginning."]
        out += [f"- {e['device']}, finished as {e['outcome']}: {e['root_cause']}" + (f" Changes it tried: {'; '.join(e['changes'])}." if e["changes"] else "") for e in earlier]
    if refused:
        out += ["", "SUGGESTIONS A PERSON ALREADY REJECTED FOR SOMETHING LIKE THIS. Do not suggest these or anything that repeats the same mistake:"]
        out += [f"- #{r_['id']} {r_['kind']}: {r_['title']}. Their reason: {r_['reason']}" for r_ in refused]
    return scrub.text("\n".join(out))


def _save(record: dict, session: tools.Session, status: str, why: str, started: float, prompt: str, reasoning: list[str], error: str = "") -> None:
    result = session.result or {}
    record["agent"] = scrub.deep({"status": status, "why": why, "model": session.model, "started": started, "finished": time.time(), "calls": session.calls, "changes": session.changes,
                                  "proposals": session.proposals, "outcome": result.get("outcome") or "needs_person", "root_cause": result.get("root_cause") or "", "prompt": prompt, "reasoning": "\n\n".join(reasoning)[:6000], "error": error})


def _keep_what_i_did(record: dict, session: tools.Session) -> None:
    ev, result = record["event"], session.result or {}
    words = lambda status: "; ".join(f"{c.get('device')}: {' '.join(c.get('what') or [])}" for c in session.changes if c.get("status") == status)
    brain.remember("support", "did", " ".join(filter(None, [f"Run {record['id']} on {ev['device']}" + (f", ticket T-{ev['ticket']}" if ev.get("ticket") else "") + f": {ev.get('title')}.", f"I concluded: {result.get('root_cause') or 'nothing firm'}.", f"It ended as {result.get('outcome') or 'unfinished'}.",
                                                            f"Kept: {words('kept')}." if words("kept") else "No change of mine stayed.", f"Taken back out: {words('reverted')}." if words("reverted") else "", f"Left for someone else: {result.get('next')}" if result.get("next") else ""])))


def _network_now() -> dict:
    """What the sweep currently shows, for the training record."""
    if not capture.ENABLED:
        return {}
    from ts import overview  # overview imports this module, so not at the top
    now = overview.now()
    return {key: now.get(key) for key in ("broken", "groups", "swept")}


def _show_progress(record: dict, log) -> None:
    """So someone with the run open can watch it work. A failure here must not stop the work."""
    try:
        store.save(record, store.DATA_DIR)
    except OSError as exc:
        log(f"{record['id']}: could not save progress: {exc}")


def take_over(record: dict, rules, log=print, model: str | None = None, root: Path = DEFAULT_RULES_DIR) -> None:
    """Work the incident until it is finished or the time runs out. The outcome goes on the record."""
    started, why = time.time(), (record.get("agent") or {}).get("why") or needed(record) or "Asked to look at this."
    event = record["event"]
    model = model or aiconfig.choice("support").model
    someone_asked, limit = bool(event.get("ticket")), FOLLOW_UP_SECONDS if event.get("follow_up") else TICKET_SECONDS if event.get("ticket") else MAX_SECONDS
    system = _system(someone_asked)
    if not _still_there(record, rules):
        record["agent"] = {"status": "not_needed", "why": why, "started": started, "finished": time.time(), "calls": [], "changes": [], "proposals": [], "outcome": "nothing_wrong",
                           "note": "It had already cleared by the time the AI's turn came."}
        return
    history.ai_session_started(record["id"], event["device"], record["result"].get("label") or event.get("event", ""))
    session = tools.Session(record, rules, root=root, model=model)
    record["first_look"] = firstlook.take(event, record.get("context"), record["id"])
    prompt = brief(record)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    definitions, reasoning, nudges, error = tools.definitions(someone_asked), [], 0, ""
    record["agent"] = {"status": "working", "why": why, "model": model, "started": started, "calls": session.calls, "changes": session.changes, "proposals": session.proposals}
    episode = capture.start("agent", record["id"], model, {"prompt_cut": capture.was_cut(prompt), "why": why, "max_reads": tools.MAX_READS, "max_acts": tools.MAX_ACTS})
    capture.inputs(capture.sections(system, prompt, definitions, record))
    capture.state("handover", _network_now())
    llm.start_conversation()
    try:
        while session.result is None and time.time() - started - session.waited < limit:
            reply = llm.chat(messages, definitions, model=model, role="support")
            if reply["reasoning"]:
                reasoning.append(reply["reasoning"][:1500])
            messages.append(reply["message"])
            if not reply["calls"]:
                nudges += 1
                if nudges > 2:
                    error = "The AI stopped calling tools without finishing."
                    break
                messages.append({"role": "user", "content": "Continue by calling a tool. When you are done, call finish."})
                continue
            for item in reply["calls"]:
                changes_before = len(session.changes)
                result = tools.call(session, item["name"], item["args"])
                given_back = json.dumps(result)[:40000 if item["name"] == "open_run" else 12000]
                messages.append({"role": "tool", "tool_call_id": item["id"], "content": given_back})
                capture.step(session.calls[-1], given_back, session.changes[-1] if len(session.changes) > changes_before else None)
            _show_progress(record, log)
    except llm.LLMError as exc:
        error = f"The model stopped answering: {exc}"
        log(f"{record['id']}: AI: {error}")
    llm.end_conversation()
    if session.result is None and not error:
        error = "The AI ran out of time."
    _save(record, session, "done" if session.result else "failed", why, started, prompt, reasoning, error)
    result = session.result or {}
    _keep_what_i_did(record, session)
    capture.state("final", _network_now())
    capture.close(episode, record["agent"]["status"], {"outcome": record["agent"]["outcome"], "root_cause": record["agent"]["root_cause"], "error": error, "changes": [c.get("status") for c in session.changes], "proposals": session.proposals, "summary": result.get("summary", "")}, messages)
    for tag in result.get("tags") or []:
        if tag in tags.known():
            history.tag_run(record["id"], tag, "ai")
    record["tags"] = history.tags_of(record["id"])
    if result:
        known = {item.get("ref") for item in [*(record.get("similar") or []), *(record.get("retrieved") or [])]}
        record["analysis"] = scrub.deep({"summary": result.get("summary", ""), "related": result.get("related") or [], "assessment": result.get("assessment", "unknown"), "why": result.get("why", ""), "next": result.get("next", ""),
                                         "used": [ref for ref in result.get("used") or [] if ref in known], "model": model, "seconds": round(time.time() - started, 1), "reasoning": "\n\n".join(reasoning)[:4000],
                                         "started": started, "finished": time.time(), "prompt": prompt,
                                         "given": {"checks": len(record["steps"]), "repair": bool(record.get("repair")), "similar": len(record.get("similar") or []), "memory": len(record.get("retrieved") or []), "live": len(record.get("context") or [])}})
