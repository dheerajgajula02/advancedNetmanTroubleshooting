"""Scoring of predicted next calls against the recorded ones; shared by the Needle and the LLM evaluators so
their numbers compare directly. Runs on the GPU machine (no repo imports).

  tool       right tool name
  exact      right tool and arguments
  args       share of the recorded arguments it got right (right tool only)
  later      the exact call is made later in the same run (useful, just out of order)
  changed    it proposed a change; on don't-act examples that is a mistake
  report_ok  on the last decision: finish with the right outcome
"""
import collections
import json
import re

def load_rows(path, tools_path):
    """Rows as written by ts/training.py, with each track's system prompt and tools put back."""
    sets = json.load(open(tools_path))
    rows = []
    for line in open(path, encoding="utf-8"):
        if line.strip():
            row = json.loads(line)
            s = sets[row["meta"].get("track", "tools")]
            row["system"], row["tools"] = s["system"], s["tools"]
            rows.append(row)
    return rows


def norm(value):
    if isinstance(value, str):
        return " ".join(value.lower().split())
    if isinstance(value, list):
        return sorted(norm(v) for v in value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def same_call(a, b):
    if not a or not b or a.get("name") != b.get("name"):
        return False
    aa, bb = a.get("arguments") or {}, b.get("arguments") or {}
    return all(norm(aa.get(k)) == norm(bb.get(k)) for k in set(aa) | set(bb))


CHANGE_TOOLS = ("try_config", "try_command")


def is_change(call):
    return bool(call) and (call["name"] in CHANGE_TOOLS or call["name"].startswith("action_"))


def score(row, pred, later_calls):
    truth = row["answers"][0]
    tool_ok = bool(pred) and pred.get("name") == truth["name"]
    args_share = None
    if tool_ok and truth["arguments"]:
        got = pred.get("arguments") or {}
        args_share = sum(norm(got.get(k)) == norm(v) for k, v in truth["arguments"].items()) / len(truth["arguments"])
    result = {"tool": tool_ok, "exact": same_call(pred, truth), "args": args_share,
              "later": any(same_call(pred, c) for c in later_calls), "changed": is_change(pred), "no_call": not pred}
    if row["meta"]["decision"] == "report":
        result["report_ok"] = (bool(pred) and pred.get("name") == "finish" and
                               (pred.get("arguments") or {}).get("outcome") == truth["arguments"].get("outcome"))
    return result


def _avg(values):
    values = [v for v in values if v is not None]
    return round(100 * sum(values) / len(values), 1) if values else None


def summarize(results):
    return {"n": len(results), "tool": _avg([r["score"]["tool"] for r in results]),
            "exact": _avg([r["score"]["exact"] for r in results]),
            "args": _avg([r["score"]["args"] for r in results]),
            "exact_or_later": _avg([r["score"]["exact"] or r["score"]["later"] for r in results]),
            "changed": _avg([r["score"]["changed"] for r in results]),
            "no_call": _avg([r["score"]["no_call"] for r in results])}


def report(rows, preds, seconds):
    """preds: {row index: (call or None, error or None)} -> (summary dict, per-row results)."""
    later = collections.defaultdict(list)
    for row in rows:
        later[row["meta"]["episode_id"]].append((row["meta"]["step"], row["answers"][0]))
    results = []
    for i, row in enumerate(rows):
        if i not in preds:
            continue
        pred, error = preds[i]
        m = row["meta"]
        calls = [c for s, c in later[m["episode_id"]] if s > m["step"]]
        results.append({"meta": m, "truth": row["answers"][0], "pred": pred, "error": error,
                        "score": score(row, pred, calls)})
    return summary_of(results, len(rows), seconds), results


def summary_of(results, planned, seconds=0):
    """The summary report() writes, from per-row results (also used on optiplex to score the base on the rows a
    stopped tuned run reached)."""
    def grouped(key):
        buckets = collections.defaultdict(list)
        for r in results:
            buckets[key(r)].append(r)
        return {k: summarize(v) for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1]))}
    reports = [r for r in results if r["meta"]["decision"] == "report"]
    changes = [r for r in results if r["meta"]["decision"] == "change"]
    dont = [r for r in results if not r["meta"]["need_act"]]
    return {"examples": len(results), "planned": planned, "seconds": round(seconds),
            "overall": summarize(results),
            "withheld": _avg([r.get("error") == "withheld" for r in results]),
            "report_status": _avg([r["score"].get("report_ok") for r in reports]),
            "change_exact": _avg([r["score"]["exact"] for r in changes]),
            "dont_act_changed": _avg([r["score"]["changed"] for r in dont]),
            "by_decision": grouped(lambda r: ("act" if r["meta"]["need_act"] else "dont-act") + ":" +
                                   (r["meta"]["decision"] or "read")),
            "by_track": grouped(lambda r: r["meta"].get("track", "tools")),
            "by_tool": dict(list(grouped(lambda r: r["truth"]["name"]).items())[:16])}


CALL_LINE = re.compile(r"(\d+)\. ([A-Za-z_]\w*)\(")
ARG = re.compile(r"(\w+)=")


def history_call(line):
    """'12. show(device="leaf1", command="show ip route") - why' (ts/dataset.py _call_text) -> (12, call)."""
    m = CALL_LINE.match(line)
    if not m:
        return None
    args, i, dec = {}, m.end(), json.JSONDecoder()
    while line[i:i + 1] != ")":
        a = ARG.match(line, i)
        if not a:
            return None
        try:
            args[a.group(1)], i = dec.raw_decode(line, a.end())
        except ValueError:
            return None
        if line.startswith(", ", i):
            i += 2
    return int(m.group(1)), {"name": m.group(2), "arguments": args}


def copy_last(rows):
    """The no-model baseline, as preds for report(): call again whatever was called last (show when nothing
    was). A model's step score means something only above this one: recorded runs come in stretches of one tool
    (get_interface_stats on every device, then show on several), so copying alone gets the next tool right about
    half the time, and never the fix or the report."""
    preds = {}
    for i, row in enumerate(rows):
        calls = [c for c in map(history_call, row["query"].splitlines()) if c]
        preds[i] = (max(calls, key=lambda c: c[0])[1] if calls else {"name": "show", "arguments": {}}, None)
    return preds


def parse_call(text):
    """The first JSON object with a 'name' in a model's text reply."""
    text = text or ""
    for start in [i for i, ch in enumerate(text) if ch == "{"][:8]:
        depth = 0
        for j in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            if depth == 0:
                try:
                    obj = json.loads(text[start:j + 1])
                except ValueError:
                    break
                if isinstance(obj, dict) and obj.get("name"):
                    args = obj.get("arguments") or obj.get("parameters") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    return {"name": obj["name"], "arguments": args if isinstance(args, dict) else {}}
                break
    return None
