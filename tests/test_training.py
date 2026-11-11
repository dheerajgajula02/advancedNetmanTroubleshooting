"""Fine-tuning: what may start, what it may spend, and that the GPU always goes away."""

import importlib.util
import json
import subprocess

import pytest

from ts import dataset, history, trainspec, training

OFFER = {"id": 7, "gpu_name": "RTX 4090", "price_per_hour": 0.4}
SPEC = {"base_model": "qwen3-0.6b", "method": "lora", "dataset": "first-v1", "config": {}, "offer": OFFER, "limit_usd": 2}


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = tmp_path / "history.sqlite"
    monkeypatch.setattr(history, "DB", db)
    monkeypatch.setattr(training, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(training, "DATASETS", tmp_path / "sets")
    monkeypatch.setattr(training, "POLL", 0)
    monkeypatch.setattr(training.time, "sleep", lambda _s: None)
    folder = tmp_path / "sets" / "first-v1"
    folder.mkdir(parents=True)
    row = json.dumps({"query": "Task: x", "answers": [{"name": "show", "arguments": {}}], "weight": 1, "meta": {"episode_id": "e", "step": 1, "decision": "", "need_act": True}})
    (folder / "train.jsonl").write_text((row + "\n") * 12)
    (folder / "test.jsonl").write_text(row + "\n")
    (folder / "tools.json").write_text(json.dumps({"tools": {"system": "s", "tools": [{"name": "show", "args": []}]}}))
    with training._db() as conn:
        conn.execute("INSERT INTO train_datasets VALUES ('first-v1','first',1,1.0,'graded',?)", (json.dumps({"train_rows": 12, "train_unique": 12, "test_rows": 1, "mean_chars": 100, "runs_solved": 1}),))
    return tmp_path


def started(world, **change):
    return training.create({**SPEC, **change}, start=lambda _job: None)


@pytest.mark.parametrize("change,word", [({"limit_usd": 0}, "limit"), ({"limit_usd": None}, "limit"), ({"limit_usd": 500}, "limit"), ({"offer": {}}, "GPU"), ({"dataset": "nope"}, "dataset"), ({"base_model": "gpt-9"}, "model")])
def test_a_job_needs_a_limit_a_gpu_and_real_data(world, change, word):
    with pytest.raises(training.Refused, match=word):
        started(world, **change)


def test_a_full_fine_tune_is_refused_on_a_big_model(world):
    with pytest.raises(training.Refused):
        started(world, base_model="qwen3-8b", method="full")


def test_only_one_job_runs_at_a_time(world):
    started(world)
    with pytest.raises(training.Refused, match="already running"):
        started(world)


def test_settings_outside_their_range_are_refused(world):
    with pytest.raises(training.Refused, match="epochs"):
        started(world, config={"epochs": 500})


class Vast:
    def __init__(self):
        self.live, self.deleted, self.sent = {}, [], []

    def __call__(self, method, path, body=None, timeout=60):
        self.sent.append((method, path))
        if path == "/v0/bundles/":
            return {"offers": [{"id": 7, "gpu_name": "RTX 4090", "dph_total": 0.4, "gpu_ram": 24000, "geolocation": "US", "reliability2": 0.99}]}
        if method == "PUT" and path.startswith("/v0/asks/"):
            self.live[101] = {"id": 101, "label": body["label"]}
            return {"new_contract": 101}
        if method == "GET" and path == "/v1/instances/":
            return {"instances": list(self.live.values())}
        if method == "GET" and path.startswith("/v0/instances/"):
            return {"instances": {"actual_status": "running", "public_ipaddr": "1.2.3.4", "ports": {"22/tcp": [{"HostPort": "2200"}]}}}
        if method == "DELETE":
            iid = int(path.rstrip("/").split("/")[-1])
            self.deleted.append(iid)
            self.live.pop(iid, None)
        return {}


def fake_machine(monkeypatch, status="done", training_crashes=False):
    calls = []

    def run(self, command, timeout=120, check=True):
        calls.append(command)
        if command == "echo ok":
            return "ok\n"
        if "nvidia-smi" in command:
            return "RTX 4090, 24564 MiB\n"
        if training_crashes and "job.sh >>" in command:
            raise RuntimeError("the machine fell over")
        if "cat status" in command:
            return f"{status}\n@@\n{{}}\n@@\nalive\n@@\n"
        return ""
    monkeypatch.setattr(training.Machine, "run", run)
    monkeypatch.setattr(training.Machine, "login", lambda self: (True, ""))
    monkeypatch.setattr(training, "_upload", lambda *_a: None)
    monkeypatch.setattr(training, "_bring_home", lambda *_a: None)
    monkeypatch.setattr(training, "_ssh_key", lambda _f: "key")
    return calls


def test_the_gpu_is_deleted_when_the_job_finishes(world, monkeypatch):
    vast = Vast()
    monkeypatch.setattr(training, "_vast", vast)
    fake_machine(monkeypatch)
    job = started(world)
    assert training.run_job(job["id"]) == 0
    assert vast.deleted == [101] and not vast.live
    done = training.get(job["id"])
    assert done["state"] == "done" and done["machine"] == "deleted" and done["released_at"]


def test_the_gpu_is_deleted_when_training_fails(world, monkeypatch):
    vast = Vast()
    monkeypatch.setattr(training, "_vast", vast)
    fake_machine(monkeypatch, training_crashes=True)
    job = started(world)
    assert training.run_job(job["id"]) == 1
    assert vast.deleted == [101]
    found = training.get(job["id"])
    assert found["state"] == "failed" and "fell over" in found["error"]


def test_training_stops_before_the_limit_and_keeps_the_model(world, monkeypatch):
    vast = Vast()
    monkeypatch.setattr(training, "_vast", vast)
    calls = fake_machine(monkeypatch, status="stopped")
    spent = iter([0.1, 1.99, 2.5])
    monkeypatch.setattr(training.Machine, "spent", lambda self: next(spent, 2.5))
    job = started(world)
    assert training.run_job(job["id"]) == 0
    assert any("touch /root/job/STOP" in c for c in calls)
    found = training.get(job["id"])
    assert found["state"] == "stopped" and found["stop_reason"] == "limit" and vast.deleted == [101]


def test_going_over_the_limit_ends_the_job_even_if_the_machine_never_stops(world, monkeypatch):
    vast = Vast()
    monkeypatch.setattr(training, "_vast", vast)
    calls = fake_machine(monkeypatch, status="train")
    monkeypatch.setattr(training.Machine, "spent", lambda self: 5.0 if any("job.sh >>" in c for c in calls) else 0.1)
    job = started(world)
    training.run_job(job["id"])
    assert vast.deleted == [101] and training.get(job["id"])["stop_reason"] == "limit"


def test_a_job_does_not_rent_again_once_the_limit_is_spent(world, monkeypatch):
    vast = Vast()
    monkeypatch.setattr(training, "_vast", vast)
    fake_machine(monkeypatch)
    monkeypatch.setattr(training.Machine, "spent", lambda self: 5.0)
    job = started(world)
    assert training.run_job(job["id"]) == 1
    assert sum(1 for m, p in vast.sent if m == "PUT") == 1 and vast.deleted == [101]


def test_stop_from_the_console_reaches_the_trainer(world, monkeypatch):
    vast = Vast()
    monkeypatch.setattr(training, "_vast", vast)
    calls = fake_machine(monkeypatch, status="stopped")
    job = started(world)
    training.update(job["id"], stop_requested=1.0)
    monkeypatch.setattr(training, "_should_abort", lambda *_a: False)
    training.run_job(job["id"])
    assert any("STOP" in c for c in calls) and training.get(job["id"])["stop_reason"] == "asked"


def test_the_sweep_deletes_only_our_abandoned_machines(world, monkeypatch):
    vast = Vast()
    monkeypatch.setattr(training, "_vast", vast)
    job = started(world)
    vast.live = {1: {"id": 1, "label": training.LABEL + job["id"]}, 2: {"id": 2, "label": "someone-elses"}}
    assert training.sweep(lambda _m: None) == [1]
    assert vast.deleted == [1] and training.get(job["id"])["state"] == "failed"


def test_the_sweep_leaves_a_job_that_is_being_looked_after(world, monkeypatch):
    vast = Vast()
    monkeypatch.setattr(training, "_vast", vast)
    monkeypatch.setattr(training, "_alive", lambda _j: True)
    job = started(world)
    vast.live = {1: {"id": 1, "label": training.LABEL + job["id"]}}
    assert training.sweep(lambda _m: None) == [] and not vast.deleted


def test_gpus_we_cannot_rate_or_that_are_too_small_are_not_offered(world, monkeypatch):
    bundle = [{"id": 1, "gpu_name": "RTX 4090", "dph_total": 0.4, "gpu_ram": 24000, "geolocation": "US"}, {"id": 2, "gpu_name": "Mystery Card", "dph_total": 0.1, "gpu_ram": 24000, "geolocation": "US"},
              {"id": 3, "gpu_name": "RTX 4090", "dph_total": 0.2, "gpu_ram": 24000, "geolocation": "Beijing, CN"}, {"id": 4, "gpu_name": "RTX 3090", "dph_total": 0.1, "gpu_ram": 4000, "geolocation": "US"}]
    monkeypatch.setattr(training, "_vast", lambda *_a, **_k: {"offers": bundle})
    found = training.offers("qwen3-0.6b", "lora", training.clean_config({}), {"train_rows": 100, "test_rows": 10, "mean_chars": 3000})
    assert [o["id"] for o in found] == [1]


def test_a_training_example_leaves_out_what_is_known_from_the_past():
    task = "INCIDENT at noon\nCONCLUSION (escalate): look\n\nSIMILAR PAST RUNS\nlots of history\nEVERY PAST RUN\nmore"
    assert dataset.short_task(task) == "INCIDENT at noon\nCONCLUSION (escalate): look"


def test_the_estimate_covers_setup_training_and_scoring():
    found = training.estimate_for("qwen3-0.6b", "lora", training.clean_config({}), {"train_rows": 200, "test_rows": 40, "mean_chars": 3000}, "RTX 4090", 0.4)
    assert found["minutes"]["total"] > found["minutes"]["train"] > 0 and found["usd"] > 0
    assert training.estimate_for("qwen3-0.6b", "lora", training.clean_config({}), {}, "Mystery Card", 0.4) is None


def test_model_figures_came_across():
    assert trainspec.LLMS["qwen3-0.6b"][2] == "Qwen/Qwen3-0.6B" and trainspec.gpu_spec("RTX 4090")[3]


def _score():
    spec = importlib.util.spec_from_file_location("score", training.TRAINER / "score.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scoring_knows_this_projects_changes_and_last_step():
    score = _score()
    assert score.is_change({"name": "try_config", "arguments": {}}) and score.is_change({"name": "action_x", "arguments": {}}) and not score.is_change({"name": "show", "arguments": {}})
    row = {"answers": [{"name": "finish", "arguments": {"outcome": "fixed"}}], "meta": {"decision": "report"}}
    assert score.score(row, {"name": "finish", "arguments": {"outcome": "fixed"}}, [])["report_ok"]


def test_the_job_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(training.TRAINER / "job.sh")]).returncode == 0


