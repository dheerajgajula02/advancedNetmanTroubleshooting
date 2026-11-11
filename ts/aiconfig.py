"""Which model does which job, set from the console's AI models page.

A job is a role: the support agent, the desk that reads tickets and writes replies,
the memory notes, and each person's computer when played by AI. Any OpenAI-compatible
endpoint can be added. A model is only called if someone chose it here.

Keys live in the secrets folder and are never sent back to a browser. An endpoint's
address cannot be changed without giving its key again, so a stored key can never be
pointed at somebody else's server.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from ts import history

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = Path(os.environ.get("TS_AI_SETTINGS", ROOT / "data" / "ai-settings.json"))
KEYS = Path(os.environ.get("TS_AI_KEYS", ROOT / "secrets" / "ai-endpoints.json"))
CATALOG = Path(os.environ.get("TS_AI_CATALOG", ROOT / "data" / "ai-catalog.json"))
OPENCODE_ENV = Path(os.environ.get("TS_LLM_ENV", ROOT / "secrets" / "opencode.env"))
BUILT_IN = "opencode"
USER_AGENT = "network-troubleshooting/1.0"
CATALOG_GOOD_FOR = 6 * 3600
DAY = 86400

_DEFAULT = os.environ.get("TS_LLM_MODEL", "deepseek-v4.1-flash")
DEFAULTS = {"support": _DEFAULT, "desk": _DEFAULT, "notes": _DEFAULT, "customer": "longcat-2.5-free"}
ALWAYS_ALLOWED = ("deepseek-v4.1-flash", "muse-spark-1.3-contributor")  # what a run may name directly, with nothing chosen on the page
ROLE_WORDS = {"support": ("Support agent", "Investigates and fixes every ticket and incident."),
              "desk": ("Service desk", "Reads each ticket, writes to the person, and judges their replies."),
              "notes": ("Memory notes", "Writes the short note kept for every finished run."),
              "customer": ("Computers, by default", "Plays the person at any computer below that has no model of its own.")}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_lock = threading.Lock()


@dataclass(frozen=True)
class Choice:
    role: str
    endpoint: str
    base: str
    key: str
    model: str
    effort: str = ""

    @property
    def built_in(self) -> bool:
        return self.endpoint == BUILT_IN


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write(path: Path, value: dict, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1))
    if private:
        os.chmod(tmp, 0o600)
    tmp.replace(path)


def _opencode() -> dict[str, str]:
    values = {}
    for line in OPENCODE_ENV.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


def _settings() -> dict:
    stored = _read(SETTINGS)
    return {"endpoints": stored.get("endpoints") or {}, "roles": stored.get("roles") or {}}


# --- who uses what ----------------------------------------------------------

def host_roles() -> list[dict]:
    from ts import tickets  # tickets calls the model through llm, which reads this module
    return [{"id": p["host"], "label": f"{p['name']} ({p['host']})", "note": f"{p['team']}, {p['where']}", "host": True} for p in tickets.PEOPLE]


def roles() -> list[dict]:
    stored, out = _settings()["roles"], []
    for role, (label, note) in ROLE_WORDS.items():
        out.append({"id": role, "label": label, "note": note, "host": False, "endpoint": (stored.get(role) or {}).get("endpoint") or BUILT_IN,
                    "model": (stored.get(role) or {}).get("model") or DEFAULTS[role], "effort": (stored.get(role) or {}).get("effort") or ""})
    for host in host_roles():
        mine = stored.get(host["id"]) or {}
        out.append({**host, "endpoint": mine.get("endpoint") or "", "model": mine.get("model") or "", "effort": mine.get("effort") or ""})
    return out


def _known_role(role: str) -> bool:
    return role in ROLE_WORDS or any(h["id"] == role for h in host_roles())


def choice(role: str) -> Choice:
    """What this job runs on. A computer with no model of its own uses the default for computers."""
    stored = _settings()
    mine = stored["roles"].get(role) or {}
    if not mine.get("model"):
        if role in ROLE_WORDS:
            mine = {"endpoint": mine.get("endpoint") or BUILT_IN, "model": DEFAULTS[role], "effort": mine.get("effort") or ""}
        else:
            return replace(choice("customer"), role=role)
    endpoint = mine.get("endpoint") or BUILT_IN
    base, key = _address(endpoint, stored)
    return Choice(role, endpoint, base, key, mine["model"], mine.get("effort") or "")


def _address(endpoint: str, stored: dict | None = None) -> tuple[str, str]:
    if endpoint == BUILT_IN:
        values = _opencode()
        return values["OPENCODE_BASE"].rstrip("/"), values["OPENCODE_API_KEY"]
    found = (stored or _settings())["endpoints"].get(endpoint)
    if not found:
        raise ValueError(f"There is no endpoint called {endpoint!r}.")
    return found["base"].rstrip("/"), _read(KEYS).get(endpoint, "")


def chosen_models() -> set[str]:
    """Every model someone has picked for a job, plus the two a run may always name."""
    return {r["model"] for r in roles() if r["model"]} | set(ALWAYS_ALLOWED)


def set_role(role: str, endpoint: str, model: str, effort: str = "") -> None:
    if not _known_role(role):
        raise ValueError("There is no such job.")
    with _lock:
        stored = _settings()
        if not model:
            if role in ROLE_WORDS:
                raise ValueError("Choose a model for this job.")
            stored["roles"].pop(role, None)
        else:
            if endpoint != BUILT_IN and endpoint not in stored["endpoints"]:
                raise ValueError("Choose an endpoint that exists.")
            stored["roles"][role] = {"endpoint": endpoint, "model": model[:120], "effort": str(effort or "")[:20]}
        _write(SETTINGS, stored)


# --- endpoints --------------------------------------------------------------

def _clean_base(base: str) -> str:
    base = (base or "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The address must start with http:// or https://.")
    return base


def endpoints() -> list[dict]:
    keys, stored = _read(KEYS), _settings()["endpoints"]
    return [{"id": BUILT_IN, "name": "Opencode", "base": _opencode().get("OPENCODE_BASE", ""), "has_key": True, "built_in": True},
            *[{"id": eid, "name": e["name"], "base": e["base"], "has_key": bool(keys.get(eid)), "built_in": False} for eid, e in stored.items()]]


def save_endpoint(name: str, base: str, key: str = "", endpoint: str = "") -> str:
    """Add an endpoint, or change one. Changing its address needs its key again."""
    name, base, key = (name or "").strip()[:60], _clean_base(base), (key or "").strip()
    if not name:
        raise ValueError("Give it a name.")
    with _lock:
        stored, keys = _settings(), _read(KEYS)
        if endpoint:
            if endpoint == BUILT_IN:
                raise ValueError("The built-in endpoint cannot be changed here.")
            old = stored["endpoints"].get(endpoint)
            if not old:
                raise ValueError("There is no such endpoint.")
            if base != old["base"] and not key:
                raise ValueError("A new address needs its key again, so the stored one is never sent somewhere else.")
        else:
            endpoint = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "endpoint"
            taken, n = set(stored["endpoints"]) | {BUILT_IN}, 2
            original = endpoint
            while endpoint in taken:
                endpoint, n = f"{original}-{n}", n + 1
        if not _ID.match(endpoint):
            raise ValueError("That name cannot be used.")
        stored["endpoints"][endpoint] = {"name": name, "base": base}
        _write(SETTINGS, stored)
        if key:
            keys[endpoint] = key
            _write(KEYS, keys, private=True)
    return endpoint


def delete_endpoint(endpoint: str) -> None:
    if endpoint == BUILT_IN:
        raise ValueError("The built-in endpoint cannot be removed.")
    with _lock:
        stored, keys = _settings(), _read(KEYS)
        if endpoint not in stored["endpoints"]:
            raise ValueError("There is no such endpoint.")
        using = [role for role, v in stored["roles"].items() if v.get("endpoint") == endpoint]
        if using:
            raise ValueError(f"It is still chosen for: {', '.join(using)}. Move those first.")
        del stored["endpoints"][endpoint]
        _write(SETTINGS, stored)
        if keys.pop(endpoint, None) is not None:
            _write(KEYS, keys, private=True)


# --- what an endpoint offers ------------------------------------------------

def _plain(model: dict) -> dict:
    """One model from /models. A plain OpenAI-style endpoint gives little beyond the id."""
    caps, cost = model.get("capabilities") or {}, ((model.get("cost") or {}).get("text") or {})
    reasoning = caps.get("reasoning") or {}
    return {"id": model["id"], "name": model.get("name") or model["id"], "tools": (caps.get("tools") or {}).get("supported"), "efforts": reasoning.get("effort") or [], "thinks": bool(reasoning.get("supported")),
            "input": (cost.get("input") or {}).get("price"), "cached": ((cost.get("input") or {}).get("cache") or {}).get("read"), "output": (cost.get("output") or {}).get("price")}


def fetch_models(endpoint: str) -> list[dict]:
    base, key = _address(endpoint)
    headers = {"User-Agent": USER_AGENT, **({"Authorization": f"Bearer {key}"} if key else {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(base + "/models", headers=headers), timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"The endpoint answered {exc.code}.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read its models: {type(exc).__name__}.") from exc
    rows = data.get("data") if isinstance(data, dict) else data
    models = sorted((_plain(m) for m in rows or [] if isinstance(m, dict) and m.get("id")), key=lambda m: m["id"])
    cached = _read(CATALOG)
    cached[endpoint] = {"time": time.time(), "models": models}
    _write(CATALOG, cached)
    return models


def models_of(endpoint: str, refresh: bool = False) -> list[dict]:
    entry = _read(CATALOG).get(endpoint) or {}
    if not refresh and entry.get("models") and time.time() - entry.get("time", 0) < CATALOG_GOOD_FOR:
        return entry["models"]
    try:
        return fetch_models(endpoint)
    except ValueError:
        return entry.get("models") or []


# --- what has been used -----------------------------------------------------

_USAGE = """CREATE TABLE IF NOT EXISTS ai_usage(time REAL, role TEXT, endpoint TEXT, model TEXT, input INTEGER, cached INTEGER, output INTEGER, seconds REAL);
CREATE INDEX IF NOT EXISTS ai_usage_time ON ai_usage(time);"""


def _db(path: Path | None = None):
    conn = history._open(path)
    conn.executescript(_USAGE)
    return conn


def note_usage(chosen: Choice, usage: dict, seconds: float, path: Path | None = None) -> None:
    """One answered call. Counting must never get in the way of the work."""
    if os.environ.get("TS_AI_USAGE", "on") == "off":
        return
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0)
    try:
        with _db(path) as db:
            db.execute("INSERT INTO ai_usage VALUES (?,?,?,?,?,?,?,?)", (time.time(), chosen.role, chosen.endpoint, chosen.model, int(usage.get("prompt_tokens") or 0), cached, int(usage.get("completion_tokens") or 0), seconds))
    except (OSError, sqlite3.Error):
        pass


def _cost(row: dict, price: dict | None) -> float | None:
    """In dollars, from the endpoint's own price list. None when it publishes none."""
    if not price or price.get("input") is None or price.get("output") is None:
        return None
    fresh = max(0, row["input"] - row["cached"])
    return (fresh * price["input"] + row["cached"] * (price.get("cached") if price.get("cached") is not None else price["input"]) + row["output"] * price["output"]) / 1e6


