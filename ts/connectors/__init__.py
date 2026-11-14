"""Connectors to the ticketing systems a company already runs: ServiceNow and Salesforce.

Both directions go through one small interface, so a third system is one more file:

- Their tickets come to us. A new network incident in ServiceNow, or a new case in Salesforce from someone we
  know, becomes a ticket on our service desk and is worked like any other. What the person writes there later is
  added to our ticket, our replies go back as comments (our internal notes as work notes), and our status is
  copied into theirs, up to resolved.
- Our own finds go to them. When a run the watcher started is worth an email, the same story opens a ticket in
  their system, resolved there at once when the run fixed it, left open for a person when it did not.

A connector is on when its login file exists in secrets/: servicenow.env or salesforce.env.
"""

from __future__ import annotations

import os
from pathlib import Path

SECRETS = Path(os.environ.get("TS_SECRETS", Path(__file__).resolve().parent.parent.parent / "secrets"))

# Our ticket statuses, in the words both systems understand.
SOLVED_LIKE = ("solved", "no_fault", "closed")


class ConnectorError(Exception):
    pass


def read_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def configured() -> list:
    """Every connector whose login file is present."""
    from ts.connectors import salesforce, servicenow
    found = []
    for module in (servicenow, salesforce):
        settings = read_env(SECRETS / module.LOGIN_FILE)
        if all(settings.get(k) for k in module.REQUIRED):
            found.append(module.Connector(settings))
    return found


def by_name(name: str):
    for c in configured():
        if c.name == name:
            return c
    raise ConnectorError(f"{name} is not set up: secrets/{name}.env is missing or incomplete.")


def match_person(name: str, email: str, people: list[dict]) -> dict | None:
    """The desk person a caller in their system is, by full name or by the email's first part."""
    name, local = " ".join((name or "").lower().split()), (email or "").lower().split("@")[0]
    for p in people:
        first, last = p["name"].lower().split()[0], p["name"].lower().split()[-1]
        if name == p["name"].lower() or local in (p["id"], f"{first}.{last}", f"{first}{last}"):
            return p
    return None