class Offers(Vast):
    """Vast with two offers, the first of which is gone by the time it is asked for."""
    def __call__(self, method, path, body=None, timeout=60):
        if method == "POST" and path == "/v0/bundles/":
            self.sent.append((method, path))
            return {"offers": [{"id": 7, "gpu_name": "RTX 4090", "dph_total": 0.4, "geolocation": "US"}, {"id": 8, "gpu_name": "RTX 4090", "dph_total": 0.41, "geolocation": "US"}]}
        if method == "PUT" and path == "/v0/asks/7/":
            self.sent.append((method, path))
            raise RuntimeError("Vast PUT /v0/asks/7/: HTTP 400 no_such_ask")
        return super().__call__(method, path, body, timeout)


def test_an_offer_that_has_gone_does_not_end_the_job(world, monkeypatch):
    vast = Offers()
    monkeypatch.setattr(training, "_vast", vast)
    fake_machine(monkeypatch)
    job = started(world)
    assert training.run_job(job["id"]) == 0
    puts = [p for m, p in vast.sent if m == "PUT"]
    assert puts[0] == "/v0/asks/7/" and puts[1] == "/v0/asks/8/" and vast.deleted == [101]


def waiting(world, monkeypatch, found, answers):
    """_wait_ready against a machine that gives these login answers, one for each address tried."""
    posts = []
    def vast(method, path, body=None, timeout=60):
        if method == "POST":
            posts.append(path)
            return {}
        return {"instances": found}
    monkeypatch.setattr(training, "_vast", vast)
    replies = iter(answers)
    monkeypatch.setattr(training.Machine, "login", lambda self: next(replies))
    (world / "m" / "ssh").mkdir(parents=True)
    (world / "m" / "ssh" / "id_ed25519.pub").write_text("key")
    job = started(world)
    return training.Machine(job["id"], world / "m", 5, 0.1, "RTX 4090"), job, posts