def usage(path: Path | None = None) -> dict:
    """Calls and tokens by model and by job, for the last day and the last week."""
    now, prices = time.time(), {}
    for entry in _read(CATALOG).values():
        prices.update({m["id"]: m for m in entry.get("models") or []})
    out = {}
    with _db(path) as db:
        for name, since in (("day", now - DAY), ("week", now - 7 * DAY)):
            rows = [dict(r) for r in db.execute("SELECT model, role, COUNT(*) calls, SUM(input) input, SUM(cached) cached, SUM(output) output FROM ai_usage WHERE time > ? GROUP BY model, role", (since,))]
            by_model: dict[str, dict] = {}
            for row in rows:
                total = by_model.setdefault(row["model"], {"model": row["model"], "calls": 0, "input": 0, "cached": 0, "output": 0, "jobs": {}})
                for field in ("calls", "input", "cached", "output"):
                    total[field] += row[field]
                total["jobs"][row["role"]] = row["calls"]
            for total in by_model.values():
                total["cost"] = _cost(total, prices.get(total["model"]))
            out[name] = sorted(by_model.values(), key=lambda t: -(t["input"] + t["output"]))
    return out


def page() -> dict:
    """Everything the AI models page shows. Never a key."""
    listed = endpoints()
    return {"endpoints": listed, "roles": roles(), "models": {e["id"]: models_of(e["id"]) for e in listed}, "usage": usage()}
