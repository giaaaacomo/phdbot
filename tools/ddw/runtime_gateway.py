"""Temporary loopback gateway for one HTTP Runtime canary behind private HTTPS.

No administration credential, database access, scheduler or public listener.
The claim is forwarded at most once, including across process restarts.
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

MAX_BODY = 12_000_000
MARKER = "scoped-runtime-v1"


class Gateway(HTTPServer):
    def __init__(self, port: int, config: dict, state: Path) -> None:
        if (not isinstance(config.get("job_id"), int) or config["job_id"] <= 0
                or not isinstance(config.get("token"), str) or len(config["token"]) < 32
                or not isinstance(config.get("worker_id"), str) or not config["worker_id"]
                or not time.time() < config["expires_epoch"] <= time.time() + 3600):
            raise ValueError("invalid_bounded_gateway_config")
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.config, self.state = config, state
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    # HTTPServer requires method names matching the HTTP verbs.
    server: Gateway

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args: object) -> None:
        pass  # Never write incoming paths, credentials or payloads to logs.

    def reply(self, status: int, data: bytes = b'{"error":"gateway_denied"}') -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-PHDBOT-DDW-Gateway", MARKER)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self) -> None:
        self.reply(403)

    do_HEAD = do_GET  # noqa: N815
    do_PUT = do_GET  # noqa: N815
    do_DELETE = do_GET  # noqa: N815
    do_PATCH = do_GET  # noqa: N815
    do_OPTIONS = do_GET  # noqa: N815
    do_CONNECT = do_GET  # noqa: N815
    do_TRACE = do_GET  # noqa: N815

    def do_POST(self) -> None:
        cfg = self.server.config
        claim = "/v1/ddw/claim"
        prefix = f"/v1/ddw/jobs/{cfg['job_id']}/1/"
        if self.requestline.split()[1] != self.path or self.path not in {claim, prefix + "result", prefix + "fail"}:
            return self.reply(403)
        if time.time() >= cfg["expires_epoch"]:
            return self.reply(410)
        # Duplicate headers and alternate encodings must not reach the backend.
        expected = {"Authorization": "Bearer " + cfg["token"], "X-DDW-Worker": cfg["worker_id"]}
        for name, value in expected.items():
            values = self.headers.get_all(name, [])
            if len(values) != 1 or not hmac.compare_digest(values[0].encode(), value.encode()):
                return self.reply(401)
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal()
                or self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding")
                or self.headers.get("Expect") or self.headers.get_all("Content-Type", []) != ["application/json"]):
            return self.reply(400)
        length = int(lengths[0])
        if length > (MAX_BODY if self.path.endswith("/result") else 256):
            return self.reply(413)
        try:
            data = self.rfile.read(length)
            if len(data) != length:
                return self.reply(400)
            if self.path == claim:
                if json.loads(data) != {}:
                    return self.reply(400)
                # Reserve before forwarding. An ambiguous/lost claim fails closed.
                try:
                    fd = os.open(self.server.state / "claim-used", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    return self.reply(409, b'{"error":"claim_already_attempted"}')
                os.fsync(fd)
                os.close(fd)
                directory_fd = os.open(self.server.state, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            connection = http.client.HTTPConnection("127.0.0.1", 8003, timeout=20)
            try:
                connection.request("POST", self.path, data, {**expected, "Content-Type": "application/json"})
                response = connection.getresponse()
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000 or response.status not in {200, 400, 401, 409, 413, 503}:
                    return self.reply(502)
                if self.path == claim and response.status == 200:
                    assignment = json.loads(raw)
                    if (not isinstance(assignment, dict) or assignment.get("job_id") != cfg["job_id"]
                            or assignment.get("manifest", {}).get("context", {}).get("attempt") != 1):
                        return self.reply(409, b'{"error":"unexpected_claim"}')
                self.reply(response.status, raw)
            finally:
                connection.close()
        except (OSError, ValueError, http.client.HTTPException):
            self.reply(502)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    os.umask(0o077)
    if args.config.stat().st_mode & 0o077:
        raise SystemExit("Gateway config must be private")
    with Gateway(args.port, json.loads(args.config.read_bytes()), args.state) as server:
        server.timeout = 1
        while time.time() < server.config["expires_epoch"]:
            server.handle_request()


if __name__ == "__main__":
    main()