UP = {"actual_status": "running", "public_ipaddr": "198.51.100.7", "ports": {"22/tcp": [{"HostPort": "4000"}]}, "ssh_host": "ssh1.example", "ssh_port": 5000}


def test_the_relay_address_is_tried_when_the_direct_one_fails(world, monkeypatch):
    machine, job, _ = waiting(world, monkeypatch, UP, [(False, "Connection refused"), (True, "")])
    training._wait_ready(machine, job)
    assert (machine.host, machine.port) == ("ssh1.example", 5000)


def test_a_refused_login_sends_the_key_once_then_gives_up_saying_why(world, monkeypatch):
    clock = iter(range(0, 100000, 200))
    monkeypatch.setattr(training.time, "time", lambda: float(next(clock)))
    machine, job, posts = waiting(world, monkeypatch, UP, [(False, "root@x: Permission denied (publickey).")] * 200)
    monkeypatch.setattr(training, "_should_abort", lambda *_a: False)
    with pytest.raises(RuntimeError, match="kept refusing the login: root@x: Permission denied"):
        training._wait_ready(machine, job)
    assert posts == ["/v0/instances/5/ssh/"]


def test_a_machine_that_cannot_download_the_software_is_given_up_on(world, monkeypatch):
    machine, job, _ = waiting(world, monkeypatch, {"actual_status": "loading", "status_msg": "Error response from daemon: pull failed"}, [])
    with pytest.raises(RuntimeError, match="cannot download"):
        training._wait_ready(machine, job)


