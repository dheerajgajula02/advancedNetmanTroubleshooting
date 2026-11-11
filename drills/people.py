"""The people at the computers.

Each one is an ordinary employee with a computer and a way to write to the service desk.
This is its own layer, apart from the support side: the only thing that passes between
them is the conversation on the ticket, as with anyone else who writes in. Nothing the
support side reads says who is writing, and nothing a person is told says who is answering.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

from drills import office
from drills.run import PREFIX, sh
from ts import brain, capture, llm, safety, scrub, tickets
from ts.sweep import INTERNET_V4, INTERNET_V6

STATE = Path(__file__).resolve().parent.parent / "data" / "drills" / "people.json"
DIARY = Path(__file__).resolve().parent.parent / "data" / "drills" / "people-diary.jsonl"  # what each person did at their computer, for whoever runs the desk
SETTLE = 15            # seconds for trouble to reach someone before they sit down
DECIDE_BY = 240        # seconds into a sitting when they stop trying things and send what they have
LONGEST_SITTING = 420  # the most one sitting can last. Past DECIDE_BY so that a slow model can still get their answer out
SITE_PINGS = 30        # about 15 seconds of real traffic for each page they try
MOST_TRIES = 6         # things they try before they stop and do something about it
READS = {"ping", "traceroute", "nslookup", "curl", "wget", "hostname", "arp", "route", "netstat"}
_SHELL = re.compile(r"[;&$`<>|\n\r\\]")
_OUT_OF_HOURS = 3
MOST_THINKING = 3     # goes at trying to remember something, in one sitting

_WHO = """You are {name}. You work in {team} at your company, in {where}. You are at your work computer. Your usual work online is {uses}.

You are an ordinary employee, not a network person. You do not know how the company's network is built and you do not guess at technical causes. You know only what you see on your own computer. You have a job to get on with, and you write to the IT service desk the way people do: short, plain and in your own words.

Your tools are your computer. open_website tries a website in your browser. run_command types a command into the terminal on your computer and shows what it printed: use it when the service desk asks you to, typing exactly what they asked for. Never say something happened on your computer unless you have actually seen it.

You remember your own working life, and only that. think_back searches your memory: use it when you need to know what happened before, such as whether something was working earlier, what you tried, or what the service desk said last time. Only say you remember what you actually remember. If you cannot remember, say so."""

_NOTICE = """It is {when} and you have sat down to do your usual online work. Try it. If things are working as they should, call all_fine. If something is wrong, try again once or twice to be sure, then use raise_ticket to tell the service desk what you are seeing and how it is affecting your work, in two to four plain sentences. They already know who you are and where you sit. No greeting, no thanks, no sign-off.

{memory}"""

_ANSWER = """You wrote to the IT service desk about a problem. This is the conversation on your ticket so far:

{conversation}

They are waiting for you to answer their last message. Actually do what it asks, on your computer. If they ask whether it worked before, when it stopped or what you did, answer from what you remember. If they ask whether it is working, test it properly before you answer: try the website more than once, give it a little time, and notice anything odd, such as it being slow, stalling or dropping out. Then use reply to tell them honestly what you found, in one to three plain sentences, like a quick message. If it is working, say so. If it is not, or only partly, or something seems strange, say what you see. Answer only what they asked. If they ask for something you cannot find out from your computer or your memory, such as a web address you do not know, say you do not know rather than hunting for it. No greeting, no thanks, no sign-off, and do not offer to keep them posted.

{memory}"""

_LAST_WORD = """You wrote to the IT service desk about a problem. This is the conversation on your ticket so far:

{conversation}

Their last message closes this conversation. They have finished, and what you send next is the last message on it: nobody will answer it. Actually try what they asked, on your computer. Test it properly: try the website more than once, give it a little time, and notice anything odd, such as it being slow, stalling or dropping out. Then use last_message to say honestly what you found, in one or two plain sentences. If it is working, say so. If something you tried is not working, say what you see. If you cannot test part of it from here, just say what you could test. No thanks, no goodbye and no sign-off.

