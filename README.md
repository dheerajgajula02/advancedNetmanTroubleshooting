# advancedNetman troubleshooting

Watches the network, works out what went wrong, puts it back when that is safe, and
emails the whole story.

```
trap or sweep ─> wait ─> beginning rules ─> main rule ─> end rules ─> repair ─> context + history ─┐
                                                                                                  │
   email + UI replay <── the AI takes over, only if the rules could not resolve it <──────────────┘
```

Traps cover what devices report (link down, OSPF, VRRP, restarts, config saves). A sweep
once a minute covers what they do not: BGP, RIP, key routes, internet reachability, DHCP,
NAT, packet loss on every routed link, and, every three minutes, each device's config
against its known-good copy. Each sweep finding fires once, when good turns bad.

## Scenarios

Thirteen diagnose rules in `rules/diagnose/`: port down, OSPF neighbor lost, VRRP gateway
change, config change, device restart or unreachable, ISP BGP, RIP, DHCP, route missing,
internet unreachable, a link dropping packets, a lost NAT rule, and border router config drift.

A rule is data. It can only combine the checks registered in `ts/blocks/`, so a rule
never runs arbitrary code. `python -m ts rule show ID` prints one as a tree.

## Rules: trigger, position, tags, action

Every rule says what it runs for, where it sits, and what state it is in:

```yaml
match: {tags: [service/internet]}   # or event: ospf.neighbor_down, role, platform
position: end                       # beginning | main | end
mode: shadow                        # proposed | shadow | active | retired
```

For one event the order is beginning rules, then one main rule, then end rules. The first
to reach a cause ends it. A rule in `shadow` runs and is recorded but decides nothing.

Tags are a fixed list, six areas of four (`ts/tags.py`): routing, switching, link, device,
service, config. Written `area/name`, and naming the area covers all four. New ones come
in only through the review page.

`rules/fix/` rules repair a named cause. Two generic actions let a rule change something
no built-in action covers: `config_lines` and `linux_change`. `output_has` lets a rule read
anything an allowlisted show command prints.

## When the rules run out

The AI takes over when the rules stop without a cause, escalate, find a cause they cannot
repair, or their repair fails (`ts/agent.py`). Physical and ISP faults go to a person.

It has one tool per check and action, plus `show`, `try_config`, `try_command`, searches
over memory and past runs, and `propose_rule`, `propose_tag`, `propose_backdate`, `finish`
(`ts/tools.py`, also served over MCP by `python -m ts mcp`). Every call is written to the
run and to the `audit` table.

A change is a transaction. It must name a check that fails now. It takes the same lock as
Jenkins, compares the whole network before and after, and is taken back out unless the
check passes, something that was wrong got better, and nothing new broke. `ts/safety.py`
is the one gate for what may be read or changed: never R5, management ports, accounts,
SNMP, logging or BGP config, at most 20 lines.

Limits: 8 takeovers an hour, the same problem on the same device once per 30 minutes,
420 s and 30 reads per run, 3 changes per run, 6 changes an hour. Reads are counted apart
from changes and suggestions, so looking around can never use up the chance to act.

Switches, each a file you create to turn something off:

| file | stops |
|---|---|
| `data/converge.off` | every repair, by rules or AI |
| `data/ai.off` | the AI taking over at all |
| `data/ai-changes.off` | AI changes; it can still investigate and suggest |

## Review

Nothing the AI suggests takes effect until someone accepts it on the review page. A rule
carries its proof: it was replayed against what the AI really saw on that run and must
reach its cause. Accept as "watch first" (shadow) or "go live" (active); rules load on
every event, so it applies at once. A rejection needs a reason. It is stored, searched
with BM25, and put in front of the AI whenever it works on something similar, and a
suggestion that means the same thing is refused outright.

## Putting things back

The aim is to converge: even a change made by hand goes back to the known-good config.

Known-good is the newest deployed golden snapshot in `/opt/netman/live/conf/golden/snapshots`.
Devices with no golden, or only an old one (r6, r7, r8, s5), use a copy saved in
`data/config-baseline/<device>.approved`, refreshed only after a deployment while the network is healthy.

Fix rules live in `rules/fix/`. A fix runs only when all of these hold:

- `data/converge.off` does not exist. **Create that file to switch every repair off.**
- It is not R5, and it does not touch management, accounts, SNMP or logging.
- This cause has been fixed on this device fewer than 3 times in the last hour.
  After that it stops and emails, rather than fighting whoever keeps doing it.
- No network-changing job is running, none finished in the last 10 minutes, and the change lock is free.
- The change comes to 40 config lines or fewer.

It holds the same lock as Jenkins jobs, validates on EOS in a throwaway config session,
then commits and saves, and checks the result for up to 40 s. If the check fails it undoes
its change. Changes made by automation are never reverted. Physical faults, a dead device
and anything on the ISP's side go to a person by email.

## What each run learns from

`ts/sources/` gathers what else was going on: Jenkins builds (read from disk), GitHub
pushes, Prometheus (turned into sentences, including a 7-day look back), the data lake,
and Grafana alerts. `data/history.sqlite` holds every run and context item with full-text
search. `ts/history.py` reads the repeats:

| verdict | meaning |
|---|---|
| `scheduled` | at least 3 days, mostly at the same hour |
| `tampering` | the same hand change twice in 24 h or 3 times in 7 days |
| `bad_hardware` | a port failing 3 times in 24 h or 5 times in 7 days with no config change |
| `repeating` | 3 or more in 7 days, none of the above |

`ts/memory.py` keeps a long memory. Every 10 minutes the watcher reads all five sources
into the store, whether or not anything has gone wrong, and the first time it looks back
two weeks. Each run then searches past runs and those records with BM25 keyword search,
keeps the ones that name its devices, ports or addresses, collapses repeats into one
record with a count, and caps each source so none can fill the list.

`ts/rag.py` is what the AI remembers about its own past work, and `ts/cards.py` decides what
each run is given. A finished run gets a note from the model, and is then cut into pieces:
what was seen, the cause, what worked, what failed, the lesson, every command with what it
printed, every change, and what people decided. Each piece starts with a line naming its
run, device, date, outcome and cause, so it still means something alone. The rule's checks
are kept for word search only, because every run repeats them.

A search runs two ways at once and blends the two rankings: words (SQLite FTS5) find exact
things like `netem` or `eth3`, and meaning (`ts/embed.py`, on the GPU at indiaserver) finds
"I can't get online" for "cannot reach the internet". Sharing a device or port name adds a
little. Runs score from their best pieces, and a run that fixed something counts for more
than one that found nothing. Repeats of the same thing on the same place fold into one with
a count, and one cause can take only two slots. Nothing dated after the incident is ever
visible, so replaying an old incident cannot peek at the answer.

Every AI run is given three things, in this order:

1. Every past run grouped by cause: how many, where, when, whether it comes at the same hour
   each day, what worked, and the latest run's id. The size is capped, and when it runs out
   the oldest groups shrink to a name and a count, so no run is ever left out.
2. What people have accepted.
3. The closest five cases, told the way an engineer wants them: what was seen, the cause, how
   that was shown, what worked and what did not, and the passage that matched.

`search_runs` looks inside every past command, output, change and decision, returns the
passage that matched, and says so plainly when nothing does. `open_run` reads one whole.

Memory grows by itself. Each finished run adds its pieces at once and needs no GPU to be
found by words. The watcher brings meaning up to date every 10 minutes, so if the GPU machine
was away it catches up by itself. Search keeps working on words alone in the meantime. The
**Memory** light on the console shows how much is held and whether meaning is reachable.
`deploy/embed/install-on-gpu.sh` sets up the permanent forwarder on indiaserver.

Every turn of one AI investigation sends the same session id, because the model provider only
reuses what it has already read when it does. In a test that took cache hits from 4 turns in
12 to all 12, and the first live run after it hit on all 14 of its later turns.

The model (`deepseek-v4.1-flash`, chosen over `muse-spark-1.3-contributor` in a test on
known answers) writes the read of a run when there is a report to write and the AI did
not already take it over. It gets the checks, the conclusion, the repair, and what was
found, and it says which older records it relied on. That read never changes anything.
The page marks everything the AI did in violet and shows the exact text it was given.