def test_loss_values_are_read_from_what_the_machine_really_prints(world, monkeypatch):
    printed = 'train\n@@\n{"pct": 10}\n@@\nalive\n@@\n{"step": 1, "loss": 1.5, "examples": 2}\n{"step": 10, "loss": 1.2, "examples": 20}\n'
    monkeypatch.setattr(training.Machine, "run", lambda self, *_a, **_k: printed)
    state = training._read_state(training.Machine("x", world, 1, 0.1, "g"), 0)
    assert [m["step"] for m in state["metrics"]] == [1, 10] and state["progress"] == {"pct": 10} and state["alive"]


def test_a_half_written_last_loss_line_waits_for_the_next_look(world, monkeypatch):
    printed = 'train\n@@\n{}\n@@\nalive\n@@\n{"step": 1, "loss": 1.5}\n{"step": 10, "lo'
    monkeypatch.setattr(training.Machine, "run", lambda self, *_a, **_k: printed)
    assert [m["step"] for m in training._read_state(training.Machine("x", world, 1, 0.1, "g"), 0)["metrics"]] == [1]


def test_the_loss_file_brought_home_fills_the_chart(world, monkeypatch):
    job = started(world)
    (world / "j" / "out").mkdir(parents=True)
    (world / "j" / "out" / "metrics.jsonl").write_text('{"step": 1, "loss": 1.5, "examples": 2}\n{"step": 10, "loss": 1.2, "examples": 20}\nnot json\n')
    training._store_metrics_file(job["id"], world / "j")
    assert [m["loss"] for m in training.detail(job["id"])["metrics"]] == [1.5, 1.2]
