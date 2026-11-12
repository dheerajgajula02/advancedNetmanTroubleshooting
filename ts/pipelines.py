"""Every rule as a Jenkins pipeline in the troubleshooting-pipelines folder.

A diagnose rule becomes one stage per step. The pipeline carries the rule's routing table: for
each step, which answer goes where. It asks the troubleshooter to run each step, decides the next
stage itself, and stops if the troubleshooter's engine would have gone somewhere else, which can
only mean the pipeline is older than its rule. A fix rule becomes its stages: is it safe, the
change, did it work, then keep it or undo it.

The pipelines are written from the rules and overwritten whenever they change, so the rule file
stays the one place a rule is edited. Each is named by the rule's `pipeline`, or its id.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

from ts import engine, jenkins_api
from ts.fixer import VERIFY_EVERY

KEEP_BUILDS = 300
# Set by the long-running services. Tests and one-off commands leave Jenkins alone when a rule changes.
AUTO = False
LOOK_EVERY = 60        # the watcher compares the rules on disk with the last sync this often
FULL_EVERY = 3600      # and checks every pipeline in Jenkins this often, in case one was edited there
_SYNCING = threading.Lock()


def name_of(rule) -> str:
    return getattr(rule, "pipeline", None) or rule.id


def _groovy(text: str) -> str:
    """A single-quoted Groovy string."""
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def _comment(text: str) -> str:
    return "\n".join(f"// {line}".rstrip() for line in str(text).splitlines())


ASK = r"""
def tell(String what) {
  withEnv(["TS_WHAT=${what}"]) {
    sh(label: 'Ask the troubleshooter', script: '''set +x
mkdir -p .ts
code=$(curl -sS -o .ts/answer -w '%{http_code}' --max-time 900 -X POST -H "X-TS-Key: $TS_KEY" "$TS_URL/jenkins/$TS_SESSION/$TS_WHAT") || { echo "The troubleshooter did not answer."; exit 1; }
if [ "$code" != 200 ]; then cat .ts/answer; echo; echo "The troubleshooter refused this (HTTP $code)."; exit 1; fi
tail -n +3 .ts/answer
sed -n 1p .ts/answer > .ts/outcome
sed -n 2p .ts/answer > .ts/next''')
  }
  return [outcome: readFile('.ts/outcome').trim(), next: readFile('.ts/next').trim()]
}
"""


def flow(rule) -> list[dict]:
    """The routing table a diagnose pipeline carries: for each step, where each answer goes."""
    return [{"name": step.id or f"s{i + 1}", "ask": step.ask or step.check.block.description, "check": step.check.render(),
             "exits": {name: engine.next_token(rule, i, outcome) for name, outcome in step.then.items()},
             "otherwise": engine.next_token(rule, i, engine.Outcome(kind="next"))} for i, step in enumerate(rule.steps)]


def diagnose_script(rule) -> str:
    rows = []
    for n, row in enumerate(flow(rule), 1):
        exits_groovy = "[" + ", ".join(f"{_groovy(k)}: {_groovy(v)}" for k, v in row["exits"].items()) + "]" if row["exits"] else "[:]"
        rows.append(f"  {n}: [name: {_groovy(row['name'])}, ask: {_groovy(row['ask'])}, "
                    f"check: {_groovy(row['check'])}, exits: {exits_groovy}, otherwise: {_groovy(row['otherwise'])}],")
    return f"""{_comment(rule.title or rule.id)}
// Written by the troubleshooter from its rule "{rule.id}" (version {rule.version}). Changes made here are overwritten: edit the rule.
// Each stage is one step. The troubleshooter runs the step's check and says what it saw. This pipeline
// decides where the rule goes next from the table below, and stops if the troubleshooter would go elsewhere.
// A step's answer that is not listed under exits goes on to "otherwise". step/N is step N; "end" is past the
// last step; cause, stop and escalate end the rule there.

def FLOW = [
{chr(10).join(rows)}
]
{ASK}
node {{
  currentBuild.displayName = "#${{env.BUILD_NUMBER}} ${{params.RUN}}"
  String at = 'step/1'
  while (at.startsWith('step/')) {{
    int n = Integer.parseInt(at.substring(5))
    def step = FLOW[n]
    stage("${{n}}. ${{step.name}}") {{
      def answer = tell("step/${{n}}")
      String mine = step.exits[answer.outcome] ?: step.otherwise
      if (mine != answer.next) {{
        error("This pipeline would go to ${{mine}} but the rule goes to ${{answer.next}}: the pipeline is older than its rule.")
      }}
      at = mine
    }}
  }}
  stage('Conclusion') {{
    def answer = tell("finish/${{at}}")
    currentBuild.description = answer.next
  }}
}}
"""


def fix_script(rule) -> str:
    verify = "; ".join(c.render() for c in rule.verify)
    require = "; ".join(c.render() for c in rule.require) or "nothing beyond the usual guards"
    return f"""{_comment(rule.title or rule.id)}