{memory}"""

_TOOLS = {
    "open_website": ("Try to open a website in your browser and see what happens.", {"site": {"type": "string"}}, ["site"]),
    "run_command": ("Type one command into the terminal on your computer and see what it prints.", {"command": {"type": "string"}}, ["command"]),
    "think_back": ("Try to remember something: whether something worked before, what you did, what happened last time and what the service desk told you.", {"about": {"type": "string"}}, ["about"]),
    "raise_ticket": ("Write to the IT service desk about what is wrong. Say what you see and how it affects your work.", {"message": {"type": "string"}}, ["message"]),
    "reply": ("Send your answer to the IT service desk on your ticket.", {"message": {"type": "string"}}, ["message"]),
    "last_message": ("Send your last message on this conversation. Nothing comes after it.", {"message": {"type": "string"}}, ["message"]),
    "all_fine": ("Everything is working, so there is nothing to report.", {}, []),
}
_lock = threading.Lock()
_busy: set[str] = set()


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"tickets": {}, "last": None}


def _save(value: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    tmp.replace(STATE)


def _remember(change) -> dict:
    with _lock:
        state = _load()
        change(state)
        _save(state)
        return state


def last() -> dict | None:
    return _load().get("last")


# --- their computer ---------------------------------------------------------

def _target(host: str) -> str:
    return INTERNET_V6 if host == "h4" else INTERNET_V4


def _try_site(host: str, target: str | None = None) -> str:
    target = target or _target(host)
    try:
        out = sh(host, "ping", *(["-6"] if ":" in target else []), "-c", str(SITE_PINGS), "-i", "0.5", "-W", "1", target, check=False)
    except subprocess.TimeoutExpired:
        return "The page never loads. The browser spins and then says the connection timed out."
    found = re.search(r"(\d+)% packet loss", out)
    lost = int(found.group(1)) if found else 100
    if lost == 100:
        return "The page never loads. The browser spins and then says the connection timed out."
    if lost >= 40:
        return "The page mostly fails. It hangs for a long time, parts of it never arrive, and reloading gives the same thing."
    if lost:
        return "The page got there in the end, but it stalled partway and hung for a few seconds before carrying on."
    return "The page opened straight away and everything on it loaded."


def _problem(command: str) -> str | None:
    if _SHELL.search(command):
        return "Please type one simple command at a time."
    try:
        argv = shlex.split(command)
    except ValueError:
        return "That command could not be understood."
    if not argv:
        return "Nothing was typed."
    return None if argv[0] in READS else safety.read_problem(command)


def _type(host: str, command: str) -> str:
    if _problem(command):
        return "This computer says you do not have permission to do that."
    argv = shlex.split(command)
    if argv[0] == "ping" and "-c" not in argv:
        argv[1:1] = ["-c", "4"]
    if argv[0] == "curl" and "-m" not in argv:
        argv[1:1] = ["-m", "10"]
    try:
        done = subprocess.run(["docker", "exec", PREFIX + host, *argv], capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return "It sat there with no answer, so you stopped it."
    return scrub.text(done.stdout + done.stderr)[:2500] or "It printed nothing."


# --- sitting down at it -----------------------------------------------------

def _diary(host: str, what: str, detail) -> None:
    try:
        with DIARY.open("a") as out:
            out.write(json.dumps({"time": time.time(), "host": host, "what": what, "detail": detail}) + "\n")
    except OSError:
        pass


def _site_address(person: dict, site: str) -> str | None:
    """Their company systems are on a company machine. Anything else is out on the internet."""
    wanted = (site or "").lower()
    return office.server_for(person["host"]) if any(name.split()[-1] in wanted for name, where in office.USES.get(person["id"], ()) if where == office.INSIDE) else None


def _keep(person: dict, kind: str, words: str) -> None:
    brain.remember(person["id"], kind, words, path=office.BRAINS)


def _do(person: dict, name: str, args: dict, thought: list) -> str:
    """One thing done at the computer. It goes into their memory the way they saw it."""
    host = person["host"]
    if name == "open_website":
        site = str(args.get("site", ""))
        seen = _try_site(host, _site_address(person, site))
        _keep(person, "did", f"You tried {site}. {seen}")
        return seen
    if name == "run_command":
        command = str(args.get("command", ""))
        seen = _type(host, command)
        _keep(person, "did", f"You typed {command} and saw: {seen[:500]}")
        return seen
    if name == "think_back":
        thought.append(1)
        if len(thought) > MOST_THINKING:
            return "You have thought about it enough and nothing more is coming back to you."
        found = brain.recall(person["id"], str(args.get("about", "")), 6, path=office.BRAINS)
        return "\n".join(brain.lines(found)) or "You cannot remember anything about that."
    return "That is not something you can do."


_ENDED_AS = {"raise_ticket": "You wrote to the service desk: {message}", "reply": "You told the service desk: {message}", "last_message": "The last thing you told the service desk about it was: {message}", "all_fine": "You checked your usual work and it was all fine."}


def _definitions(names: tuple) -> list[dict]:
    return [{"type": "function", "function": {"name": n, "description": _TOOLS[n][0], "parameters": {"type": "object", "properties": _TOOLS[n][1], "required": _TOOLS[n][2]}}} for n in names]


def _sit_down(person: dict, opening: str, names: tuple, ends_on: tuple) -> dict | None:
    """One go at the computer, until they send something or are done. What they ended on, or None."""
    host, defs = person["host"], _definitions(names)
    thought: list = []
    system = _WHO.format(name=person["name"], team=person["team"], where=person["where"], uses=", ".join(office.uses_of(person["id"])) or "ordinary web browsing")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": opening}]
    episode = capture.start("person", "", "", {"role": host, "person": person["id"]})
    capture.inputs([{"section": "Instructions", "source": "instructions", "body": system}, {"section": "Tools it can call", "source": "tools", "body": json.dumps(defs), "items": defs}, {"section": "What it sat down to", "source": "incident", "body": opening}])
    status, outcome, done = "failed", None, 0
    llm.start_conversation()
    try:
        stalls, tries, sat_down, deciding = 0, 0, time.time(), False
        stop = sat_down + LONGEST_SITTING
        while time.time() < stop:
            if not deciding and (tries >= MOST_TRIES or time.time() >= sat_down + DECIDE_BY):  # nobody keeps reloading forever: they tell someone, or get on with their day
                defs, deciding = _definitions(ends_on), True
                messages.append({"role": "user", "content": "You have tried enough and you need to get on with your work. Decide now, from what you have seen."})
            turn = llm.chat(messages, defs, role=host, max_tokens=4000, temperature=0.6, timeout=120, retries=2)
            messages.append(turn["message"])
            if not turn["calls"]:
                if deciding and len(ends_on) == 1 and (turn.get("text") or "").strip():  # what they wrote is their answer, even if it did not arrive the usual way
                    _keep(person, "said", _ENDED_AS.get(ends_on[0], "").format(message=turn["text"].strip()))
                    status, outcome = "done", {"outcome": ends_on[0]}
                    return {"tool": ends_on[0], "message": turn["text"].strip()}
                stalls += 1
                _diary(host, "said", (turn.get("text") or "")[:600])
                if stalls >= _OUT_OF_HOURS:
                    return None
                messages.append({"role": "user", "content": "Go on."})
                continue
            for call in turn["calls"]:
                args = call["args"] if isinstance(call["args"], dict) else {}
                _diary(host, call["name"], args)
                done += 1
                if call["name"] in ends_on:
                    _keep(person, "said", _ENDED_AS.get(call["name"], "").format(message=args.get("message", "")))
                    capture.step({"n": done, "tool": call["name"], "args": args, "kind": "finish", "outcome": "done", "started": time.time()}, "")
                    status, outcome = "done", {"outcome": call["name"]}
                    return {"tool": call["name"], **args}
                if deciding:  # it was not offered, so it does not happen
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": "There is no time for that now. Send your message."})
                    continue
                tries += call["name"] != "think_back"
                began = time.time()
                result = _do(person, call["name"], args, thought)
                capture.step({"n": done, "tool": call["name"], "args": args, "kind": "read", "outcome": "ok", "output": result, "started": began, "ms": int((time.time() - began) * 1000)}, result)
                _diary(host, "saw", result[:600])
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        _diary(host, "ran out of time", "")
        return None
    finally:
        llm.end_conversation()
        capture.close(episode, status, outcome, messages)


def take(number: int, person_id: str) -> None:
    """From here on this person answers on this ticket."""
    _remember(lambda s: s["tickets"].__setitem__(str(number), {"person": person_id, "answered": 0}))


def _notice(person_id: str, on_ticket, on_nothing) -> None:
    time.sleep(SETTLE)
    person, result = tickets.person(person_id), None
    try:
        memory = office.remembered(person_id, "trouble with my usual work online, what went wrong last time and what the service desk said")
        result = _sit_down(person, _NOTICE.format(when=time.strftime("%A afternoon" if time.localtime().tm_hour >= 12 else "%A morning"), memory=memory), ("open_website", "run_command", "think_back", "raise_ticket", "all_fine"), ("raise_ticket", "all_fine"))
    except Exception as exc:  # the desk must stay up whatever happens at someone's computer
        print(f"{person_id}: {type(exc).__name__}: {exc}", flush=True)
    if result and result["tool"] == "raise_ticket" and result.get("message"):
        number = tickets.create(person_id, scrub.text(result["message"]))
        on_ticket(number)
        _remember(lambda s: s.__setitem__("last", {"person": person_id, "ticket": number, "at": time.time()}))
    else:
        on_nothing()
        _remember(lambda s: s.__setitem__("last", {"person": person_id, "ticket": None, "at": time.time(), "said": "They tried their usual work and saw nothing wrong." if result else "They could not be reached."}))


def notice_soon(person_id: str, on_ticket, on_nothing) -> None:
    threading.Thread(target=_notice, args=(person_id, on_ticket, on_nothing), daemon=True).start()


def _answer(key: str, ticket: dict, note_id: int, person_id: str) -> None:
    try:
        person = tickets.person(person_id)
        said = tickets.notes_of(ticket["id"], private=False)
        conversation = "\n\n".join(f"{n['who']}: {n['body']}" for n in said)
        asked = next((n["body"] for n in reversed(said) if n.get("role") != "person"), "")
        _keep(person, "heard", f"On your ticket about {ticket['subject']}, the service desk wrote: {asked}")
        closing = any(n["id"] == note_id and n.get("closing") for n in said)  # they have finished, so this is the last thing this person sends on it
        prompt, ending = (_LAST_WORD, "last_message") if closing else (_ANSWER, "reply")
        result = _sit_down(person, prompt.format(conversation=conversation, memory=office.remembered(person_id, f"{ticket['subject']} {asked}")), ("open_website", "run_command", "think_back", ending), (ending,))
        if result and result.get("message"):
            tickets.reply(ticket["id"], person["name"], "person", scrub.text(result["message"]))
            _remember(lambda s: s["tickets"][key].__setitem__("answered", note_id))
    except Exception as exc:  # the desk must stay up whatever happens at someone's computer
        print(f"T-{key}: {type(exc).__name__}: {exc}", flush=True)
    finally:
        _busy.discard(key)


def tick() -> None:
    """Anyone who has been asked something goes and deals with it."""
    for key, who in list(_load()["tickets"].items()):
        ticket = tickets.get(int(key))
        if ticket is None or ticket["status"] in ("solved", "no_fault", "needs_agent", "closed", "workaround"):
            if ticket:
                brain.remember(who["person"], "outcome", f"Your ticket T-{key} about {ticket['subject']} ended like this: {ticket['stage'] or ticket['status'].replace('_', ' ')}.", path=office.BRAINS)
            _remember(lambda s: s["tickets"].pop(key, None))
            continue
        note_id = ticket["waiting_note"]
        if note_id and note_id != who.get("answered") and key not in _busy:
            _busy.add(key)
            threading.Thread(target=_answer, args=(key, ticket, note_id, who["person"]), daemon=True).start()