## Training data

Everything that goes into or comes out of the model is kept for fine-tuning, in the same
layout as the hackathon's training database: raw facts, with any training format derived
later (`ts/capture.py`, `ts/dataset.py`). The recorder sits inside `ts/llm.py`, so no model
call can avoid it.

For each session it keeps what the model was given section by section (RAG results with
their scores included), the whole conversation, the full thinking, the reason the model
gave for each command, full outputs, the network around each change, what people accepted
or rejected and why, and for drills what was really broken. The AI cannot read any of it.
Secrets are removed before storing. If the database is away, rows wait in
`data/capture-waiting.jsonl` and go in later, and the work is never held up.

```
docker compose -f deploy/training-db/compose.yaml up -d   # the database, on this machine only
python -m ts dataset snapshot                             # one complete copy, in data/snapshots/
python -m ts dataset export OUT [--all]                   # train.jsonl, test.jsonl, tools.json, conversations.jsonl
python -m ts dataset backfill                             # runs from before this existed
python -m ts dataset stats
```

The Data page shows all of it. Runs from before this existed are marked as older: their
thinking was shortened when saved and they have no stated reasons.

Every agent is recorded, and each can have its own dataset and its own fine-tune, so that
over time none of them has to depend on a provider: the support agent, the service desk,
the notes writer, and each person at a computer (or all of them together). The Data page
shows how much has been gathered for each. Agents that answer in one go, such as the
desk sorting a ticket, become one example per conversation, written as a single call so
the same trainer and scoring work for them. Their written answers are kept whole. A
dataset needs at least 8 training examples before it can be trained.

## What starts a run

Traps, tickets from the service desk, monitors you accepted (see Monitors below), and a
heartbeat that checks everything every 20 minutes.

The service desk (`ts/desk.py`, port 8771) is its own site over the same database. People
write in their own words, and `ts/tickets.py` knows who sits at which computer, so the
investigation starts there. Tickets are worked one at a time and go straight to the agent,
with no rules first. The agent can ask the person to try things on the ticket
(`ask_customer`), and the time spent waiting for an answer does not count against its limit.

A ticket is not solved until the person says so. When the agent is done, the desk sends one
closing message with its `send_closing_message` tool: "we made some changes, can you see it
working?". The person's next reply is the last message on that conversation and nobody
answers it, so there are no thank-yous going back and forth. If what they tried works, the
ticket settles, and something they could not try from their desk does not count against it.
If something they tried fails, a shorter follow-up begins from everything said so far, and
it ends with its own closing message. After three goes an engineer takes it, and with no
reply for a day the desk stops waiting.

When the real repair needs a person, such as a port that keeps flapping, the agent can keep
people working with a temporary change: `try_config` with `temporary` true, then `finish`
with outcome `workaround`. The change is held (`ts/configs.py`) so drift checks and
known-good leave it alone, and the ticket stays open as "Working for now" until an engineer
closes it, which lets known-good put it back.

Before anything is changed, by a rule or by the agent, `ts/firstlook.py` answers the two
questions an engineer asks first: was it working before, and has anything changed. The
answers stay on the run. Every difference from known-good is one of two kinds, and they are
handled in opposite ways:

- Nothing on record explains it. That is drift, and it goes back to known-good.
- Earlier work left it there on purpose, such as an OSPF cost stop-gap for a failing cable.
  The agent has to reason about it instead of restoring it. If it does not bear on the new
  trouble, it stays. If it does and its reason is gone, `retire_earlier_change` takes all of
  it out, and puts it back with its reasons if that does not hold up. If its reason is still
  there and one more change can do both jobs, that change goes on top and is temporary too.
  If nothing can do both, nothing changes and the run ends by naming the real problem.

`try_config` refuses to alter something left in place on purpose without that reasoning, and
a rule will not overwrite it at all: it hands the run to the agent.

The people at the computers are a separate layer (`drills/people.py`). Each is an ordinary
employee who can open a website, run diagnostic commands on their own computer, and write on
their ticket. Nothing either side is told mentions the other being automated. The only thing
that passes between them is the conversation on the ticket. People can look but not change
anything.

