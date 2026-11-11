"""Web UI: python -m ts serve --host <addr> --port 8770.

Every request re-reads what is stored, so new runs show up without a restart.
The writes are deleting a stored run, deciding a suggestion on the review page, and
moving a rule between modes. All of them need JSON from this page's own origin, so
another site cannot send them: the browser asks first, and this server does not
answer OPTIONS.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from ts import aiconfig, collect, configs, dataset, engine, history, overview, review, rulebook, store, training as fine_tuning
from ts.ruleset import load_ruleset
from ts.view import state

STATIC = Path(__file__).resolve().parent / "static"
FONT = re.compile(r"^/fonts/([a-z0-9-]+\.woff2)$")
ASSETS = {"/app.js": "text/javascript; charset=utf-8", "/app.css": "text/css; charset=utf-8", "/base.css": "text/css; charset=utf-8"}
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'",
}


def make_handler(rules_dir: Path):
    def rules():
        return load_ruleset(rules_dir)

    def as_json(value):
        return json.dumps(value).encode(), "application/json", "no-store"

    def training(path: str, query: dict):
        try:
            if path == "/api/training":
                value = dataset.page(query)
            else:
                m = re.fullmatch(r"/api/training/([0-9a-f-]{36})(?:/(input|step)/(\d+))?", path)
                if not m:
                    return None
                value = dataset.detail(m.group(1)) if not m.group(2) else dataset.input_section(m.group(1), int(m.group(3))) if m.group(2) == "input" else dataset.step(m.group(1), int(m.group(3)))
                if value is None:
                    return None
        except dataset.AWAY as exc:
            value = {"down": True, "why": type(exc).__name__}
        return json.dumps(value, default=dataset.plain).encode(), "application/json", "no-store"

    def route(path: str, query: dict):
        if path.startswith("/api/training"):
            return training(path, query)
        if path == "/api/workbench":
            try:
                return json.dumps(collect.overview(), default=dataset.plain).encode(), "application/json", "no-store"
            except dataset.AWAY as exc:
                return as_json({"down": True, "why": type(exc).__name__})
        if path == "/api/train":
            return as_json(fine_tuning.describe())
        job = re.fullmatch(r"/api/train/jobs/(ft-[0-9a-z-]+)", path)
        if job:
            found = fine_tuning.detail(job.group(1))
            return as_json(found) if found else None
        rows = re.fullmatch(r"/api/workbench/datasets/([a-z0-9-]+)", path)
        if rows:
            found = fine_tuning.dataset_rows(rows.group(1), query.get("split", "train"), int(query.get("page") or 1))
            return as_json(found) if found else None
        if path == "/api/now":
            return as_json(overview.now())
        if path == "/api/ai-models":
            return as_json(aiconfig.page())
        if path == "/api/runs":
            return as_json(store.page(query, history.all_run_tags()))
        if path == "/api/review":
            return as_json({**review.listing(query.get("status") or None, query.get("kind") or None, int(query.get("page") or 1), int(query.get("size") or 12)), "counts": review.counts()})
        if path == "/api/tags":
            return as_json(review.tag_catalog())
        if path == "/api/incidents":
            return json.dumps(store.summaries()).encode(), "application/json", "no-store"
        m = re.fullmatch(r"/api/incidents/([A-Za-z0-9_.-]+)", path)
        if m:
            record = store.load(m.group(1))
            if not record:
                return None
            state_now, by = store.verdict(record)
            return as_json({**record, "state": state_now, "by": by, "tags": history.tags_of(record["id"])})
        m = re.fullmatch(r"/api/rules/([a-z0-9-]+)", path)
        if m:
            loaded = rules().get(m.group(1))
            if not loaded or loaded.rule.table != "diagnose":
                return None
            return json.dumps(engine.rule_map(loaded.rule, {})).encode(), "application/json", "no-store"
        if path == "/api/scenarios":
            rows = [engine.rule_map(r, {}) for r in rules().parsed if r.table == "diagnose"]
            return json.dumps(sorted(rows, key=lambda r: r["title"] or r["id"])).encode(), "application/json", "no-store"
        if path == "/api/rules":
            return json.dumps(state(rules())).encode(), "application/json", "no-store"
        if path in ("/", "/index.html"):
            return (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8", "no-store"
        if path in ASSETS:  # also fetched by the Netman GUI, which shows these pages in its own sidebar
            return (STATIC / path[1:]).read_bytes(), ASSETS[path], "no-store"
        m = FONT.match(path)
        if m and (STATIC / "fonts" / m.group(1)).is_file():
            return (STATIC / "fonts" / m.group(1)).read_bytes(), "font/woff2", "max-age=86400"
        if path == "/healthz":
            return b"ok\n", "text/plain", "no-store"
        return None

    class Handler(BaseHTTPRequestHandler):
        server_version = "ts-ui"

        def do_GET(self):  # noqa: N802
            try:
                url = urlsplit(self.path)
                found = route(url.path, dict(parse_qsl(url.query)))
                if found is None:
                    self.send_error(404)
                    return
                body, ctype, cache = found
                self.send_response(200)
            except Exception as exc:  # show the reason instead of a dropped connection
                body, ctype, cache = json.dumps({"error": str(exc)}).encode(), "application/json", "no-store"
                self.send_response(500)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for key, value in HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _reply(self, code: int, value=None):
            body = json.dumps(value).encode() if value is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, header in HEADERS.items():
                self.send_header(key, header)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._reply(415, {"error": "This needs JSON."})
                return
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self._reply(403, {"error": "Not from this page."})
                return
            try:
                data = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
                path = self.path.split("?", 1)[0]
                decided = re.fullmatch(r"/api/review/(\d+)/(accept|reject)", path)
                moved = re.fullmatch(r"/api/rules/([a-z0-9-]+)/mode", path)
                handled = re.fullmatch(r"/api/incidents/([A-Za-z0-9_.-]+)/handled", path)
                stop_job = re.fullmatch(r"/api/train/jobs/(ft-[0-9a-z-]+)/stop", path)
                stop_campaign = re.fullmatch(r"/api/workbench/campaigns/(cmp-[0-9a-f]+)/stop", path)
                endpoint = re.fullmatch(r"/api/ai-models/endpoints/([a-z0-9-]+)/(delete|models)", path)
                if path == "/api/ai-models/roles":
                    aiconfig.set_role(data["role"], data.get("endpoint") or aiconfig.BUILT_IN, data.get("model") or "", data.get("effort") or "")
                    self._reply(200, {"saved": True})
                elif path == "/api/ai-models/endpoints":
                    self._reply(200, {"id": aiconfig.save_endpoint(data.get("name") or "", data.get("base") or "", data.get("key") or "", data.get("id") or "")})
                elif endpoint and endpoint.group(2) == "delete":
                    aiconfig.delete_endpoint(endpoint.group(1))
                    self._reply(200, {"deleted": True})
                elif endpoint:
                    self._reply(200, {"models": aiconfig.fetch_models(endpoint.group(1))})
                elif path == "/api/training/snapshot":
                    self._reply(200, dataset.snapshot())
                elif path == "/api/workbench/datasets":
                    self._reply(200, fine_tuning.build_dataset(data.get("name") or "", data.get("include") or "graded", who=data.get("who") or "support"))
                elif path == "/api/workbench/campaigns":
                    from drills import campaign
                    self._reply(200, campaign.start(data.get("name") or "", data.get("scenarios") or [], int(data.get("rounds") or 1)))
                elif stop_campaign:
                    from drills import campaign
                    campaign.stop(stop_campaign.group(1))
                    self._reply(200, {"stopped": True})
                elif path == "/api/train/offers":
                    self._reply(200, fine_tuning.preview(data))
                elif path == "/api/train/jobs":
                    self._reply(200, fine_tuning.create(data))
                elif stop_job:
                    self._reply(200, fine_tuning.request_stop(stop_job.group(1)) or {})
                elif decided and decided.group(2) == "accept":
                    self._reply(200, review.accept(int(decided.group(1)), "operator", data.get("mode") or "shadow", data.get("runs"), rules_dir))
                elif decided:
                    review.reject(int(decided.group(1)), data.get("reason") or "", "operator")
                    self._reply(200, {"rejected": True})
                elif handled:
                    now_is = store.set_handled(handled.group(1), data.get("handled", True) is not False)
                    if now_is == "handled":  # the real repair is done, so any stop-gap from this run may go back to known-good
                        configs.release(run_id=handled.group(1))
                    self._reply(200, {"state": now_is})
                elif moved:
                    rulebook.set_mode(rules_dir, moved.group(1), data.get("mode") or "", "operator")
                    self._reply(200, {"mode": data.get("mode")})
                else:
                    self._reply(404, {"error": "No such thing."})
            except (ValueError, rulebook.RuleRefused, review.NotPending, KeyError) as exc:
                self._reply(400, {"error": str(exc)})
            except Exception as exc:  # show the reason instead of a dropped connection
                self._reply(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_DELETE(self):  # noqa: N802
            m = re.fullmatch(r"/api/incidents/([A-Za-z0-9_.-]+)", self.path.split("?", 1)[0])
            if not m or not store.delete(m.group(1)):
                self.send_error(404)
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            for key, value in HEADERS.items():
                self.send_header(key, value)
            self.end_headers()

        def log_message(self, fmt, *args):
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    return Handler


def serve(host: str, port: int, rules_dir: Path) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(Path(rules_dir)))
    threading.Thread(target=fine_tuning.sweep_forever, daemon=True).start()
    print(f"troubleshooting UI on http://{host}:{port}/  (rules: {rules_dir})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