// Written by the troubleshooter from its fix rule "{rule.id}" (version {rule.version}). Changes made here are overwritten: edit the rule.
// The troubleshooter does each part and holds the network change lock from the change to the end.
// If this build stops part-way, it undoes the change and lets go of the lock by itself.
// Requires: {require}
// Does: {rule.action.do.render()}
// Works when: {verify}
{ASK}
node {{
  currentBuild.displayName = "#${{env.BUILD_NUMBER}} ${{params.RUN}}"
  boolean closed = false
  try {{
    String state = ''
    stage('Safe to change?') {{
      state = tell('fix/check').outcome
    }}
    if (state == 'go') {{
      stage({_groovy('Change: ' + (rule.title or rule.id))}) {{
        state = tell('fix/change').outcome
      }}
      if (state == 'changed') {{
        stage('Did it work?') {{
          state = tell('fix/verify').outcome
          while (state == 'not_yet') {{
            sleep(time: {VERIFY_EVERY}, unit: 'SECONDS')
            state = tell('fix/verify').outcome
          }}
        }}
        if (state == 'ok') {{
          stage('Keep it as the new starting point') {{
            tell('fix/settle')
          }}
        }} else {{
          stage('Undo') {{
            tell('fix/undo')
          }}
        }}
      }}
    }}
    stage('Conclusion') {{
      def answer = tell('fix/finish')
      currentBuild.description = answer.next
      closed = true
      if (answer.outcome == 'failed') {{
        unstable('The repair did not work, and was undone.')
      }}
    }}
  }} finally {{
    if (!closed) {{
      try {{
        tell('fix/abandon')
      }} catch (err) {{
        echo 'Could not tell the troubleshooter that this build stopped. It undoes the change and lets go of the lock by itself.'
      }}
    }}
  }}
}}
"""


PARAMS = [
    ("RUN", "string", "The troubleshooting run this build belongs to."),
    ("TS_URL", "string", "Where the troubleshooter is listening for this build, on this machine."),
    ("TS_SESSION", "string", "The troubleshooter's name for this build."),
    ("TS_KEY", "password", "A one-time key, so only this build can ask the troubleshooter to run these steps."),
]


def _params_xml() -> str:
    kinds = {"string": "hudson.model.StringParameterDefinition", "password": "hudson.model.PasswordParameterDefinition"}
    out = []
    for name, kind, text in PARAMS:
        extra = "<trim>true</trim>" if kind == "string" else ""
        out.append(f"<{kinds[kind]}><name>{name}</name><description>{jenkins_api._xml(text)}</description><defaultValue></defaultValue>{extra}</{kinds[kind]}>")
    return "".join(out)


def description(rule) -> str:
    lines = [rule.title or rule.id, "", rule.description.strip()]
    if rule.table == "diagnose":
        if getattr(rule, "trigger", None):
            lines += ["", f"Runs when: {rule.trigger}"]
        lines += ["", "Steps:"] + [f"{i}. {s.ask or s.check.block.description} ({s.check.render()})" for i, s in enumerate(rule.steps, 1)]
    else:
        lines += ["", f"Answers: {json.dumps(rule.match.get('cause'))}", f"Does: {rule.action.do.render()}", f"Works when: {'; '.join(c.render() for c in rule.verify)}", f"Risk: {rule.risk}"]
    lines += ["", f"Mode: {rule.mode}. Rule {rule.id}, {rule.table} table, from {rule.source.by}.",
              "Started by the troubleshooter for each run; not meant to be started by hand. Written from the rule, so edits here are overwritten."]
    return "\n".join(lines)


def config_xml(rule) -> tuple[str, str]:
    """(the job's config.xml, its fingerprint)."""
    script = diagnose_script(rule) if rule.table == "diagnose" else fix_script(rule)
    text = description(rule)
    print_ = hashlib.sha256((script + text).encode()).hexdigest()[:16]
    script = f"// fingerprint {print_}\n" + script
    xml = f"""<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <description>{jenkins_api._xml(text)}</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <jenkins.model.BuildDiscarderProperty><strategy class="hudson.tasks.LogRotator"><daysToKeep>-1</daysToKeep><numToKeep>{KEEP_BUILDS}</numToKeep><artifactDaysToKeep>-1</artifactDaysToKeep><artifactNumToKeep>-1</artifactNumToKeep></strategy></jenkins.model.BuildDiscarderProperty>
    <hudson.model.ParametersDefinitionProperty><parameterDefinitions>{_params_xml()}</parameterDefinitions></hudson.model.ParametersDefinitionProperty>
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" plugin="workflow-cps"><script>{jenkins_api._xml(script)}</script><sandbox>true</sandbox></definition>
  <triggers/>
  <disabled>{'true' if rule.mode == 'retired' else 'false'}</disabled>
</flow-definition>
"""
    return xml, print_


FOLDER_TEXT = ("The troubleshooter's rules, one pipeline each. The troubleshooter starts them for every run, "
               "and its run pages link to the builds. They are written from the rules in the troubleshooter's rules/ folder; "
               "changes made here are overwritten.")


def wanted(rules) -> dict[str, object]:
    """Pipeline name -> rule, for every rule that runs. Two rules may not share a name."""
    out: dict[str, object] = {}
    for rule in sorted(rules, key=lambda r: r.id):
        if rule.table not in ("diagnose", "fix") or rule.mode == "proposed":
            continue
        name = name_of(rule)
        if name in out:
            raise ValueError(f"rules {out[name].id} and {rule.id} both want the pipeline {name}")
        out[name] = rule
    return out


def clash(rules, name: str, rule_id: str) -> str | None:
    """Why a new rule may not have this pipeline name, or None."""
    for rule in rules:
        if rule.id != rule_id and name_of(rule) == name:
            return f"The pipeline {name} already belongs to the rule {rule.id}."
    return None


def sync(rules, log=print) -> dict:
    """Make the folder match the rules: create or rewrite what changed, disable what is gone or retired."""
    created_folder = jenkins_api.ensure_folder(FOLDER_TEXT)
    have = jenkins_api.jobs()
    done = {"folder_created": created_folder, "created": [], "updated": [], "same": 0, "disabled": [], "enabled": []}
    for name, rule in wanted(rules).items():
        xml, print_ = config_xml(rule)
        if name in have:
            current = jenkins_api.config(name) or ""
            if f"fingerprint {print_}" in current:
                done["same"] += 1
            else:
                jenkins_api.put_job(name, xml, exists=True)
                done["updated"].append(name)
            if have[name]["disabled"] != (rule.mode == "retired"):
                jenkins_api.set_enabled(name, rule.mode != "retired")
                done["enabled" if rule.mode != "retired" else "disabled"].append(name)
        else:
            jenkins_api.put_job(name, xml, exists=False)
            done["created"].append(name)
    for name, info in have.items():
        if name not in wanted(rules) and not info["disabled"]:
            jenkins_api.set_enabled(name, False)
            done["disabled"].append(name)
    if done["created"] or done["updated"] or done["disabled"] or done["enabled"]:
        log(f"Jenkins pipelines: {len(done['created'])} created, {len(done['updated'])} rewritten, {len(done['disabled'])} disabled, {done['same']} unchanged")
    return done


def digest(rules) -> str:
    """What the folder should hold, in one value: it changes whenever a pipeline would."""
    parts = [f"{name}:{config_xml(rule)[1]}:{rule.mode}" for name, rule in wanted(rules).items()]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _sync_from(root, log) -> None:
    from ts.ruleset import DEFAULT_RULES_DIR, load_ruleset
    try:
        with _SYNCING:
            sync(load_ruleset(root or DEFAULT_RULES_DIR).parsed, log)
    except Exception as exc:  # the watcher tries again within a minute
        log(f"Jenkins pipelines could not be brought up to date: {type(exc).__name__}: {exc}")


def sync_soon(root=None, log=print) -> None:
    """A rule was installed or changed mode: bring its pipeline up to date in the background."""
    if AUTO:
        threading.Thread(target=_sync_from, args=(root, log), daemon=True).start()


def keep_in_step(root, log=print) -> None:
    """The watcher's loop: sync whenever the rules on disk change, and check everything hourly."""
    from ts.ruleset import load_ruleset
    last, checked, failing = None, 0.0, ""
    while True:
        try:
            rules = load_ruleset(root).parsed
            now = digest(rules)
            if now != last or time.time() - checked > FULL_EVERY:
                with _SYNCING:
                    sync(rules, log)
                last, checked, failing = now, time.time(), ""
        except Exception as exc:  # said once, not every minute, while Jenkins is away
            why = f"{type(exc).__name__}: {exc}"
            if why != failing:
                log(f"Jenkins pipelines could not be brought up to date: {why}")
            failing = why
        time.sleep(LOOK_EVERY)
