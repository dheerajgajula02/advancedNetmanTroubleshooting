"""What the Collect page shows: how much has been gathered for training, and where it came from."""

from __future__ import annotations

from ts import capture, dataset, training

TRIGGERS = {"trap": "Traps", "ticket": "Tickets", "health": "Heartbeat"}
SOLVED = "fixed"
GATHER = """
SELECT e.episode_id::text AS id, e.run_id, e.kind, e.outcome->>'outcome' AS outcome,
       r.record->'event'->>'source' AS source, r.record->'event'->>'event' AS event,
       (SELECT count(*) FROM steps s WHERE s.episode_id = e.episode_id) AS steps,
       (SELECT count(*) FROM steps s WHERE s.episode_id = e.episode_id AND s.reason <> '') AS reasons,
       (SELECT count(*) FROM llm_calls c WHERE c.episode_id = e.episode_id AND c.thinking <> '') AS thought,
       (SELECT count(*) FROM llm_calls c WHERE c.episode_id = e.episode_id) AS calls
FROM episodes e LEFT JOIN runs r USING (run_id) WHERE e.status <> 'running' AND e.kind = 'agent'
"""


def _held_out(row: dict) -> bool:
    return dataset._fraction(row["run_id"] or row["id"]) < dataset.SETTINGS["test_fraction"]


def _tally(rows: list[dict]) -> dict:
    return {"episodes": len(rows), "solved": sum(r["outcome"] == SOLVED for r in rows), "held_out": sum(_held_out(r) for r in rows), "steps": sum(r["steps"] for r in rows)}


def _grouped(rows: list[dict], key) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return sorted(({"key": k, **_tally(v)} for k, v in groups.items()), key=lambda g: -g["episodes"])


def _campaign_view(campaign: dict, by_run: dict[str, dict], titles: dict[str, str], built: list[dict], jobs: list[dict]) -> dict:
    lines = []
    for scenario in campaign["scenarios"]:
        plan = [i for i in campaign["plan"] if i["scenario"] == scenario]
        ended = [i for i in plan if i["status"] == "done"]
        rows = [by_run[i["run_id"]] for i in ended if i["run_id"] in by_run]
        lines.append({"id": scenario, "title": titles.get(scenario, scenario), "planned": len([i for i in plan if i["status"] != "skipped"]), "done": len(ended), "staged": any(i["status"] == "staged" for i in plan),
                      "episodes": len(rows), "solved": sum(r["outcome"] == SOLVED for r in rows), "held_out": sum(_held_out(r) for r in rows)})
    planned = sum(x["planned"] for x in lines)
    finished = sum(x["done"] for x in lines)
    made = next((d for d in built if d["name"] == campaign["name"]), None)
    job = next((j for j in jobs if made and j["dataset"].startswith(campaign["name"] + "-v")), None)
    tuned = ((job or {}).get("result") or {}).get("tuned")
    stages = [{"label": "Situations", "state": "done"}, {"label": "Collect", "state": "done" if planned and finished == planned else "now" if campaign["state"] == "running" else "stopped"},
              {"label": "Dataset", "state": "done" if made else "next"}, {"label": "Fine-tune", "state": "done" if job and job["state"] == "done" else "now" if job and job["state"] in training.ACTIVE else "stopped" if job else "next"},
              {"label": "Test", "state": "done" if tuned else "next"}]
    return {**{k: campaign[k] for k in ("id", "name", "created", "state", "rounds", "target")}, "lines": lines, "planned": planned, "finished": finished, "stages": stages, "dataset": made and {"folder": made["folder"], **made["stats"]},
            "job": job and {"id": job["id"], "name": job["name"], "state": job["state"]}, "plan": [i for i in campaign["plan"] if i["status"] == "staged"]}


def overview() -> dict:
    from drills import campaign as campaigns, scenarios
    from drills.run import _why_not_now
    with capture.open_for_reading() as db:
        rows = [dict(r) for r in db.execute(GATHER).fetchall()]
    built, jobs = training.datasets(), training.listing()
    by_run = {r["run_id"]: r for r in rows if r["run_id"]}
    titles = {s["id"]: s["title"] for s in scenarios.SCENARIOS}
    steps, reasons = sum(r["steps"] for r in rows), sum(r["reasons"] for r in rows)
    calls, thought = sum(r["calls"] for r in rows), sum(r["thought"] for r in rows)
    triggers = [{**g, "label": TRIGGERS.get(g["key"], "Before tickets and the heartbeat")} for g in _grouped(rows, lambda r: r["source"] if r["source"] in TRIGGERS else "other")]
    return {"totals": {**_tally(rows), "reasons": reasons, "thought": thought, "calls": calls}, "share": {"reasons": reasons / steps if steps else 0, "thought": thought / calls if calls else 0}, "triggers": triggers,
            "kinds": _grouped(rows, lambda r: r["event"] or "unknown")[:12], "campaigns": [_campaign_view(c, by_run, titles, built, jobs) for c in campaigns.listing()], "datasets": built, "agents": dataset.by_agent(),
            "situations": [{"id": s["id"], "title": s["title"], "what": s["what"]} for s in scenarios.SCENARIOS], "why_not": _why_not_now(), "target": campaigns.TARGET_SOLVED, "max_rounds": campaigns.MAX_ROUNDS}
