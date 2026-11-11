"""Grafana's alerts and annotations. Only ever reads. The login lives in secrets/grafana.env."""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from pathlib import Path

from ts.sources import Doc, NotConfigured, source

BASE = os.environ.get("TS_GRAFANA", "http://10.0.0.58:3000")
LOGIN_FILE = Path(os.environ.get("TS_GRAFANA_ENV", Path(__file__).resolve().parent.parent.parent / "secrets" / "grafana.env"))


def _authorization() -> str:
    try:
        values = dict(line.split("=", 1) for line in LOGIN_FILE.read_text().splitlines() if "=" in line)
    except OSError as exc:
        raise NotConfigured("no login in secrets/grafana.env") from exc
    if values.get("GRAFANA_TOKEN"):
        return f"Bearer {values['GRAFANA_TOKEN']}"
    if values.get("GRAFANA_USER") and values.get("GRAFANA_PASSWORD"):
        pair = f"{values['GRAFANA_USER']}:{values['GRAFANA_PASSWORD']}".encode()
        return f"Basic {base64.b64encode(pair).decode()}"
    raise NotConfigured("no login in secrets/grafana.env")


def _get(path: str, authorization: str):
    request = urllib.request.Request(BASE + path, headers={"Authorization": authorization})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


@source("grafana")
def alerts(start: float, end: float, device: str | None = None) -> list[Doc]:
    auth = _authorization()
    docs = [Doc("grafana", a["time"] / 1000, f"Annotation: {a.get('text', '')[:200]}") for a in
            _get(f"/api/annotations?from={int(start * 1000)}&to={int(end * 1000)}&limit=50", auth)]
    docs += [Doc("grafana", end, f"Alert {a.get('labels', {}).get('alertname', '?')}: {a.get('status', {}).get('state', '')}",
                 a.get("annotations", {}).get("summary", "")) for a in _get("/api/alertmanager/grafana/api/v2/alerts", auth)]
    return docs
