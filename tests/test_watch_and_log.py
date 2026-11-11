"""The device log check's config-session logic and the trap-log follower, without the lab."""

import calendar
import os
import time

from ts.blocks import checks
from ts.engine import Context
from ts.intake import follow


def utc(hms):  # an epoch for 2026-11-06 at hh:mm:ss UTC
    return calendar.timegm(time.strptime(f"2026-11-06 {hms}", "%Y-%m-%d %H:%M:%S"))


def run_log(monkeypatch, lines, trap="23:56:57", interface="Ethernet1"):
    monkeypatch.setattr(checks.lab, "eos", lambda device, cmd: (f"{device}# {cmd}", "\n".join(lines), 0))
    return checks.device_log(Context(utc(trap)), device="s3", interface=interface)


DOWN = "Nov  6 23:56:57 s3 Ebra: %LINEPROTO-5-UPDOWN: Line protocol on Interface Ethernet1 (To-R3-Border), changed state to down"
ENTER = "Nov  6 23:56:50 s3 ConfigAgent: %SYS-5-CONFIG_E: Enter configuration mode from console by admin on vty4 (172.20.20.61)"
SAVED = "Nov  6 23:57:26 s3 ConfigAgent: %SYS-5-CONFIG_I: Configured from console by admin on vty4 (172.20.20.61)"


def test_change_typed_at_a_prompt(monkeypatch):
    r = run_log(monkeypatch, [ENTER, DOWN, SAVED])
    assert r.outcome == "config_change"
    assert r.values["changed_by"] == "admin on vty4 (172.20.20.61, Guacamole)"
    assert "left config mode" in r.saw


def test_session_still_open(monkeypatch):
    r = run_log(monkeypatch, [ENTER, DOWN])
    assert r.outcome == "config_change" and "still in config mode" in r.saw


def test_one_shot_change(monkeypatch):
    one_shot = "Nov  6 23:56:57 s3 ConfigAgent: %SYS-5-CONFIG_I: Configured from console by root on UnknownTty (UnknownIpAddr)"
    assert run_log(monkeypatch, [one_shot, DOWN]).outcome == "config_change"


def test_session_that_ended_before_the_down_does_not_count(monkeypatch):
    earlier = [ENTER.replace("23:56:50", "23:50:00"), SAVED.replace("23:57:26", "23:50:30")]
    r = run_log(monkeypatch, earlier + [DOWN])
    assert r.outcome == "link_lost"


def test_other_ports_and_nothing(monkeypatch):
    assert run_log(monkeypatch, [DOWN], interface="Ethernet11").outcome == "nothing"


def test_follow_survives_rotation(tmp_path):
    log = tmp_path / "traps.log"
    log.write_text("old line\n")
    lines, gen = [], follow(log, poll=0)
    assert next(gen) is None  # starts at the end; the old line is skipped
    with open(log, "a") as fh:
        fh.write("first\nhalf")
    for _ in range(5):
        item = next(gen)
        if item:
            lines.append(item)
    assert lines == ["first\n"]  # "half" waits for its newline
    os.rename(log, tmp_path / "traps.log.1")  # what logrotate does at midnight
    log.write_text("after rotation\n")
    for _ in range(5):
        item = next(gen)
        if item:
            lines.append(item)
    assert lines == ["first\n", "after rotation\n"]
