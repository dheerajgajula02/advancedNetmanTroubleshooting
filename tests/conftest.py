import os

os.environ["TS_CAPTURE"] = "off"  # tests must never write to the real training database
os.environ["TS_EMBED"] = "off"    # nor call the GPU machine
import tempfile  # noqa: E402

_ai = tempfile.mkdtemp(prefix="ts-tests-ai-")
os.environ["TS_AI_SETTINGS"], os.environ["TS_AI_KEYS"], os.environ["TS_AI_CATALOG"] = f"{_ai}/settings.json", f"{_ai}/keys.json", f"{_ai}/catalog.json"  # nor change which models are in use
os.environ["TS_JENKINS_INDEX"] = f"{_ai}/jenkins.sqlite"  # nor the AI's index of Jenkins builds
os.environ["TS_AI_USAGE"] = "off"

import copy
import shutil
from pathlib import Path

import pytest

from tests import fake_blocks
from ts.registry import load_blocks
from ts.schema import parse_rule

load_blocks()
fake_blocks.register()

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _first_look_stays_home(monkeypatch):
    """Comparing configs reads real devices and asking about deployments reaches Jenkins."""
    from ts import firstlook
    monkeypatch.setattr(firstlook, "_config_differences", lambda: ([], []))
    monkeypatch.setattr(firstlook, "_deploys", lambda: [])


@pytest.fixture(autouse=True)
def _memories_stay_home(tmp_path, monkeypatch):
    """Nothing a test does may end up in what a real agent remembers."""
    from drills import office
    from ts import brain
    monkeypatch.setattr(brain, "DB", tmp_path / "brains.sqlite")
    monkeypatch.setattr(office, "BRAINS", tmp_path / "people-brains.sqlite")


def diag(rule_id="test-diag", event="interface.down", steps=None, **extra):
    data = {"id": rule_id, "table": "diagnose", "priority": 50, "description": "a rule written for a test",
            "match": {"event": event}, "source": {"by": "test"},
            "steps": steps or [{"check": "interface_status {device} {interface}",
                                "then": {"admin_down": "cause interface.admin_shut"}}]}
    data.update(extra)
    return data


def fix(rule_id="test-fix", cause="interface.admin_shut", **extra):
    data = {"id": rule_id, "table": "fix", "priority": 50, "description": "a fix written for a test",
            "match": {"cause": cause}, "source": {"by": "test"},
            "require": ["change_window_open == closed"],
            "action": {"do": {"name": "config_lines", "device": "{device}",
                              "lines": ["interface {interface}", "no shutdown"]}},
            "verify": ["interface_status {device} {interface} == up"], "risk": "low"}
    data.update(extra)
    return data


def rules(*dicts):
    return [parse_rule(copy.deepcopy(d)) for d in dicts]


@pytest.fixture
def rules_dir(tmp_path):
    dest = tmp_path / "rules"
    shutil.copytree(REPO / "rules", dest)
    return dest
