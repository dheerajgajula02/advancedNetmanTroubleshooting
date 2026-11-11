"""The AI's tools over MCP (JSON-RPC on stdin and stdout), for any agent that speaks it.

These are the same tools with the same guards the built-in takeover uses, so a change
is still a transaction and a suggestion still waits for review.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from ts import store, tools
from ts.ruleset import load_ruleset

PROTOCOL = "2024-11-05"


def _session(root: Path, run_id: str | None) -> tools.Session:
    record = store.load(run_id) if run_id else None
    if record is None:
        now = time.time()
        record = {"id": f"mcp-{int(now)}", "event": {"time": now, "device": "", "event": ""}, "result": {"kind": "stop"}, "steps": []}
    return tools.Session(record, load_ruleset(root).parsed, root=root, model="mcp client")


def handle(session: tools.Session, message: dict) -> dict | None:
    """The answer to one JSON-RPC message, or None for a notification."""
    method, number = message.get("method"), message.get("id")
    if number is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}}, "serverInfo": {"name": "network-troubleshooting", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema} for t in tools.registry().values()]}
    elif method == "tools/call":
        params = message.get("params") or {}
        out = tools.call(session, params.get("name", ""), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": json.dumps(out)}], "isError": out.get("outcome") in ("invalid", "error")}
    else:
        return {"jsonrpc": "2.0", "id": number, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": number, "result": result}


def serve(root: Path, run_id: str | None) -> int:
    session = _session(root, run_id)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            answer = handle(session, json.loads(line))
        except json.JSONDecodeError:
            answer = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}}
        if answer is not None:
            print(json.dumps(answer), flush=True)
    return 0