Every AI agent has its own memory (`ts/brain.py`), found by words and by meaning, and can only
reach its own. Each person's working day (`drills/office.py`) really uses the network from
their computer, with no model involved, and a long run of the same result is remembered as
one stretch. That is how they can answer "did this work before?". What they did, said and
heard goes in as well, and `think_back` searches it. The support agent keeps what it
concluded and whether that held up once the person answered, and sees what this person has
written in about before. The service desk and the notes writer keep their own too. The
people's memories are in a separate file the support side never opens.

The Memory page on the service desk shows every agent's memory, and lets you search it the way
the agent would, add to it, rewrite it and delete from it.

The Scenarios page (`drills/scenarios.py`) breaks the network for real, then either files the
ticket that person would write or lets them find the problem at their computer and write in.

The console's AI models page chooses the endpoint, model and thinking effort for each job and
for each person's computer, takes any OpenAI-compatible endpoint, and shows what has been
used (`ts/aiconfig.py`). Saved keys are never sent back to the page.

## Monitors

A monitor is a condition on numbers the network already reports. When the condition is
met, it starts a troubleshooting run. An example: "carrier transitions on s1 Ethernet4 in
the last 10 minutes ≥ 2 and linkDown traps from s1 in the last 10 minutes ≥ 1". Monitors
live in `ts/monitors.py`. Each one has:

- **Variables (1 to 5).** A variable reads either a Prometheus metric (gNMI or SNMP) through
  a fixed function such as `increase`, `rate`, `changes` or `max_over` over a window, or a
  count of traps or syslog lines from the data lake.
- **Conditions (1 to 5).** Each compares one variable with a number. A monitor needs all of
  them, or any one of them.
- **Hold and quiet time.** The hold is how long the conditions must stay true. The quiet
  time is how long it waits before starting another run.

Every variable is checked when it is written, against Prometheus and the data lake, so a
monitor cannot read a metric or port that does not exist. The watcher looks at every active
monitor every 30 seconds. While a job that changes the network is running, or within five
minutes of one ending, a monitor that would fire is held back and noted instead, so the
nightly tests never start runs.

**Suggestions.** After a run finds a cause, `ts/monitor_ai.py` asks the model chosen for
"Monitor suggestions" on the AI models page whether a monitor would have caught it sooner.
The model never writes PromQL or guesses names. It works through tools:

1. `list_variables` shows what a device reports.
2. `read_variable` shows a variable's values at the incident, on a normal day and week, and
   while tests ran.
3. `test_monitor` replays a monitor over the past seven days.
4. `propose_monitor` accepts only a monitor that was tested and passed: it caught this
   incident and started at most two other runs in the week.

A suggestion waits in Review with the model's reasons, the replay results and a graph of
each variable around the incident. Nothing watches until you accept it. At most four runs
an hour are looked at, and runs during tests are skipped.

**Where they show.** The Monitors page lists each monitor, its state and current values,
and graphs of the last 6 hours, and lets you pause, resume or retire it. The page draws
those graphs itself, because each embedded Grafana panel loads a whole Grafana app and
stays blank for seconds. The Dashboard tab is the
whole Grafana dashboard "Troubleshooting monitors" (`ts/grafana_monitors.py`), which is
rebuilt whenever a monitor is accepted, paused, resumed or retired. The graphs read what
the watcher itself exports at `http://10.0.0.10:9810/metrics` (`ts_monitor_*`), which the
Prometheus job `ts_monitors` scrapes. Adding monitors needs no change on the Prometheus
machine.

## ServiceNow and Salesforce

The desk connects to the ticketing system a company already runs (`ts/connectors/`), both ways:

- **Their tickets come in.** A new incident in ServiceNow with category Network, or a new case in
  Salesforce from a known contact, becomes a desk ticket and is worked like any other. What the
  person writes there later is added to our ticket. Our replies go back as comments, our internal
  notes as work notes, and our status follows into theirs: In Progress, On Hold (awaiting caller),
  Resolved with a close code read from the instance.
- **Our finds go out.** When a run the watcher started on its own is worth an email, the same story
  opens an incident there. It is resolved at once when the run fixed it, and left open for a person
  when it did not.

