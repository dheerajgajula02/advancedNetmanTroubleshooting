"""Blocks that exist only in tests.

The schema and linter still have to handle host-only checks, explicit undo and
platform limits, so these stand-ins give the tests something to point at. They never run.
"""

from ts.registry import ACTIONS, CHECKS, action, check


def _never(*_a, **_k):
    raise AssertionError("test-only block was run")


def register() -> None:
    if "vrrp_split_brain" in CHECKS:  # conftest may be imported more than once
        return
    check("vrrp_split_brain", ["device", "interface"], ["split", "ok"], platforms=("eos",))(_never)
    action("restore_golden_section", ["device", "section"], undo="auto")(_never)
    action("escalate", ["reason"], undo="not_needed", platforms=("any",))(_never)
    assert "escalate" in ACTIONS
