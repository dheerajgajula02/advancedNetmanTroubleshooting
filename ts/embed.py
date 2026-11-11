"""Turning text into numbers on the GPU machine, so past work can be found by what it means.

If the machine cannot be reached, callers get None and carry on with keyword search.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.parse
import urllib.request
from pathlib import Path

SECRETS = Path(os.environ.get("TS_EMBED_ENV", Path(__file__).resolve().parent.parent / "secrets" / "embed.env"))
BATCH = 24
LONGEST = 6000  # characters of one text; the model reads about this much
# What each model wants in front of a question, and in front of something stored. Without it these models find less.
PREFIX = {"nomic-embed-text": ("search_query: ", "search_document: "), "mxbai-embed-large": ("Represent this sentence for searching relevant passages: ", ""),
          "snowflake-arctic-embed2": ("Represent this sentence for searching relevant passages: ", ""), "bge-m3": ("", ""), "embeddinggemma": ("task: search result | query: ", "title: none | text: ")}
_settings_cache: dict[str, str] | None = None
_working: str | None = None


def enabled() -> bool:
    return os.environ.get("TS_EMBED", "").lower() != "off" and SECRETS.exists()


def _settings() -> dict[str, str]:
    global _settings_cache
    if _settings_cache is None:
        lines = (line.split("=", 1) for line in SECRETS.read_text().splitlines() if "=" in line)
        _settings_cache = {name.strip(): value.strip() for name, value in lines}
    return _settings_cache


def model() -> str:
    return _settings().get("EMBED_MODEL", "nomic-embed-text") if enabled() else ""


def _answers(base: str) -> bool:
    url = urllib.parse.urlsplit(base)
    try:
        socket.create_connection((url.hostname, url.port or 80), 3).close()
        return True
    except OSError:
        return False


def _base() -> str | None:
    """The first place the model answers, trying the last one that worked first."""
    global _working
    places = [p for p in _settings().get("EMBED_BASES", "").split(",") if p]
    for place in sorted(places, key=lambda p: p != _working):
        if _answers(place):
            _working = place
            return place
    return None


def _ask(base: str, texts: list[str]) -> list[list[float]]:
    body = json.dumps({"model": model(), "input": texts, "truncate": True, "keep_alive": "30m"}).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {_settings().get('EMBED_KEY', '')}"}
    with urllib.request.urlopen(urllib.request.Request(f"{base}/api/embed", body, headers), timeout=180) as answer:
        return json.load(answer)["embeddings"]


def reachable() -> bool:
    return enabled() and _base() is not None


def vectors(texts: list[str], purpose: str = "search_document") -> list[list[float]] | None:
    """One vector per text. `purpose` is search_document for what is stored and search_query for what is looked up."""
    if not enabled() or not texts:
        return None
    base = _base()
    if base is None:
        return None
    asking, storing = PREFIX.get(model().split(":")[0], ("", ""))
    prepared = [(asking if purpose == "search_query" else storing) + text[:LONGEST] for text in texts]
    out: list[list[float]] = []
    try:
        for start in range(0, len(prepared), BATCH):
            out += _ask(base, prepared[start:start + BATCH])
    except (OSError, ValueError, KeyError):
        return None
    return out
