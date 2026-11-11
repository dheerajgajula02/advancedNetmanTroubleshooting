"""Puts the GPU machine's embedding model on the private network, for the troubleshooter only.

The model runner listens on localhost and needs root to change that, so this small
forwarder does it instead. It takes one kind of request, from holders of the key.
"""

import hmac
import json
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEY = (HERE / "key").read_text().strip()
MODEL = "nomic-embed-text"
RUNNER = "http://127.0.0.1:11434/api/embed"
ADDRESS = ("10.9.8.190", 8791)
BIGGEST = 8_000_000


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _reply(self, code: int, value: dict) -> None:
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {KEY}")

    def do_GET(self):
        if not self._allowed():
            return self._reply(401, {"error": "key"})
        self._reply(200, {"model": MODEL})

    def do_POST(self):
        if not self._allowed():
            return self._reply(401, {"error": "key"})
        size = int(self.headers.get("Content-Length") or 0)
        if self.path != "/api/embed" or not 0 < size <= BIGGEST:
            return self._reply(400, {"error": "bad request"})
        texts = json.loads(self.rfile.read(size))["input"]
        ask = urllib.request.Request(RUNNER, json.dumps({"model": MODEL, "input": texts, "truncate": True, "keep_alive": "30m"}).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(ask, timeout=180) as answer:
                self._reply(200, {"model": MODEL, "embeddings": json.load(answer)["embeddings"]})
        except OSError as exc:
            self._reply(502, {"error": type(exc).__name__})


def main() -> None:
    while True:  # the private network can come up after this does
        try:
            server = ThreadingHTTPServer(ADDRESS, Handler)
            break
        except OSError:
            time.sleep(5)
    server.serve_forever()


if __name__ == "__main__":
    main()
