"""Fine-tuning on what was collected: build a dataset, rent one GPU, train, bring the model home, delete the GPU.

Each job runs in its own process, so restarting the console does not orphan a rented machine. The money rules:
the job stops itself before its limit, the machine is deleted whatever happens, and a sweep deletes any machine
whose job is no longer being looked after. The machine also powers itself off if nobody has touched it for 40 minutes.
"""

from __future__ import annotations

import gzip
import json
import os
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ts import capture, dataset, history, trainspec

ROOT = capture.ROOT
RUNS = ROOT / "data" / "training"
DATASETS = ROOT / "data" / "datasets"
TRAINER = ROOT / "deploy" / "trainer"
VAST_KEYS = ROOT / "secrets" / "finetune" / "vast"
VAST = "https://console.vast.ai/api"
IMAGE = "vastai/pytorch:cuda-12.9.2-auto"
LABEL = "ts-train-"
REMOTE_ENV = "export PATH=/venv/main/bin:/opt/venv/bin:/usr/local/bin:$PATH HF_HUB_DISABLE_TELEMETRY=1; "
ONSTART = r"""mkdir -p /root/job; touch /root/.deadman
env | grep -E '^CONTAINER_(API_KEY|ID)=' > /root/.vastself; chmod 600 /root/.vastself
nohup bash -c 'while sleep 120; do a=$(( $(date +%s) - $(stat -c %Y /root/.deadman) )); if [ $a -gt 2400 ]; then
  . /root/.vastself; curl -s -X PUT -H "Authorization: Bearer $CONTAINER_API_KEY" -H "Content-Type: application/json"
  -d "{\"state\":\"stopped\"}" https://console.vast.ai/api/v0/instances/$CONTAINER_ID/; fi; done' > /root/deadman.log 2>&1 &
"""
ACTIVE = ("queued", "running", "stopping")
MAX_LIMIT = 25.0            # dollars; a typing slip should not be able to spend more than this
POLL = 20
WIND_DOWN_S = 300           # kept back from the limit: save, copy home, delete
STOP_GRACE_S = 480          # how long a stop may take before the machine is deleted anyway
LOST_AFTER_S = 600
READY_LIMIT_S = 900
REFUSED_LIMIT_S = 300       # a machine that answers but keeps turning the login down is given up on after this long
RENT_TRIES = 3
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
JOB_ID = re.compile(r"^ft-[0-9a-z-]+$")
METER = 1.02                # storage and bandwidth on top of the hourly price
SHORT_SYSTEM = 1400
SHORT_ARG = 240
DEFAULTS = {"epochs": 3.0, "batch_size": 2, "grad_accum": 4, "max_len": 6144, "lora_rank": 16, "lr": 2e-4, "max_examples": 0, "eval_examples": 40, "eval_base": True}
LIMITS = {"epochs": (0.25, 10), "batch_size": (1, 16), "grad_accum": (1, 16), "max_len": (1024, 8192), "lora_rank": (4, 128), "lr": (1e-6, 1e-2), "max_examples": (0, 40000), "eval_examples": (0, 500)}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS train_datasets (folder TEXT PRIMARY KEY, name TEXT, version INTEGER, created REAL, include TEXT, stats TEXT);
CREATE TABLE IF NOT EXISTS train_jobs (id TEXT PRIMARY KEY, name TEXT, created REAL, updated REAL, state TEXT, stage TEXT, stop_reason TEXT DEFAULT '', base_model TEXT, method TEXT,
  dataset TEXT, config TEXT, offer TEXT, gpu_name TEXT, price_per_hour REAL, limit_usd REAL, instance INTEGER, rented_at REAL, released_at REAL, cost_usd REAL DEFAULT 0, machine TEXT DEFAULT '',
  worker_pid INTEGER DEFAULT 0, stop_requested REAL DEFAULT 0, progress TEXT DEFAULT '{}', result TEXT DEFAULT '{}', error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS train_events (job_id TEXT, time REAL, level TEXT, msg TEXT);
CREATE INDEX IF NOT EXISTS train_events_job ON train_events (job_id, time);
CREATE TABLE IF NOT EXISTS train_metrics (job_id TEXT, step INTEGER, examples INTEGER, loss REAL, lr REAL, rate REAL, time REAL, PRIMARY KEY (job_id, step));
"""


class Refused(ValueError):
    """A job that may not start, in words a person can act on."""


def _db(path: Path | None = None):
    conn = history._open(path)
    conn.executescript(_SCHEMA)
    return conn


# --------------------------------------------------------------- datasets
def _short_tools(tools: list) -> list:
    """A small model cannot read the full tool descriptions: the name and its arguments are what it has to learn."""
    out = []
    for tool in tools or []:
        spec = tool.get("function") or tool
        out.append({"name": spec["name"], "args": sorted((spec.get("parameters") or {}).get("properties", {}))})
    return out


def _short_system(system: str) -> str:
    return dataset._cut((system or "").split("\n\n")[0], SHORT_SYSTEM)


def _short_row(row: dict) -> dict:
    answer = dict(row["answers"][0])
    if not row["meta"].get("whole"):  # when writing is the job, a cut answer teaches it to stop mid-sentence
        answer["arguments"] = {k: dataset._cut(v, SHORT_ARG) if isinstance(v, str) else v for k, v in (answer.get("arguments") or {}).items()}
    return {**row, "answers": [answer], "meta": {"need_act": True, **row["meta"]}}


def _rewrite(path: Path) -> list[dict]:
    rows = [_short_row(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return rows


def build_dataset(name: str, include: str = "graded", db: Path | None = None, root: Path | None = None, who: str = "support") -> dict:
    """Write one versioned copy of one agent's training examples. Later runs cannot change it."""
    name = (name or "").strip().lower()
    if not NAME.fullmatch(name):
        raise Refused("Name the dataset with lowercase letters, numbers and dashes.")
    if include not in ("graded", "solved"):
        raise Refused("Choose which runs to use.")
    if who not in dataset.agent_names():
        raise Refused("Choose which agent this dataset is for.")
    root = root or DATASETS
    with _db(db) as conn:
        version = 1 + conn.execute("SELECT COALESCE(MAX(version), 0) FROM train_datasets WHERE name = ?", (name,)).fetchone()[0]
    folder = f"{name}-v{version}"
    target = root / folder
    stats = dataset.export(target, include, who)
    tracks = json.loads((target / "tools.json").read_text())["tracks"]
    (target / "tools.json").write_text(json.dumps({track: {"system": _short_system(job["system"]), "tools": _short_tools(job["tools"])} for track, job in tracks.items()}))
    train, test = _rewrite(target / "train.jsonl"), _rewrite(target / "test.jsonl")
    (target / "conversations.jsonl").unlink(missing_ok=True)  # the snapshot keeps every whole conversation; this copy is what training reads
    chars = [len(r["query"]) for r in train]
    summary = {**stats, "train_unique": len(train), "train_rows": sum(int(r.get("weight", 1)) for r in train), "test_rows": len(test), "mean_chars": round(sum(chars) / len(chars)) if chars else 0}
    with _db(db) as conn:
        conn.execute("INSERT INTO train_datasets VALUES (?,?,?,?,?,?)", (folder, name, version, time.time(), include, json.dumps(summary)))
    return {"folder": folder, "name": name, "version": version, **summary}


def datasets(db: Path | None = None) -> list[dict]:
    with _db(db) as conn:
        rows = conn.execute("SELECT * FROM train_datasets ORDER BY created DESC").fetchall()
    return [{**dict(r), "stats": json.loads(r["stats"])} for r in rows]


def dataset_rows(folder: str, split: str = "train", page: int = 1, size: int = 25, root: Path | None = None) -> dict | None:
    """One page of the examples, to read what training will see."""
    if not re.fullmatch(r"[a-z0-9-]+-v\d+", folder) or split not in ("train", "test"):
        return None
    path = (root or DATASETS) / folder / f"{split}.jsonl"
    if not path.is_file():
        return None
    lines = path.read_text().splitlines()
    page = max(1, page)
    return {"folder": folder, "split": split, "page": page, "size": size, "total": len(lines), "rows": [json.loads(x) for x in lines[(page - 1) * size:page * size]]}


# --------------------------------------------------------------- the GPU market
def _key() -> str:
    label = (VAST_KEYS / "active").read_text().strip()
    return (VAST_KEYS / f"{label}.key").read_text().strip()


def _vast(method: str, path: str, body=None, timeout: int = 60):
    request = Request(VAST + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": "Bearer " + _key(), "Accept": "application/json", "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urlopen(request, timeout=timeout) as answer:
                return json.load(answer)
        except HTTPError as exc:
            if exc.code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"Vast {method} {path}: HTTP {exc.code} {exc.read()[:200]!r}") from None
        except OSError:
            if attempt == 3:
                raise
            time.sleep(5)


def credit() -> float | None:
    try:
        return round(float(_vast("GET", "/v0/users/current/", timeout=15).get("credit") or 0), 2)
    except (OSError, RuntimeError, ValueError):
        return None


def clean_config(given: dict) -> dict:
    out = {}
    for key, default in DEFAULTS.items():
        value = given.get(key, default)
        if isinstance(default, bool):
            out[key] = value is not False and value not in ("0", "false")
            continue
        try:
            value = type(default)(value)
        except (TypeError, ValueError):
            raise Refused(f"{key} needs a number.") from None
        low, high = LIMITS[key]
        if not low <= value <= high:
            raise Refused(f"{key} must be between {low} and {high}.")
        out[key] = value
    return out


def need_vram(base_model: str, method: str, config: dict) -> float:
    return trainspec.llm_vram_gb(trainspec.LLMS[base_model][1], method, int(config["max_len"]), int(config["batch_size"]))


def estimate_for(base_model: str, method: str, config: dict, stats: dict, gpu_name: str, price: float) -> dict | None:
    """Minutes and dollars, or None when the speed of this card is not known."""
    speed = trainspec.llm_tokens_per_s(trainspec.LLMS[base_model][1], method, gpu_name)
    if not speed:
        return None
    tokens = max(300.0, (stats.get("mean_chars", 5000) + SHORT_SYSTEM + 2000) / trainspec.CHARS_PER_TOKEN)
    rows = stats.get("train_rows", 0) * float(config["epochs"])
    if config["max_examples"]:
        rows = min(rows, config["max_examples"])
    train = rows / (speed / tokens) / 60
    per_test = 0.25 + trainspec.LLMS[base_model][1] * 0.08
    tests = min(int(config["eval_examples"]), stats.get("test_rows", 0))
    scoring = (tests * per_test / 60 * (2 if config["eval_base"] else 1) + 1.5) if tests else 0
    total = trainspec.SETUP_MIN + train + scoring + trainspec.WIND_DOWN_MIN
    return {"minutes": {"setup": trainspec.SETUP_MIN, "train": round(train, 1), "scoring": round(scoring, 1), "wind_down": trainspec.WIND_DOWN_MIN, "total": round(total, 1)}, "usd": round(total / 60 * price * METER, 3)}


def _offer_query(need: float) -> dict:
    return {"num_gpus": {"eq": 1}, "rentable": {"eq": True}, "verified": {"eq": True}, "reliability2": {"gte": 0.98}, "disk_space": {"gte": 50}, "inet_down": {"gte": 200}, "direct_port_count": {"gte": 1},
            "cuda_max_good": {"gte": 12.9}, "gpu_ram": {"gte": int(need * 1024 * 0.9)}, "type": "on-demand", "order": [["dph_total", "asc"]], "limit": 60}


def offers(base_model: str, method: str, config: dict, stats: dict) -> list[dict]:
    """What can be rented for this job right now, cheapest job first. Mainland China is left out: model downloads are slow or blocked there."""
    need = need_vram(base_model, method, config)
    seen, out = set(), []
    for o in _vast("POST", "/v0/bundles/", _offer_query(need)).get("offers", []):
        spec = trainspec.gpu_spec(o["gpu_name"])
        memory = round(o.get("gpu_ram", 0) / 1024, 1)
        if not spec or not spec[3] or memory < need * 0.9 or str(o.get("geolocation") or "").endswith("CN"):
            continue
        estimate = estimate_for(base_model, method, config, stats, o["gpu_name"], o["dph_total"])
        key = (o["gpu_name"], round(o["dph_total"], 2))
        if estimate and key not in seen:
            seen.add(key)
            out.append({"id": o["id"], "gpu_name": o["gpu_name"], "vram_gb": memory, "price_per_hour": round(o["dph_total"], 3), "where": o.get("geolocation") or "", "reliability": round(o.get("reliability2", 0), 3), "estimate": estimate})
    return sorted(out, key=lambda o: (o["estimate"]["usd"], o["estimate"]["minutes"]["total"]))[:14]


def preview(body: dict, db: Path | None = None) -> dict:
    """The form's estimate: every GPU that fits, and a limit to suggest."""
    base_model, method = body.get("base_model"), body.get("method") or "lora"
    if base_model not in trainspec.LLMS or method not in trainspec.METHODS:
        raise Refused("Choose a model and a method.")
    config = clean_config(body.get("config") or {})
    found = next((d for d in datasets(db) if d["folder"] == body.get("dataset")), None)
    if not found:
        raise Refused("Choose a dataset, or build one on the Collect page.")
    rows = offers(base_model, method, config, found["stats"])
    return {"need_vram_gb": round(need_vram(base_model, method, config), 1), "offers": rows, "suggested_limit": trainspec.suggested_limit(rows[0]["estimate"]["usd"]) if rows else None}


# --------------------------------------------------------------- jobs
def _jobs_row(row) -> dict:
    out = dict(row)
    for key in ("config", "offer", "progress", "result"):
        out[key] = json.loads(out.get(key) or "{}")
    return out


def get(job_id: str, db: Path | None = None) -> dict | None:
    with _db(db) as conn:
        row = conn.execute("SELECT * FROM train_jobs WHERE id = ?", (job_id,)).fetchone()
    return _jobs_row(row) if row else None


def update(job_id: str, db: Path | None = None, **fields) -> None:
    fields["updated"] = time.time()
    values = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
    with _db(db) as conn:
        conn.execute(f"UPDATE train_jobs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*values, job_id))


def event(job_id: str, msg: str, level: str = "info", db: Path | None = None) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
    with _db(db) as conn:
        conn.execute("INSERT INTO train_events VALUES (?,?,?,?)", (job_id, time.time(), level, msg[:1500]))


def listing(db: Path | None = None) -> list[dict]:
    with _db(db) as conn:
        rows = [_jobs_row(r) for r in conn.execute("SELECT * FROM train_jobs ORDER BY created DESC LIMIT 40").fetchall()]
        for row in rows:
            row["loss_spark"] = [m["loss"] for m in conn.execute("SELECT loss FROM (SELECT step, loss FROM train_metrics WHERE job_id = ? ORDER BY step DESC LIMIT 60) ORDER BY step", (row["id"],))]
    return rows


def detail(job_id: str, db: Path | None = None) -> dict | None:
    job = get(job_id, db)
    if not job:
        return None
    with _db(db) as conn:
        job["events"] = [dict(e) for e in conn.execute("SELECT time, level, msg FROM train_events WHERE job_id = ? ORDER BY time", (job_id,))]
        job["metrics"] = [dict(m) for m in conn.execute("SELECT step, examples, loss, lr, rate FROM train_metrics WHERE job_id = ? ORDER BY step", (job_id,))]
    return job


def spend(db: Path | None = None) -> dict:
    start = time.time() - (time.time() % 86400)
    with _db(db) as conn:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) total, COALESCE(SUM(CASE WHEN COALESCE(rented_at, created) >= ? THEN cost_usd ELSE 0 END), 0) today FROM train_jobs", (start,)).fetchone()
    return {"today": round(row["today"], 2), "total": round(row["total"], 2)}


def describe(db: Path | None = None) -> dict:
    models = [{"id": k, "title": v[0], "params_b": v[1], "repo": v[2], "license": v[3], "methods": [m for m in trainspec.METHODS if m != "full" or v[1] <= 1.8]} for k, v in trainspec.LLMS.items()]
    return {"models": models, "methods": trainspec.METHODS, "defaults": DEFAULTS, "limits": LIMITS, "max_limit": MAX_LIMIT, "datasets": datasets(db), "jobs": listing(db), "spend": spend(db), "credit": credit()}


def create(spec: dict, db: Path | None = None, start=None) -> dict:
    """Check the request and start its worker."""
    base_model, method = spec.get("base_model"), spec.get("method") or "lora"
    if base_model not in trainspec.LLMS or method not in trainspec.METHODS or (method == "full" and trainspec.LLMS[base_model][1] > 1.8):
        raise Refused("That model and method do not go together.")
    config = clean_config(spec.get("config") or {})
    found = next((d for d in datasets(db) if d["folder"] == spec.get("dataset")), None)
    if not found:
        raise Refused("Choose a dataset.")
    if found["stats"]["train_rows"] < 8:
        raise Refused("That dataset is too small to train on. Collect more first.")
    offer = spec.get("offer") or {}
    if not offer.get("id") or not offer.get("price_per_hour"):
        raise Refused("Choose a GPU.")
    try:
        limit = float(spec.get("limit_usd"))
    except (TypeError, ValueError):
        raise Refused("Set a spending limit.") from None
    if not 0 < limit <= MAX_LIMIT:
        raise Refused(f"The spending limit must be above $0 and at most ${MAX_LIMIT:.0f}.")
    with _db(db) as conn:
        if conn.execute(f"SELECT 1 FROM train_jobs WHERE state IN ({','.join('?' * len(ACTIVE))})", ACTIVE).fetchone():
            raise Refused("A job is already running. One at a time, so the spending stays easy to follow.")
        job_id = f"ft-{time.strftime('%m%d-%H%M')}-{secrets.token_hex(2)}"
        name = (spec.get("name") or "").strip()[:80] or f"{trainspec.LLMS[base_model][0]} on {found['folder']}"
        now = time.time()
        conn.execute("INSERT INTO train_jobs (id, name, created, updated, state, stage, base_model, method, dataset, config, offer, gpu_name, price_per_hour, limit_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (job_id, name, now, now, "queued", "Waiting to start", base_model, method, found["folder"], json.dumps(config), json.dumps(offer), offer.get("gpu_name"), float(offer["price_per_hour"]), limit))
    event(job_id, f"Asked for {offer.get('gpu_name')} at ${float(offer['price_per_hour']):.3f}/h, limit ${limit:.2f}", db=db)
    (start or _spawn)(job_id)
    return get(job_id, db)


def _spawn(job_id: str) -> None:
    folder = RUNS / job_id
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "worker.log").open("ab") as log:
        subprocess.Popen([sys.executable, "-m", "ts", "train-worker", job_id], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def request_stop(job_id: str, db: Path | None = None) -> dict | None:
    job = get(job_id, db)
    if not job or job["state"] not in ACTIVE:
        return job
    update(job_id, db, stop_requested=time.time(), **({"state": "canceled", "stage": "Canceled", "stop_reason": "asked"} if job["state"] == "queued" and not _alive(job) else {}))
    event(job_id, "Stop requested from the console", db=db)
    return get(job_id, db)


def _alive(job: dict) -> bool:
    pid = job.get("worker_pid") or 0
    if not pid:
        return False
    try:
        return f"train-worker\0{job['id']}" in Path(f"/proc/{pid}/cmdline").read_text()
    except OSError:
        return False


# --------------------------------------------------------------- the machine
class Machine:
    def __init__(self, job_id: str, folder: Path, iid: int, price: float, gpu: str, before: float = 0.0):
        self.job_id, self.folder, self.iid, self.price, self.gpu, self.before = job_id, folder, iid, price, gpu, before  # before: what earlier machines of this job cost
        self.rented_at, self.host, self.port = time.time(), "", 0

    def spent(self) -> float:
        return round(self.before + (time.time() - self.rented_at) / 3600 * self.price * METER, 4)

    def ssh(self) -> list[str]:
        return ["ssh", "-i", str(self.folder / "ssh" / "id_ed25519"), "-p", str(self.port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30",
                "-o", f"UserKnownHostsFile={self.folder / 'ssh' / 'known_hosts'}", "-o", "StrictHostKeyChecking=accept-new", f"root@{self.host}"]

    def run(self, command: str, timeout: int = 120, check: bool = True) -> str:
        done = subprocess.run(self.ssh() + [REMOTE_ENV + command], capture_output=True, text=True, errors="replace", timeout=timeout)
        if check and done.returncode != 0:
            raise RuntimeError(f"on the GPU machine: {(done.stderr or done.stdout)[-300:]}")
        return done.stdout


    def login(self) -> tuple[bool, str]:
        """Whether a login works now, and what came back when it does not."""
        try:
            done = subprocess.run(self.ssh() + ["echo ok"], capture_output=True, text=True, errors="replace", timeout=60)
        except subprocess.TimeoutExpired:
            return False, "the login timed out"
        return done.stdout.strip() == "ok", (done.stderr or done.stdout).strip()[-200:]


def _ssh_key(folder: Path) -> str:
    keys = folder / "ssh"
    keys.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = keys / "id_ed25519"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ts-train", "-f", str(key)], check=True)
    return (keys / "id_ed25519.pub").read_text().strip()


def _addresses(found: dict) -> list[tuple[str, int]]:
    """Where the machine can be logged into: its own address first, then Vast's relay."""
    out = []
    ports = (found.get("ports") or {}).get("22/tcp") or []
    if ports and found.get("public_ipaddr"):
        out.append((found["public_ipaddr"].strip(), int(ports[0]["HostPort"])))
    if found.get("ssh_host") and found.get("ssh_port"):
        out.append((found["ssh_host"], int(found["ssh_port"])))
    return out


def _wait_ready(machine: Machine, job: dict) -> None:
    """Until a login works. Every way this can fail says what the machine last answered."""
    started = time.time()
    status, answer, pull_errors, refused_since, keyed_again = None, "", 0, 0.0, False
    while time.time() - started < READY_LIMIT_S:
        if _should_abort(job, machine):
            raise RuntimeError("stopped while the machine was starting")
        found = _vast("GET", f"/v0/instances/{machine.iid}/").get("instances") or {}
        status, note = found.get("actual_status"), str(found.get("status_msg") or "")
        pull_errors = pull_errors + 1 if "Error response from daemon" in note else 0
        if pull_errors >= 4:
            raise RuntimeError(f"this machine cannot download the software: {note[:120]}")
        if status == "running":
            for machine.host, machine.port in _addresses(found):
                worked, answer = machine.login()
                if worked:
                    return
            if "denied" in answer.lower():
                refused_since = refused_since or time.time()
                if not keyed_again:
                    keyed_again = True
                    event(job["id"], "The machine is up but refused the login; sending it the key again", "warn")
                    try:
                        _vast("POST", f"/v0/instances/{machine.iid}/ssh/", {"ssh_key": (machine.folder / "ssh" / "id_ed25519.pub").read_text().strip()})
                    except (OSError, RuntimeError) as exc:
                        event(job["id"], f"Sending the key again did not go through: {exc}", "warn")
                elif time.time() - refused_since > REFUSED_LIMIT_S:
                    raise RuntimeError(f"the machine kept refusing the login: {answer}")
        if status in ("exited", "offline") and time.time() - started > 300:
            raise RuntimeError(f"the machine went {status}")
        time.sleep(15)
    raise RuntimeError(f"the machine did not become reachable in time (last status {status or 'unknown'}; last login answer: {answer or 'none'})")


def _should_abort(job: dict, machine: Machine | None) -> bool:
    now = get(job["id"]) or {}
    return bool(now.get("stop_requested")) or bool(machine and machine.spent() >= job["limit_usd"])


def _rent(job: dict, folder: Path) -> Machine:
    """The chosen offer, or the next cheapest of no higher price if it has gone. Offers are looked up afresh for each try: a machine that fails can take many minutes."""
    pub = _ssh_key(folder)
    choice = job["offer"]
    need = need_vram(job["base_model"], job["method"], job["config"])
    before, tried, why = 0.0, set(), "no GPU at that price is available now"
    for _ in range(RENT_TRIES):
        fresh = [o for o in _vast("POST", "/v0/bundles/", _offer_query(need)).get("offers", []) if o["id"] not in tried and o["dph_total"] <= choice["price_per_hour"] * 1.15
                 and (trainspec.gpu_spec(o["gpu_name"]) or (0, 0, 0, False))[3] and not str(o.get("geolocation") or "").endswith("CN")]
        if not fresh:
            break
        offer = min(fresh, key=lambda o: (o["id"] != choice["id"], o["dph_total"]))
        tried.add(offer["id"])
        event(job["id"], f"Renting {offer['gpu_name']} at ${offer['dph_total']:.3f}/h in {offer.get('geolocation')}")
        try:
            iid = _vast("PUT", f"/v0/asks/{offer['id']}/", {"client_id": "me", "image": IMAGE, "disk": 40, "label": LABEL + job["id"], "runtype": "ssh_direc ssh_proxy", "onstart": ONSTART, "env": {"HF_HUB_DISABLE_TELEMETRY": "1"}}).get("new_contract")
        except RuntimeError as exc:
            why = str(exc)
            iid = None
        if not iid:
            event(job["id"], "That offer was taken; trying the next one", "warn")
            continue
        machine = Machine(job["id"], folder, iid, float(offer["dph_total"]), offer["gpu_name"], before)
        update(job["id"], instance=iid, rented_at=machine.rented_at, gpu_name=offer["gpu_name"], price_per_hour=machine.price, machine="rented", stage="Starting the GPU")
        try:
            _vast("POST", f"/v0/instances/{iid}/ssh/", {"ssh_key": pub})
            _wait_ready(machine, job)
            return machine
        except Exception as exc:
            why = str(exc)
            event(job["id"], f"This machine did not work out: {exc}", "warn")
            _destroy(machine, job)
            before = machine.spent()
            if before >= job["limit_usd"] or _should_abort(job, None):
                raise
    raise RuntimeError(f"none of the offered machines started: {why}")


def _destroy(machine: Machine, job: dict) -> bool:
    """Delete it and check that it is gone."""
    for _ in range(8):
        try:
            _vast("DELETE", f"/v0/instances/{machine.iid}/")
        except (OSError, RuntimeError) as exc:
            event(job["id"], f"Delete did not go through yet: {exc}", "warn")
        time.sleep(5)
        try:
            live = [i for i in _vast("GET", "/v1/instances/").get("instances") or [] if i.get("id") == machine.iid]
        except (OSError, RuntimeError):
            continue
        if not live:
            cost = machine.spent()
            update(job["id"], released_at=time.time(), machine="deleted", cost_usd=cost)
            event(job["id"], f"GPU deleted. This job cost ${cost:.2f}")
            return True
    update(job["id"], machine="check", error="The GPU may still exist. Check the Vast account.")
    event(job["id"], f"The GPU {machine.iid} may still exist. Check the Vast account.", "error")
    return False


def _bundle(job: dict, folder: Path, root: Path | None = None) -> dict[str, Path]:
    """The job's own copy of the data, with its example caps applied."""
    source = (root or DATASETS) / job["dataset"]
    data = folder / "data"
    data.mkdir(parents=True, exist_ok=True)
    picker = random.Random(0)
    train = (source / "train.jsonl").read_text().splitlines()
    test = (source / "test.jsonl").read_text().splitlines()
    picker.shuffle(train)
    picker.shuffle(test)
    if job["config"]["max_examples"]:
        train = train[:job["config"]["max_examples"]]
    test = test[:job["config"]["eval_examples"]]
    with gzip.open(data / "train.jsonl.gz", "wt") as out:
        out.write("\n".join(train) + "\n")
    (data / "eval.jsonl").write_text("\n".join(test) + ("\n" if test else ""))
    shutil.copy(source / "tools.json", data / "tools.json")
    return {f"data/{p.name}": p for p in data.iterdir()} | {name: TRAINER / name for name in ("llm_train.py", "score.py", "job.sh")}


def _upload(machine: Machine, files: dict[str, Path]) -> None:
    folder = machine.folder / "upload"
    shutil.rmtree(folder, ignore_errors=True)
    for arc, path in files.items():
        (folder / arc).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, folder / arc)
    pack = subprocess.Popen(["tar", "cf", "-", "-C", str(folder), "."], stdout=subprocess.PIPE)
    send = subprocess.run(machine.ssh() + ["mkdir -p /root/job && tar xf - -C /root/job"], stdin=pack.stdout, capture_output=True, timeout=900)
    pack.wait()
    if send.returncode != 0:
        raise RuntimeError(f"upload failed: {send.stderr[-200:]!r}")


def _command(job: dict, deadline: float) -> str:
    c = job["config"]
    repo = trainspec.LLMS[job["base_model"]][2]
    env = {"REPO": repo, "METHOD": job["method"], "EPOCHS": c["epochs"], "LR": c["lr"], "WARMUP": 20, "BATCH": c["batch_size"], "ACCUM": c["grad_accum"], "MAX_LEN": c["max_len"], "RANK": c["lora_rank"], "ALPHA": c["lora_rank"] * 2,
           "TARGETS": "all-linear", "SAVE_EVERY": 120, "SEED": 0, "EVAL_BASE": int(c["eval_base"]), "TRAIN_DEADLINE": f"{deadline:.0f}"}
    assigns = " ".join(f"{k}={v}" for k, v in env.items())
    return f"cd /root/job && rm -f STOP && touch /root/.deadman && ({assigns} setsid nohup bash job.sh >> job.log 2>&1 < /dev/null &); echo started"


STAGES = {"setup": "Installing what it needs", "load": "Loading the model", "tokenize": "Preparing the examples", "probe": "Checking it fits on the GPU", "eval_base": "Scoring the untrained model", "train": "Training", "eval": "Scoring the trained model", "done": "Finished", "stopped": "Stopped"}


def _read_state(machine: Machine, seen: int) -> dict:
    text = machine.run(f"touch /root/.deadman; cd /root/job; cat status 2>/dev/null; echo @@; cat progress.json 2>/dev/null; echo @@; pgrep -f '[j]ob.sh' >/dev/null && echo alive || echo gone; echo @@; tail -n +{seen + 1} out/metrics.jsonl 2>/dev/null", 60)
    parts = text.split("@@")
    try:
        progress = json.loads(parts[1].strip() or "{}")
    except ValueError:
        progress = {}
    metrics = []
    for line in parts[3].strip().splitlines():
        try:
            metrics.append(json.loads(line))
        except ValueError:
            break
    return {"status": parts[0].strip(), "progress": progress, "alive": "alive" in parts[2], "metrics": metrics}


def _store_metrics(job_id: str, rows: list[dict]) -> None:
    with _db() as conn:
        conn.executemany("INSERT OR REPLACE INTO train_metrics VALUES (?,?,?,?,?,?,?)", [(job_id, r["step"], r.get("examples"), r.get("loss"), r.get("lr"), r.get("ex_per_s"), r.get("t", time.time())) for r in rows if "step" in r])


def _store_metrics_file(job_id: str, folder: Path) -> None:
    """Everything the trainer wrote, from the copy brought home: a value written after the last look at the machine still counts."""
    path = folder / "out" / "metrics.jsonl"
    rows = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    _store_metrics(job_id, rows)


def _watch(machine: Machine, job: dict) -> str:
    """Follow the trainer until it ends, stopping it before the money runs out."""
    job_id, limit = job["id"], job["limit_usd"]
    reserve = machine.price * METER * WIND_DOWN_S / 3600
    seen, last_status, last_ok, stop_at, why = 0, "", time.time(), 0.0, ""
    while True:
        time.sleep(POLL)
        spent = machine.spent()
        update(job_id, cost_usd=spent)
        now = get(job_id) or job
        if not stop_at and (now.get("stop_requested") or spent >= limit - reserve):
            why = "asked" if now.get("stop_requested") else "limit"
            event(job_id, "Stopping as asked: saving what it has" if why == "asked" else f"Reached ${spent:.2f} of the ${limit:.2f} limit: saving what it has", "warn")
            machine.run("touch /root/job/STOP", 60, check=False)
            stop_at = time.time()
            update(job_id, state="stopping", stop_reason=why, stage="Stopping and saving")
        if spent >= limit or (stop_at and time.time() - stop_at > STOP_GRACE_S):
            event(job_id, "The stop is taking too long or the limit is reached: ending now", "warn")
            return why or "limit"
        try:
            state = _read_state(machine, seen)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            if time.time() - last_ok > LOST_AFTER_S:
                raise RuntimeError(f"lost contact with the GPU machine: {exc}") from None
            continue
        last_ok = time.time()
        if state["metrics"]:
            _store_metrics(job_id, state["metrics"])
            seen += len(state["metrics"])
        status = state["status"]
        if status != last_status:
            event(job_id, STAGES.get(status, status))
            last_status = status
        update(job_id, progress=state["progress"], stage=STAGES.get(status, status))
        if status.startswith("failed"):
            raise RuntimeError(status)
        if status in ("done", "stopped"):
            return why or "finished"
        if not state["alive"] and status:
            raise RuntimeError(f"the training stopped by itself during: {status}")


def _bring_home(machine: Machine, job: dict) -> None:
    """Copy what was made back here, checking every model file against the machine's own checksums."""
    home = machine.folder / "out"
    want = {}
    listed = machine.run("cd /root/job/out && find adapter model -type f -exec sha256sum {} + 2>/dev/null", 300, check=False)
    for line in listed.splitlines():
        digest, name = line.split(None, 1)
        want[name.strip()] = digest
    for attempt in range(3):
        shutil.rmtree(home, ignore_errors=True)
        home.mkdir(parents=True)
        reader = subprocess.Popen(machine.ssh() + ["tar cf - -C /root/job out job.log"], stdout=subprocess.PIPE)
        subprocess.run(["tar", "xf", "-", "-C", str(machine.folder)], stdin=reader.stdout, timeout=1200, check=False)
        reader.wait()
        bad = [n for n, d in want.items() if subprocess.run(["sha256sum", str(home / n)], capture_output=True, text=True).stdout.split(" ")[0] != d]
        if not bad:
            event(job["id"], f"Copied the model home, {len(want)} files, every checksum matched")
            return
        event(job["id"], f"{len(bad)} files came back different, trying again ({attempt + 1})", "warn")
    raise RuntimeError("the model could not be copied home intact")


def _summary(folder: Path, name: str) -> dict | None:
    try:
        return json.loads((folder / "out" / name / "summary.json").read_text())
    except (OSError, ValueError):
        return None


def _copy_last_score(folder: Path) -> dict | None:
    """What simply repeating the last call would score, so a model's number has something to beat."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ts_trainer_score", TRAINER / "score.py")
        score = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(score)
        rows = score.load_rows(str(folder / "data" / "eval.jsonl"), str(folder / "data" / "tools.json"))
        return score.report(rows, score.copy_last(rows), 0)[0] if rows else None
    except Exception as exc:  # the baseline is a nicety; the job's result does not depend on it
        print(f"copy-last baseline: {type(exc).__name__}: {exc}", flush=True)
        return None


def result_of(folder: Path) -> dict:
    try:
        trained = json.loads((folder / "out" / "train_summary.json").read_text())
    except (OSError, ValueError):
        trained = {}
    return {"base": _summary(folder, "eval-base"), "tuned": _summary(folder, "eval-tuned"), "copy_last": _copy_last_score(folder), "train": trained}


def run_job(job_id: str) -> int:
    """The worker: everything between asking for a GPU and that GPU being gone."""
    job = get(job_id)
    if not job or job["state"] != "queued":
        return 1
    signal.signal(signal.SIGTERM, lambda *_a: sys.exit(1))  # so a plain kill still reaches the finally below and deletes the GPU
    folder = RUNS / job_id
    folder.mkdir(parents=True, exist_ok=True)
    update(job_id, state="running", stage="Getting the data ready", worker_pid=os.getpid())
    machine = None
    try:
        files = _bundle(job, folder)
        job = get(job_id)
        machine = _rent(job, folder)
        update(job_id, stage="Checking the GPU")
        event(job_id, "GPU: " + machine.run("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader", 60).strip())
        _upload(machine, files)
        reserve = machine.price * METER * WIND_DOWN_S / 3600
        deadline = machine.rented_at + max(60.0, (job["limit_usd"] - reserve - machine.before) / (machine.price * METER) * 3600)
        machine.run(_command(job, deadline), 60)
        why = _watch(machine, job)
        _bring_home(machine, job)
        _store_metrics_file(job_id, folder)
        state, stage = ("done", "Finished") if why == "finished" else ("stopped", "Stopped at the limit" if why == "limit" else "Stopped")
        update(job_id, state=state, stage=stage, stop_reason="" if why == "finished" else why, result=result_of(folder))
        return 0
    except Exception as exc:
        event(job_id, f"Failed: {exc}", "error")
        update(job_id, state="failed", stage="Failed", error=str(exc)[:600])
        return 1
    finally:
        if machine:
            _destroy(machine, job)
            shutil.rmtree(folder / "upload", ignore_errors=True)


def sweep(log=print) -> list[int]:
    """Delete any GPU labelled as ours whose job nobody is looking after any more. Run while the console is up."""
    removed = []
    for found in _vast("GET", "/v1/instances/").get("instances") or []:
        label = str(found.get("label") or "")
        if not label.startswith(LABEL):
            continue
        job_id = label[len(LABEL):]
        job = get(job_id) if JOB_ID.fullmatch(job_id) else None
        if job and job["state"] in ACTIVE and _alive(job):
            continue
        _vast("DELETE", f"/v0/instances/{found['id']}/")
        removed.append(found["id"])
        log(f"deleted GPU {found['id']}, left behind by {job_id}")
        if job:
            update(job_id, machine="deleted", released_at=time.time(), **({"state": "failed", "stage": "Failed", "error": "Its worker stopped, so the GPU was deleted."} if job["state"] in ACTIVE else {}))
            event(job_id, "The worker was gone, so the GPU was deleted by the sweep", "warn")
    return removed


def _fail_dead_workers() -> None:
    """A job marked running with no worker and no machine would otherwise say running for ever."""
    with _db() as conn:
        rows = [_jobs_row(r) for r in conn.execute(f"SELECT * FROM train_jobs WHERE state IN ({','.join('?' * len(ACTIVE))})", ACTIVE).fetchall()]
    for job in rows:
        if time.time() - job["created"] > 90 and not _alive(job):
            update(job["id"], state="failed", stage="Failed", error="Its worker stopped.")
            event(job["id"], "The worker stopped, so this job has ended", "error")


def sweep_forever(log=print) -> None:
    while True:
        try:
            _fail_dead_workers()
            sweep(log)
        except Exception as exc:  # a failed sweep must not take the console with it; the next one tries again
            log(f"training sweep: {type(exc).__name__}: {str(exc)[:200]}")
        time.sleep(300)

