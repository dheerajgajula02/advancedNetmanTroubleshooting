"""python -m ts ...  Inspect building blocks, lint, list, show and install rules, serve the UI."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from ts.lint import errors, lint
from ts.registry import ACTIONS, CHECKS
from ts.ruleset import DEFAULT_RULES_DIR, load_ruleset
from ts.schema import RuleLoadError, parse_rule, read_rule_file
from ts.view import fixes_for, match_text, producers_of, result_text, sort_key

MODE_LINE = re.compile(r"^mode:[ \t]*\S+[ \t]*$", re.M)


def _print_issues(issues) -> None:
    for issue in issues:
        print(f"  {issue}")


# --- blocks ---------------------------------------------------------------

def cmd_blocks(args) -> int:
    blocks = list(CHECKS.values()) + list(ACTIONS.values())
    if args.name:
        block = CHECKS.get(args.name) or ACTIONS.get(args.name)
        if block is None:
            print(f"no check or action named {args.name!r}")
            return 1
        print(f"{block.kind} {block.name}: {block.description}")
        print(f"  arguments: {', '.join(p.name + ('[]' if p.many else '') + ('?' if p.optional else '') for p in block.params) or 'none'}")
        print(f"  platforms: {', '.join(sorted(block.platforms))}")
        if block.kind == "check":
            print(f"  outcomes:  {', '.join(block.outcomes)}  (+ error, unsupported)")
            if block.produces:
                print(f"  produces:  {', '.join(block.produces)}")
        else:
            print(f"  undo:      {block.undo}")
        return 0
    for kind in ("check", "action"):
        rows = sorted((b for b in blocks if b.kind == kind), key=lambda b: b.name)
        print(f"{kind}s ({len(rows)}):")
        width = max((len(b.name) for b in rows), default=0)
        for b in rows:
            extra = ", ".join(b.outcomes) if kind == "check" else f"undo {b.undo}"
            print(f"  {b.name:<{width}}  {' '.join(b.param_names):<38} -> {extra}")
        print()
    return 0


# --- rules ----------------------------------------------------------------

def cmd_lint(args) -> int:
    rs = load_ruleset(args.rules)
    issues = lint(rs.parsed)
    for err in rs.errors:
        print(err)
    _print_issues(issues)
    n_err = len(rs.errors) + len(errors(issues))
    n_warn = len(issues) - len(errors(issues))
    print(f"{len(rs.rules)} rules, {n_err} errors, {n_warn} warnings")
    return 1 if n_err else 0


def _check_against_installed(rs, rule):
    """Issues that installing `rule` would add to the installed set."""
    before = set(lint(rs.parsed))
    after = lint([r.rule for r in rs.rules if r.id != rule.id] + [rule])
    return [i for i in after if i not in before]


def cmd_validate(args) -> int:
    rs = load_ruleset(args.rules)
    failed = False
    for file in args.files:
        try:
            _, rule = read_rule_file(Path(file))
        except (RuleLoadError, OSError) as exc:
            print(exc)
            failed = True
            continue
        new = _check_against_installed(rs, rule)
        bad = errors(new)
        print(f"{file}: {rule.table} rule {rule.id} v{rule.version}: {'INVALID' if bad else 'ok'}")
        _print_issues(new)
        failed |= bool(bad)
    return 1 if failed else 0


def _force_proposed(text: str, raw: dict) -> str:
    candidate = MODE_LINE.sub("mode: proposed", text, count=1) if MODE_LINE.search(text) \
        else text.rstrip("\n") + "\nmode: proposed\n"
    try:
        if yaml.safe_load(candidate).get("mode") == "proposed":
            return candidate
    except yaml.YAMLError:
        pass
    return yaml.safe_dump({**raw, "mode": "proposed"}, sort_keys=False)  # loses comments, still correct


def cmd_install(args) -> int:
    rs = load_ruleset(args.rules)
    if rs.errors:
        print("the installed rules have load errors; fix them first:")
        for err in rs.errors:
            print(f"  {err}")
        return 1
    path = Path(args.file)
    try:
        text = path.read_text()
        raw, rule = read_rule_file(path)
    except (RuleLoadError, OSError) as exc:
        print(exc)
        return 1
    existing = rs.get(rule.id)
    if existing and not args.replace:
        print(f"{rule.id} is already installed (v{existing.rule.version}); "
              f"bump version and use --replace to change it")
        return 1
    if existing and rule.version <= existing.rule.version:
        print(f"{rule.id}: the new version ({rule.version}) must be higher than the installed one "
              f"({existing.rule.version})")
        return 1
    if rule.mode != "proposed":
        print(f"note: mode set to proposed (the file said {rule.mode}); promoting a rule is a reviewed step")
        text = _force_proposed(text, raw)
        rule = parse_rule(yaml.safe_load(text), str(path))
    new = _check_against_installed(rs, rule)
    _print_issues(new)
    if errors(new):
        print(f"not installed: {rule.id} would add {len(errors(new))} error(s)")
        return 1
    dest = rs.root / rule.table / f"{rule.id}.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    if existing and existing.path.resolve() != dest.resolve():
        existing.path.unlink()
    verb = "replaced" if existing else "installed"
    print(f"{verb} {rule.id} v{rule.version} ({rule.table}, proposed) -> {dest}")
    return 0


def cmd_list(args) -> int:
    rs = load_ruleset(args.rules)
    rules = sorted((r for r in rs.parsed if not args.table or r.table == args.table), key=sort_key)
    if not rules:
        print("no rules")
        return 0
    rows = [(r.table, str(r.priority), r.id, r.mode, match_text(r.match), result_text(r)) for r in rules]
    head = ("TABLE", "PRIO", "ID", "MODE", "MATCH", "RESULT")
    widths = [max(len(x[i]) for x in rows + [head]) for i in range(5)]
    fmt = "{:<%d}  {:>%d}  {:<%d}  {:<%d}  {:<%d}  {}" % tuple(widths)
    print(fmt.format(*head))
    for row in rows:
        print(fmt.format(*row))
    return 0


def cmd_show(args) -> int:
    rs = load_ruleset(args.rules)
    loaded = rs.get(args.id)
    if loaded is None:
        print(f"no rule {args.id!r}")
        return 1
    rule, rules = loaded.rule, rs.parsed
    print(f"{rule.id}  v{rule.version}  {rule.table}  priority {rule.priority}  mode {rule.mode}")
    print(f"  {rule.description}")
    print(f"  match:  {match_text(rule.match)}")
    print(f"  source: {rule.source.by}" + (f" (incident {rule.source.incident})" if rule.source.incident else ""))
    print(f"  file:   {loaded.path}")
    if rule.table == "filter":
        for c in rule.when:
            print(f"  when    {c.render()}")
        print(f"  verdict {rule.verdict}")
    elif rule.table == "diagnose":
        for n, step in enumerate(rule.steps, 1):
            print(f"  {n}. {step.check.render()}" + (f"   [{step.id}]" if step.id else ""))
            width = max([len(k) for k in step.then] + [7])
            for name, outcome in step.then.items():
                line = f"       {name:<{width}} -> {outcome.render()}"
                if outcome.kind == "cause":
                    fixes = fixes_for(outcome.target, rules)
                    line += f"   (fix: {', '.join(fixes)})" if fixes else "   (no fix rule; a person decides)"
                print(line)
            print(f"       {'(other)':<{width}} -> next")
        print(f"  end: {rule.end.render()}")
    else:
        for c in rule.require:
            print(f"  require {c.render()}")
        print(f"  do      {rule.action.do.render()}")
        undo = rule.action.undo.render() if rule.action.undo else f"({rule.action.do.block.undo})"
        print(f"  undo    {undo}")
        for c in rule.verify:
            print(f"  verify  {c.render()}")
        print(f"  risk {rule.risk}; retire after {rule.retire.max_failures} failed runs "
              f"or {rule.retire.unused_days} days unused")
        producers = producers_of(rule, rules)
        print(f"  causes come from: {', '.join(producers) if producers else 'no diagnose rule'}")
    issues = [i for i in lint(rules) if i.rule_id == rule.id]
    if issues:
        print("  issues:")
        _print_issues(issues)
    return 0


# --- incidents ------------------------------------------------------------

CONFIRM_DELAY = 120  # seconds after the trap; the traffic window (checks.TRAFFIC_WINDOW) ends at 90 s


def _run_event(rules, event, rerun=False, root=DEFAULT_RULES_DIR, hand_over=None) -> str:
    """Investigate one event. With hand_over, a run for the AI is left queued and passed to it."""
    from ts import engine, pipeline, store
    from ts.vocab import FORBIDDEN_DEVICES
    label = f"{event['trap_time']}  {event['device']} {event.get('subject') or event.get('interface')}"
    if event.get("device") in FORBIDDEN_DEVICES:
        return f"{label}: not ours to look at"
    if store.exists(engine.incident_id(event)) and not rerun:
        return f"{label}: already investigated"
    chain = engine.chain_for(rules, event)
    record = engine.run_chain(chain, event) if chain else engine.no_rule(event)
    r = record["result"]
    kept = event.get("source") == "ticket" or not (any(rule.quiet_stops for rule in chain if rule.position == "main") and r["kind"] == "stop")
    if kept:
        pipeline.finish(record, rules, print, inline=hand_over is None, root=root)
        store.save(record)
        if hand_over and (record.get("agent") or {}).get("status") == "queued":
            hand_over(record["id"])
    if event["event"] == "config.changed":
        _remember_config(event)
    repair = f"; repair: {record['repair']['status']}" if record.get("repair") else ""
    mail = "; emailed" if (record.get("email") or {}).get("sent") else ""
    ai = f"; AI: {record['agent']['status']}" if record.get("agent") else ""
    return (f"{label}: {len(record['steps'])} steps -> {r['kind']}: {r.get('text', r.get('label', ''))}{repair}{ai}{mail}"
            f"{'' if kept else ' (not kept as a run)'}")


def _remember_config(event: dict) -> None:
    """So the next change is compared with this config. What counts as known-good is decided elsewhere."""
    from ts import configs
    _shown, lines = configs.running(event["device"])
    if lines is not None:
        configs.save_baseline(event["device"], lines)


def cmd_incident_from_traps(args) -> int:
    import time
    from ts.intake import link_down_events
    rules = load_ruleset(args.rules).parsed
    since = time.time() - args.hours * 3600
    events = [e for e in link_down_events(since) if time.time() - e["time"] >= CONFIRM_DELAY]
    if args.device:
        events = [e for e in events if e["device"] == args.device]
    for event in events[-args.limit:]:
        print(_run_event(rules, event, args.rerun))
    if not events:
        print(f"no linkDown traps in the last {args.hours} h")
    return 0


def cmd_incident_list(args) -> int:
    import time
    from ts import store
    rows = store.summaries()
    if not rows:
        print("no incidents yet")
    for r in rows:
        when = time.strftime("%b %d %H:%M:%S", time.localtime(r["time"]))
        print(f"{when}  {r['device']:<4} {r['interface']:<10} {r['steps']} steps  {r['result']['kind']:<9} "
              f"{r['result'].get('text') or r['result'].get('label')}")
    return 0


def cmd_watch(args) -> int:
    """Follow the trap log and sweep the network; investigate each event once it is ready."""
    import queue
    import threading
    import time
    from ts import engine, lab, store, sweep
    from ts.intake import TRAPS_LOG, Waiting, events_since, follow, parse_line
    from ts.vocab import FORBIDDEN_DEVICES

    from ts import pipeline, status

    def stamp() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def say(message: str) -> None:
        print(f"{stamp()} {message}", flush=True)

    from ts import history
    stored = [r for r in (store.load(s["id"]) for s in store.summaries()) if r]
    indexed = history.backfill(stored)
    for record in stored:
        pipeline.tag(record, "backfill")
    say(f"history holds {indexed} runs")
    status.update(started=time.time())

    ai_work: queue.Queue = queue.Queue()

    def taking_over() -> None:
        """The AI works one incident at a time, off to the side, so alarms keep being investigated meanwhile."""
        while True:
            run_id = ai_work.get()
            record = store.load(run_id)
            if not record:
                continue
            status.update(ai_working={"id": run_id, "title": record["event"].get("title"), "since": time.time()})
            say(f"{run_id}: the AI is taking over: {record['agent'].get('why')}")
            try:
                record["agent"]["status"] = "working"
                store.save(record)
                pipeline.take_over(record, load_ruleset(args.rules).parsed, say, Path(args.rules))
                store.save(record)
                say(f"{run_id}: the AI finished: {record['agent'].get('status')}, {record['agent'].get('outcome')}, {len(record['agent'].get('calls') or [])} steps, {len(record['agent'].get('proposals') or [])} suggestions")
            except Exception as exc:  # one bad run must not stop the next
                say(f"{run_id}: the AI failed: {type(exc).__name__}: {exc}")
            status.update(ai_working=None)

    threading.Thread(target=taking_over, daemon=True).start()
    for record in stored:  # anything left unfinished by a restart
        state = (record.get("agent") or {}).get("status")
        if state == "queued":
            ai_work.put(record["id"])
        elif state == "working":
            record["agent"].update(status="failed", error="The watcher restarted while the AI was working on this.", outcome="needs_person")
            store.save(record)

    def remembering() -> None:
        from ts import cards, memory
        cards.AUTO = True
        first = True
        while True:
            try:
                cards.catch_up(say)
                added = memory.keep_up(say)
                if first:
                    say(f"memory is up to date, took in {sum(added.values())} records: {added}")
                first = False
            except Exception as exc:  # the memory must never stop the watcher
                say(f"memory failed: {type(exc).__name__}: {exc}")
            try:
                from ts import jenkins_index
                jenkins_index.keep_up(say)
            except Exception as exc:  # nor the Jenkins index
                say(f"Jenkins index failed: {type(exc).__name__}: {exc}")
            time.sleep(memory.INGEST_EVERY)

    threading.Thread(target=remembering, daemon=True).start()

    def investigate(event: dict) -> str:
        return _run_event(load_ruleset(args.rules).parsed, event, root=Path(args.rules), hand_over=ai_work.put)

    from ts import tickets
    threading.Thread(target=tickets.work_forever, args=(investigate, say), daemon=True).start()
    waiting = Waiting()

    def wait_for(event: dict, how: str) -> None:
        if event["device"] in FORBIDDEN_DEVICES:
            return
        if waiting.add(event):
            say(f"{how}: {event.get('title', event['event'])}; investigating when ready")

    # Traps that arrived while the watcher was not running (a restart, a crash).
    for event in events_since(time.time() - args.catch_up * 60):
        if not store.exists(engine.incident_id(event)):
            event["change_running"] = lab.change_running()
            wait_for(event, "catching up")

    found: queue.Queue = queue.Queue()
    if args.sweep > 0:
        def sweeping() -> None:
            state = sweep.load_state()
            sweep.remember_configs(say)
            while True:
                began = time.time()
                try:
                    for event in sweep.run(state, say):
                        found.put(event)
                    sweep.save_state(state)
                    status.update(last_sweep=time.time())
                except Exception as exc:  # a failed sweep must not stop the next one
                    say(f"sweep failed: {type(exc).__name__}: {exc}")
                time.sleep(max(1.0, args.sweep - (time.time() - began)))

        threading.Thread(target=sweeping, daemon=True).start()

    import os
    from ts import jenkins_runs, pipelines
    pipelines.AUTO = True
    threading.Thread(target=pipelines.keep_in_step, args=(Path(args.rules), say), daemon=True).start()
    if os.environ.get("TS_JENKINS", "on") != "off":
        jenkins_runs.enable(say)
        say("rules and repairs run as Jenkins pipelines in troubleshooting-pipelines; here if Jenkins cannot take them")

    from ts import monitor_ai, monitors
    threading.Thread(target=monitors.watch_forever, args=(found.put, say), daemon=True).start()
    monitors.serve_metrics(log=say)
    monitor_ai.start(say)

    say(f"watching {TRAPS_LOG} for traps" + (f", sweeping every {args.sweep:.0f} s" if args.sweep > 0 else ""))
    last_status = 0.0
    for line in follow(TRAPS_LOG):
        if time.time() - last_status >= 5:
            last_status = time.time()
            status.update(alive=last_status, waiting=[{"title": i["event"].get("title") or i["event"]["event"], "device": i["event"]["device"], "source": i["event"].get("source"), "due": i["due"], "count": i["count"]} for i in waiting.items], ai_queue=ai_work.qsize())
        if line:
            event = parse_line(line)
            if event:
                event["change_running"] = lab.change_running()
                wait_for(event, "trap")
            continue
        while not found.empty():
            wait_for(found.get(), "sweep")
        ready = waiting.ready(time.time())
        if ready:
            rules = load_ruleset(args.rules).parsed
            for event in ready:
                try:
                    say(_run_event(rules, event, root=Path(args.rules), hand_over=ai_work.put))
                except Exception as exc:  # one bad event must not stop the watcher
                    say(f"{event['device']} {event.get('subject')}: failed: {type(exc).__name__}: {exc}")
    return 0


def cmd_sweep(args) -> int:
    """One look at everything the sweep watches, without opening any investigations."""
    from ts import sweep
    bad = 0
    for finding in sweep.look(print):
        bad += not finding.ok
        print(f"{'ok ' if finding.ok else 'BAD'}  {finding.key}")
    print(f"{bad} not ok")
    return 0


def cmd_memory_stats(args) -> int:
    from ts import history
    held = history.sizes()
    print(f"{held['runs']} past runs and {held['docs']} records")
    for name, count in sorted(held["by_source"].items()):
        print(f"  {name:11} {count}")
    return 0


def cmd_memory_catch_up(args) -> int:
    from ts import memory
    print(memory.keep_up(print))
    return 0


def cmd_memory_notes(args) -> int:
    from ts import cards
    print(cards.catch_up(print, limit=None))
    return 0


def cmd_memory_search(args) -> int:
    import time
    from ts import memory
    for item in memory.search(" ".join(args.words), args.k):
        seen = f"  (seen {item['times_seen']} times)" if item["times_seen"] > 1 else ""
        print(f"{item['score']:7.1f}  {time.strftime('%m-%d %H:%M', time.localtime(item['time']))}  {item['source']:10}  {item['title'][:110]}{seen}")
    return 0


def cmd_rule_mode(args) -> int:
    from ts import rulebook
    try:
        rulebook.set_mode(Path(args.rules), args.id, args.mode, "cli")
    except rulebook.RuleRefused as exc:
        print(exc)
        return 1
    print(f"{args.id} is now {args.mode}")
    return 0


def cmd_review_list(args) -> int:
    import time
    from ts import review
    for row in review.listing(args.status, size=100)["items"]:
        when = time.strftime("%b %d %H:%M", time.localtime(row["created"]))
        print(f"#{row['id']:<4} {row['status']:<9} {row['kind']:<9} {when}  {row['title']}" + (f"  (rejected: {row['reason']})" if row["reason"] else ""))
    return 0


def cmd_review_accept(args) -> int:
    from ts import review
    print(review.accept(args.id, "cli", args.mode, root=Path(args.rules)))
    return 0


def cmd_review_reject(args) -> int:
    from ts import review
    review.reject(args.id, args.reason, "cli")
    print(f"#{args.id} rejected")
    return 0


def cmd_tag_list(args) -> int:
    from ts import review
    for row in review.tag_catalog():
        print(f"{row['tag']:<28} {row['runs']:>4} runs  {row['description']}")
    return 0


def cmd_ai_run(args) -> int:
    """Let the AI work on a stored run, right now."""
    from ts import pipeline, store
    record = store.load(args.id)
    if record is None:
        print(f"no run {args.id}")
        return 1
    pipeline.take_over(record, load_ruleset(args.rules).parsed, print, Path(args.rules))
    store.save(record)
    ai = record["agent"]
    print(ai["status"], ai.get("outcome"), len(ai.get("calls") or []), "steps", ai.get("proposals"), ai.get("error", ""))
    return 0


def cmd_mcp(args) -> int:
    from ts.mcp import serve
    return serve(Path(args.rules), args.run)


def cmd_dataset_backfill(args) -> int:
    from ts import dataset
    print(dataset.backfill())
    return 0


def cmd_dataset_export(args) -> int:
    from ts import dataset
    print(json.dumps(dataset.export(Path(args.out), "all" if args.all else "solved"), indent=1))
    return 0


def cmd_dataset_snapshot(args) -> int:
    from ts import dataset
    print(json.dumps(dataset.snapshot(), indent=1))
    return 0


def cmd_dataset_stats(args) -> int:
    from ts import capture, dataset
    with capture.open_for_reading() as db:
        print(json.dumps(dataset.totals(db), indent=1, default=dataset.plain))
    return 0


def cmd_desk(args) -> int:
    from ts.desk import serve
    serve(args.host, args.port)
    return 0


def cmd_train_worker(args) -> int:
    from ts import training
    return training.run_job(args.job)


def _spec_from(path: str) -> dict:
    import json
    return json.loads(Path(path).read_text())


def cmd_monitor_list(args) -> int:
    import time
    from ts import monitors
    rows = monitors.listing()
    if not rows:
        print("no monitors yet")
    for row in rows:
        seen = time.strftime("%m-%d %H:%M", time.localtime(row["last_eval"])) if row.get("last_eval") else "never"
        print(f"{row['id']:<32} {row['status']:<7} {row.get('state') or '-':<10} looked {seen}")
        print(f"    {monitors.describe(row['spec'])}")
    return 0


def cmd_monitor_test(args) -> int:
    import json
    import time
    from ts import monitors
    try:
        spec = monitors.clean(_spec_from(args.file))
    except monitors.Invalid as exc:
        print(f"invalid: {exc}")
        return 1
    around = time.mktime(time.strptime(args.around, "%Y-%m-%d %H:%M")) if args.around else None
    result = monitors.backtest(spec, days=args.days, around=around)
    print(monitors.describe(spec))
    print(json.dumps({k: v for k, v in result.items() if k != "episodes"}, indent=1, default=str))
    for e in result["episodes"]:
        what = "fired" if e["fired_at"] else "held back: " + ", ".join(e["jobs"][:2])
        print(f"  {time.strftime('%m-%d %H:%M', time.localtime(e['began']))}  {what}")
    return 0


def cmd_monitor_add(args) -> int:
    from ts import grafana_monitors, monitors
    try:
        made = monitors.add(monitors.clean(_spec_from(args.file)), args.why, "operator")
    except monitors.Invalid as exc:
        print(f"invalid: {exc}")
        return 1
    print(f"added {made['id']}; grafana: {grafana_monitors.sync_quietly()}")
    return 0


def cmd_monitor_status(args) -> int:
    from ts import grafana_monitors, monitors
    monitors.set_status(args.id, args.status, "operator")
    print(f"{args.id} is {args.status}; grafana: {grafana_monitors.sync_quietly()}")
    return 0


def cmd_monitor_suggest(args) -> int:
    from ts import monitor_ai, store
    record = store.load(args.run)
    if not record:
        print(f"no run {args.run}")
        return 1
    why_not = monitor_ai.skip_reason(record)
    if why_not and not args.anyway:
        print(f"the watcher would not look at this run: {why_not} (use --anyway to look all the same)")
        return 1
    done = monitor_ai.suggest(record, print, args.model)
    for c in done["calls"]:
        print(f"  {c['n']:>2}. {c['tool']}: {c['outcome']} {c['saw'][:160]}")
    return 0 if done["status"] == "done" else 1


def cmd_monitor_grafana(args) -> int:
    from ts import grafana_monitors
    print(grafana_monitors.sync())
    return 0


def cmd_jenkins_sync(args) -> int:
    import json
    from ts import pipelines
    print(json.dumps(pipelines.sync(load_ruleset(args.rules).parsed), indent=1))
    return 0


def cmd_jenkins_status(args) -> int:
    from ts import jenkins_api, pipelines
    wanted = pipelines.wanted(load_ruleset(args.rules).parsed)
    have = jenkins_api.jobs()
    print(f"{jenkins_api.link()}  {len(have)} pipelines, {len(wanted)} rules that run")
    for name in sorted(set(wanted) | set(have)):
        rule = wanted.get(name)
        state = "missing" if name not in have else "disabled" if have[name]["disabled"] else "ready"
        print(f"  {name:<44} {state:<9} {rule.table + ' ' + rule.mode if rule else 'no rule'}")
    return 0


def cmd_jenkins_run(args) -> int:
    """Run one stored run's rule again through Jenkins, to see a pipeline work. Changes nothing: fix rules are not run."""
    from ts import engine, jenkins_runs, store
    record = store.load(args.run)
    if not record:
        print(f"no run {args.run}")
        return 1
    rules = load_ruleset(args.rules).parsed
    rule = next((r for r in rules if r.id == (args.rule or record["rule"]["id"])), None)
    if rule is None or rule.table != "diagnose":
        print("that run has no diagnose rule to replay; name one with --rule")
        return 1
    jenkins_runs.enable()
    again = engine.run(rule, record["event"])
    j = again.get("jenkins") or {}
    print(f"{rule.id}: {again['result']['kind']} {again['result'].get('label') or again['result'].get('text') or ''}")
    print(f"ran {j.get('ran')}: {j.get('url') or j.get('why')}")
    return 0 if j.get("ran") == "jenkins" else 1