A connector is on when its login file exists: `secrets/servicenow.env` (`SN_INSTANCE`, `SN_USER`,
`SN_PASSWORD`, Table API with basic auth) or `secrets/salesforce.env` (`SF_DOMAIN`, `SF_CLIENT_ID`,
`SF_CLIENT_SECRET`, an External Client App with the client-credentials flow).
`sh deploy/connectors/set-login.sh servicenow` writes the file without echoing the password. The
watcher syncs every 15 seconds. Only tickets made after a connector is first switched on come in,
and our own comments are recognised when they come back, so nothing is imported or echoed twice.

```
python -m ts connector status                 # what is connected and what was synced
python -m ts connector setup servicenow       # add the desk's people as callers there
python -m ts connector file servicenow --person priya "Internet not working" "Nothing loads since lunch."
python -m ts connector sync                   # one pass now
```

`tests/test_connectors.py` covers both directions against an in-memory system and checks the
requests sent to ServiceNow's and Salesforce's APIs.

## Jenkins

Every rule and every repair is a Jenkins pipeline in the folder `troubleshooting-pipelines`
(https://jenkins.dheerajgajula.com/job/troubleshooting-pipelines/), and every time the
watcher runs one it is a build of that pipeline. The build shows each step as a stage,
with the question, the check, its answer, what was seen and the evidence. A repair's build
has the stages "Safe to change?", the change, "Did it work?", and then "Keep it as the new
starting point" or "Undo".

**How a build runs.** The troubleshooter still decides which rules apply. For each one it
starts the rule's pipeline (`ts/jenkins_runs.py`) and serves that build on 127.0.0.1, with
a one-time key. Each stage asks the troubleshooter, with curl, to run its step. The check
runs here, with the lab access, change lock and scrubbing the troubleshooter already has.
The pipeline holds the rule's routing table and picks the next stage itself. If the
troubleshooter would go somewhere else, the build stops: the pipeline is older than its rule.

**When Jenkins cannot.** If every executor is busy, Jenkins cannot be reached, or a build
has not started within a minute, the rule runs here instead. The run page says so and why.
If a repair's build stops after the change, the change is undone here and the lock let go,
whether or not the build said so. A build that says nothing for three minutes while
holding the lock is treated the same way. A repair is never run twice. `TS_JENKINS=off` in
the watcher's environment turns all of this off.

**Keeping the pipelines in step** (`ts/pipelines.py`). The pipelines are written from the
rules, so the rule file stays the one place a rule is edited. Each pipeline carries a
fingerprint of what it was written from. The watcher compares the rules with the last sync
every minute and checks every pipeline in Jenkins once an hour. Accepting a rule, or moving
one to another mode, from the Review or Rules page, the CLI or anywhere else, updates its
pipeline straight away. A retired rule's pipeline is disabled, never deleted, so its
builds stay. A proposed rule has no pipeline until it is accepted.

**Names.** A rule's pipeline is named by its `pipeline` field, or by its id. When the AI
suggests a rule it must name the pipeline: a few lowercase hyphenated words saying what it
looks for. Its fix gets `<name>-fix` unless the AI names that too. Two rules may not share
a name; the linter refuses it. The Review card shows the names before you accept.

**Links.** Each run page has a "Ran in Jenkins" panel linking every build of that run: the
rules before and after the main one, the main rule and the repair. The Rules page links
each rule's pipeline.

**Everything Jenkins has run, for the AI** (`ts/jenkins_index.py`). Every 10 minutes the
watcher indexes each new build of every job of this network, in `data/jenkins.sqlite`. That
covers tests, deployments, config changes, password rotation, housekeeping, and these
pipelines. It is kept apart from the memory a run's brief is drawn from, so once-a-minute
builds never crowd it. A job that runs dozens of times a day keeps only a line for each build that
passed. The AI has `search_jenkins` (find builds by what they printed, by job and by result)
and `read_build` (one build whole, a part at a time, read from disk). Its brief lists this
run's own builds. Consoles are scrubbed like everything else, and anything that looks like
a password, token, key or SNMP community is replaced with `[hidden]` before it is stored or
shown. metaGlobalHack and the other jobs that are not this network's are never read.

```sh
.venv/bin/python -m ts jenkins sync      # create or rewrite the pipelines now
.venv/bin/python -m ts jenkins status    # every pipeline and its rule
.venv/bin/python -m ts jenkins run RUN   # replay a stored run's rule as a build; changes nothing
```

## Collect and train

Collect runs campaigns (`drills/campaign.py`): a list of situations, a number of rounds, one
at a time. Only the drills side knows they are staged. What is kept is the ordinary ticket
and run.

Data builds a dataset, a fixed copy of the examples. Examples hold the incident, what the
rules found and the calls so far; the rest of a run's history is left out because a small
model cannot read that much.

Train (`ts/training.py`, `deploy/trainer/`) rents one GPU on Vast, trains an adapter, scores
the model before and after on held-out decisions, copies the model to `data/training/`, and
deletes the GPU. You set a spending limit. The job stops itself before the limit and saves
what it has. Three things stand between a failure and a forgotten machine: the worker deletes
the GPU however the job ends, the console sweeps for machines nobody is looking after, and
the machine powers itself off if nothing touches it for 40 minutes.

## Layout

| path | what |
|---|---|
| `ts/intake.py`, `ts/sweep.py` | events from traps, and from looking |
| `ts/engine.py`, `ts/blocks/` | run a rule; the checks and actions |
| `ts/fixer.py`, `ts/configs.py` | repair guards; config diff and repair lines |
| `ts/tags.py`, `ts/rulebook.py`, `ts/review.py` | the tag list; installing rules; the review queue |
| `ts/agent.py`, `ts/tools.py`, `ts/safety.py`, `ts/mcp.py` | the AI's turn; its tools; what it may touch; MCP |
| `ts/pipeline.py` | everything after the conclusion |
| `ts/sources/`, `ts/history.py`, `ts/memory.py` | context, the store, finding things again |
| `ts/analyst.py`, `ts/llm.py` | the model |
| `ts/report.py` | the email, through the local mailer on port 8025 |
| `ts/lab.py` | how devices are reached; the only code that can change them |
| `ts/scrub.py` | keeps the simulator's names out of anything the debugging side stores |
| `drills/run.py`, `drills/scenarios.py`, `drills/campaign.py` | staged faults and campaigns. The troubleshooter never imports them |
| `ts/rag.py`, `ts/cards.py`, `ts/embed.py` | how past runs are stored and found, and what each run is told about them |
| `ts/tickets.py`, `ts/desk.py`, `ts/static/desk.html` | the service desk |
| `ts/firstlook.py` | was it working before, has anything changed, and why it differs |
| `ts/brain.py` | each agent's own memory and how it is found again |
| `drills/people.py`, `drills/office.py` | the people at the computers, their working day, and what they notice and answer |
| `ts/aiconfig.py` | which model each job uses, endpoints, keys and usage |
| `ts/monitors.py`, `ts/monitor_ai.py`, `ts/grafana_monitors.py`, `ts/monitor_page.py` | monitors: variables, watching and replaying; the AI's suggestions; the Grafana dashboard; the page |
| `ts/collect.py`, `ts/training.py`, `ts/trainspec.py`, `deploy/trainer/` | collecting and fine-tuning |
| `ts/pipelines.py`, `ts/jenkins_runs.py`, `ts/jenkins_api.py`, `ts/jenkins_index.py` | rules as Jenkins pipelines; serving their builds; the Jenkins API; every build indexed for the AI |
| `deploy/backup/` | the nightly backup to pve2 and its weekly restore check |
| `deploy/health/` | the five-minute health check and its emails |
| `ts/web.py`, `ts/overview.py`, `ts/store.py`, `ts/static/` | the UI and the API behind it: `app.js` and `app.css` draw the pages, `index.html` and `base.css` are this service's own frame |

## Where the pages are

The pages are drawn in the Netman GUI's look, and they live in its sidebar under
Troubleshooting (http://10.9.8.182:8050/troubleshooting). Each sidebar entry opens a
group whose pages are tabs across the top: Incidents (Now, History, each run's replay),
Rules & review (Review, Rules, Tags), Monitors (Monitors, Dashboard) and AI agents
(Models, Collect, Data, Train). The
Netman GUI forwards `/troubleshooting/app.js`, `app.css` and `/troubleshooting/api/...`
here, so a change to these pages needs no Netman release; see `lab4_gui/README.md` there.
This service's own page on port 8770 shows the same pages in a copy of Netman's frame
(`base.css`, copied from `lab4_gui/pages.py`; keep the two in step) and still works when
the Netman GUI is down. The service desk on port 8771 stays its own app, in the same look.

Publicly, everything is at https://nmas.shiplabs.net behind one Netman sign-in: these
pages under `/troubleshooting`, the service desk under `/desk/`, NetBox under `/ipam/`
(a second NetBox front end, `netbox-public`). Links between the desk and these pages use
the ports on the LAN and those paths on the public address (no port). The nginx site,
the NetBox front end and its env-file writer are in `deploy/nmas/`.

## Keeping it running

The watcher, the replay page and the desk run as services (`deploy/install.sh`, run once
with sudo), so they come back after a reboot.

**Backups.** Every night at about 02:20, `deploy/backup/boulder_backup.py run` copies boulder's
own state to pve2: Netman's accounts and state, this project's `data/` and `secrets/`, the
NetBox, Guacamole and training databases, and Jenkins's configuration (jobs, credentials,
users; no build history). SQLite files are copied safely while they are in use, as their
owner. The copies go to `boulderbak@pve2:/srv/boulder-backup/data/<date>/`; that account
can only rsync into that folder. pve2 keeps 30 days, never fewer than the 7 newest. Every
Sunday at 05:40 `boulder_backup.py check` fetches the newest copy back, restores each
database into a throwaway server with no network, and compares every table's rows. The
data lake on pve is not included. Install with `sudo deploy/backup/install.sh` on boulder,
then `deploy/backup/pve2-setup.sh '<printed key>'` as root on pve2. The result of the last
backup and check is in `/var/lib/boulder-backup/status.json`.

**Health check.** Every five minutes `deploy/health/health_check.py` looks at the services,
containers and lab devices, every page and tool, the nmas sign-in gate, the data lake's
newest event, the watcher, the embedding server, the backups, the root disk and the Jenkins
jobs that matter. A problem seen twice in a row is emailed through the local mailer, so is
its recovery, and anything still failing is repeated every 12 hours. Once an hour, if
`~/.config/nmas/health-check.json` holds a monitor account, it also signs in on the public
address and opens each tool with its styles. Results: `data/health.json`; to look by hand:
`.venv/bin/python deploy/health/health_check.py --print`. Install with
`sudo deploy/health/install.sh`.

## Commands

```sh
.venv/bin/python -m ts watch [--sweep 1200] [--catch-up 10]  # log: data/watch.log
.venv/bin/python -m ts serve --host 10.9.8.182 --port 8770
.venv/bin/python -m ts desk --host 10.9.8.182 --port 8771    # the service desk
.venv/bin/python -m ts sweep                                 # one look, opens nothing
.venv/bin/python -m ts memory stats | catch-up | search WORDS...
.venv/bin/python -m ts rule lint | list | show ID | mode ID MODE
.venv/bin/python -m ts review list | accept N | reject N REASON
.venv/bin/python -m ts tags
.venv/bin/python -m ts ai run ID                             # give a stored run to the AI
.venv/bin/python -m ts mcp [--run ID]                        # the same tools over MCP
.venv/bin/python -m ts monitor list | test FILE [--around 'YYYY-MM-DD HH:MM'] | add FILE
.venv/bin/python -m ts monitor status ID active|paused|retired | suggest RUN | grafana
.venv/bin/python -m pytest -q
.venv/bin/python drills/run.py list | NAME [NAME ...]        # results: data/drills/
```

Secrets live in `secrets/`, which git ignores: `opencode.env` (the model), `grafana.env`, `embed.env` (the embedding model) and `finetune/` (GPU accounts).

## Not yet

- The desk and console have no login: people pick their name from a list, and anyone who can reach the console can change models and endpoints.
- Fine-tuning rents from Vast only. The hackathon's RunPod, Brev and Fireworks routes and its teacher step were not brought over.
- RIP links are not probed for packet loss, only OSPF links.