def cmd_serve(args) -> int:
    from ts import pipelines
    from ts.web import serve
    pipelines.AUTO = True  # a rule accepted or moved on these pages gets its pipeline straight away
    serve(args.host, args.port, Path(args.rules))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m ts", description=__doc__)
    p.add_argument("--rules", default=str(DEFAULT_RULES_DIR), help="rules directory (default: %(default)s)")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("blocks", help="list the registered checks and actions")
    b.add_argument("name", nargs="?")
    b.set_defaults(func=cmd_blocks)

    r = sub.add_parser("rule", help="work with rules").add_subparsers(dest="rule_command", required=True)
    r.add_parser("lint", help="lint every installed rule").set_defaults(func=cmd_lint)
    v = r.add_parser("validate", help="check rule files against the installed set without installing")
    v.add_argument("files", nargs="+")
    v.set_defaults(func=cmd_validate)
    ls = r.add_parser("list", help="the installed rules, like dump-flows")
    ls.add_argument("--table", choices=("filter", "diagnose", "fix"))
    ls.set_defaults(func=cmd_list)
    sh = r.add_parser("show", help="one rule as a tree")
    sh.add_argument("id")
    sh.set_defaults(func=cmd_show)
    ins = r.add_parser("install", help="install a rule file as proposed")
    ins.add_argument("file")
    ins.add_argument("--replace", action="store_true", help="replace an installed rule with a higher version")
    ins.set_defaults(func=cmd_install)

    mode = r.add_parser("mode", help="move a rule to another mode")
    mode.add_argument("id")
    mode.add_argument("mode", choices=("proposed", "shadow", "active", "retired"))
    mode.set_defaults(func=cmd_rule_mode)

    rev = sub.add_parser("review", help="what the AI has suggested").add_subparsers(dest="review_command", required=True)
    rl = rev.add_parser("list")
    rl.add_argument("--status", choices=("pending", "accepted", "rejected"))
    rl.set_defaults(func=cmd_review_list)
    ra = rev.add_parser("accept")
    ra.add_argument("id", type=int)
    ra.add_argument("--mode", choices=("shadow", "active"), default="shadow")
    ra.set_defaults(func=cmd_review_accept)
    rr = rev.add_parser("reject")
    rr.add_argument("id", type=int)
    rr.add_argument("reason")
    rr.set_defaults(func=cmd_review_reject)
    sub.add_parser("tags", help="the list of tags and how often each is used").set_defaults(func=cmd_tag_list)
    ai = sub.add_parser("ai", help="the AI").add_subparsers(dest="ai_command", required=True)
    ar = ai.add_parser("run", help="let the AI work on a stored run now")
    ar.add_argument("id")
    ar.set_defaults(func=cmd_ai_run)
    m = sub.add_parser("mcp", help="serve the AI's tools over MCP on stdin and stdout")
    m.add_argument("--run", help="the stored run these tools work on")
    m.set_defaults(func=cmd_mcp)

    inc = sub.add_parser("incident", help="investigations").add_subparsers(dest="incident_command", required=True)
    ft = inc.add_parser("from-traps", help="investigate linkDown traps already in the trap log")
    ft.add_argument("--hours", type=float, default=24)
    ft.add_argument("--limit", type=int, default=50)
    ft.add_argument("--device")
    ft.add_argument("--rerun", action="store_true", help="investigate again even if a record exists")
    ft.set_defaults(func=cmd_incident_from_traps)
    inc.add_parser("list", help="stored incidents").set_defaults(func=cmd_incident_list)
    w = sub.add_parser("watch", help="investigate new traps as they arrive, and sweep for what sends none")
    w.add_argument("--catch-up", type=float, default=10, metavar="MIN",
                   help="on start, also investigate traps from the last MIN minutes that have no record")
    w.add_argument("--sweep", type=float, default=1200, metavar="SEC", help="seconds between heartbeat checks of the whole network, 0 for none")
    w.set_defaults(func=cmd_watch)
    sub.add_parser("sweep", help="one look at everything the sweep watches").set_defaults(func=cmd_sweep)

    mem = sub.add_parser("memory", help="the searchable store of what every source has said").add_subparsers(dest="memory_command", required=True)
    mem.add_parser("stats", help="how much is stored").set_defaults(func=cmd_memory_stats)
    mem.add_parser("catch-up", help="read the sources now, up to the present").set_defaults(func=cmd_memory_catch_up)
    mem.add_parser("notes", help="write the missing note on every past run and index everything by meaning").set_defaults(func=cmd_memory_notes)
    find = mem.add_parser("search", help="search it by hand, the way a run does")
    find.add_argument("words", nargs="+")
    find.add_argument("-k", type=int, default=10)
    find.set_defaults(func=cmd_memory_search)

    ds = sub.add_parser("dataset", help="the data kept for fine-tuning").add_subparsers(dest="dataset_command", required=True)
    ds.add_parser("backfill", help="put every run already on disk into the training database").set_defaults(func=cmd_dataset_backfill)
    ex = ds.add_parser("export", help="write the fine-tuning files")
    ex.add_argument("out")
    ex.add_argument("--all", action="store_true", help="include runs the AI did not solve, up to their first mistake")
    ex.set_defaults(func=cmd_dataset_export)
    ds.add_parser("snapshot", help="one complete copy of everything collected").set_defaults(func=cmd_dataset_snapshot)
    ds.add_parser("stats", help="how much is stored").set_defaults(func=cmd_dataset_stats)

    d = sub.add_parser("desk", help="serve the service desk, where people raise tickets")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8771)
    d.set_defaults(func=cmd_desk)
    tw = sub.add_parser("train-worker", help="look after one fine-tuning job; the console starts this")
    tw.add_argument("job")
    tw.set_defaults(func=cmd_train_worker)
    jen = sub.add_parser("jenkins", help="the rules as Jenkins pipelines").add_subparsers(dest="jenkins_command", required=True)
    js = jen.add_parser("sync", help="create or rewrite the pipelines to match the rules")
    js.add_argument("--rules", default=str(DEFAULT_RULES_DIR))
    js.set_defaults(func=cmd_jenkins_sync)
    jt = jen.add_parser("status", help="each rule's pipeline and whether it is ready")
    jt.add_argument("--rules", default=str(DEFAULT_RULES_DIR))
    jt.set_defaults(func=cmd_jenkins_status)
    jr = jen.add_parser("run", help="run a stored run's diagnose rule again through its pipeline (read-only checks)")
    jr.add_argument("run")
    jr.add_argument("--rule")
    jr.add_argument("--rules", default=str(DEFAULT_RULES_DIR))
    jr.set_defaults(func=cmd_jenkins_run)
    mon = sub.add_parser("monitor", help="monitors: variables with conditions that start a run").add_subparsers(dest="monitor_command", required=True)
    mon.add_parser("list", help="the monitors and their state").set_defaults(func=cmd_monitor_list)
    mt = mon.add_parser("test", help="replay a monitor written as JSON over the past days")
    mt.add_argument("file")
    mt.add_argument("--days", type=float, default=7)
    mt.add_argument("--around", help="when an incident began, YYYY-MM-DD HH:MM, to see whether it is caught")
    mt.set_defaults(func=cmd_monitor_test)
    ma = mon.add_parser("add", help="add a monitor written as JSON, active at once")
    ma.add_argument("file")
    ma.add_argument("--why", default="", help="why it exists, shown with it")
    ma.set_defaults(func=cmd_monitor_add)
    ms = mon.add_parser("status", help="pause, resume or retire a monitor")
    ms.add_argument("id")
    ms.add_argument("status", choices=("active", "paused", "retired"))
    ms.set_defaults(func=cmd_monitor_status)
    mg = mon.add_parser("suggest", help="let the AI suggest monitors from a stored run now")
    mg.add_argument("run")
    mg.add_argument("--model", help="a model already chosen on the AI models page")
    mg.add_argument("--anyway", action="store_true", help="even if the watcher would skip this run")
    mg.set_defaults(func=cmd_monitor_suggest)
    mon.add_parser("grafana", help="rebuild the Grafana dashboard of monitors").set_defaults(func=cmd_monitor_grafana)
    s = sub.add_parser("serve", help="serve the read-only rules UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8770)
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
