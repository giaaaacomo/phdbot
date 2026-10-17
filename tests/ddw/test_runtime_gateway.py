# ruff: noqa: PT009, PT027
# Keep these isolated tests runnable with the standard library unittest runner.
"""Scope and durable claim guard, without production services or networking."""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/ddw"))
import runtime_canary as canary
import runtime_gateway as gateway


class FakeSocket:
    def __init__(self, request):
        self.input, self.output = io.BytesIO(request), bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, value):
        pass


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"worker_id": "canary-worker", "token": "t" * 40,
                       "job_id": 8, "expires_epoch": time.time() + 120}
        self.server = SimpleNamespace(config=self.config, state=Path(self.tmp.name))
        self.backend = MagicMock()
        self.backend.getresponse.return_value.status = 200
        self.backend.getresponse.return_value.read.return_value = b'{"state":"accepted"}'

    def request(self, path, *, method="POST", body=b"{}", headers=None):
        pairs = headers if headers is not None else [
            ("Authorization", "Bearer " + self.config["token"]),
            ("X-DDW-Worker", "canary-worker"), ("Content-Type", "application/json"),
            ("Content-Length", str(len(body)))
        ]
        raw = f"{method} {path} HTTP/1.1\r\nHost: example.test\r\n".encode()
        raw += b"".join(f"{k}: {v}\r\n".encode() for k, v in pairs) + b"\r\n" + body
        connection = FakeSocket(raw)
        with patch.object(gateway.http.client, "HTTPConnection", return_value=self.backend):
            gateway.Handler(connection, ("127.0.0.1", 12345), self.server)
        return int(connection.output.split(b" ")[1])

    def test_forbidden_routes_never_reach_backend(self):
        for path in ["/", "/dashboard", "/v1/ddw/workers", "/v1/ddw/status", "/v1/ddw/jobs",
                     "/v1/ddw/jobs/9/1/result", "/v1/ddw/jobs/8/2/result", "/v1/ddw/jobs/8/cancel",
                     "/v1/ddw/claim?x=1", "/v1/ddw/%63laim", "/v1/ddw/../claim"]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path), 403)
        self.assertEqual(self.request("/v1/ddw/claim", method="GET"), 403)
        self.backend.request.assert_not_called()

    def test_auth_and_ambiguous_headers_fail_closed(self):
        base = [("Authorization", "Bearer " + "t" * 40), ("X-DDW-Worker", "canary-worker"),
                ("Content-Type", "application/json"), ("Content-Length", "2")]
        self.assertEqual(self.request("/v1/ddw/claim", headers=[]), 401)
        for extra, code in [(("Authorization", "Bearer other"), 401),
                            (("Content-Length", "2"), 400), (("Transfer-Encoding", "chunked"), 400),
                            (("Content-Encoding", "gzip"), 400)]:
            self.assertEqual(self.request("/v1/ddw/claim", headers=[*base, extra]), code)
        self.backend.request.assert_not_called()

    def test_claim_is_forwarded_once_and_durable(self):
        self.backend.getresponse.return_value.read.return_value = json.dumps(
            {"job_id": 8, "manifest": {"context": {"attempt": 1}}}).encode()
        self.assertEqual(self.request("/v1/ddw/claim"), 200)
        self.server = SimpleNamespace(config=self.config.copy(), state=Path(self.tmp.name))
        self.assertEqual(self.request("/v1/ddw/claim"), 409)
        self.backend.request.assert_called_once()

    def test_wrong_claim_is_not_released_to_worker(self):
        self.backend.getresponse.return_value.read.return_value = b'{"job_id":9}'
        self.assertEqual(self.request("/v1/ddw/claim"), 409)
        self.assertEqual(self.request("/v1/ddw/claim"), 409)
        self.backend.request.assert_called_once()

    def test_result_replay_preserves_exact_ciphertext(self):
        for _ in range(2):
            self.assertEqual(self.request("/v1/ddw/jobs/8/1/result", body=b'{"ciphertext":"opaque"}'), 200)
        self.assertEqual(self.backend.request.call_count, 2)
        self.assertEqual(self.backend.request.call_args.args[2], b'{"ciphertext":"opaque"}')
        self.assertEqual(set(self.backend.request.call_args.args[3]),
                         {"Authorization", "X-DDW-Worker", "Content-Type"})

    def test_expired_gateway_forwards_nothing(self):
        self.config["expires_epoch"] = time.time() - 1
        self.assertEqual(self.request("/v1/ddw/claim"), 410)
        self.backend.request.assert_not_called()

    def test_oversize_and_nonempty_claim_rejected(self):
        self.assertEqual(self.request("/v1/ddw/claim", body=b" " * 257), 413)
        self.assertEqual(self.request("/v1/ddw/claim", body=b'{"job_id":9}'), 400)
        self.backend.request.assert_not_called()

    def test_export_has_no_manifest_or_plaintext(self):
        root = Path(self.tmp.name)
        spool = root / "spool"
        (spool / "result").mkdir(parents=True)
        raw = json.dumps({"version": 1, "signature": "signature", "envelope": {
            "version": 1, "context": {}, "nonce": "nonce", "wrapped_key": "key", "ciphertext": "cipher"}}).encode()
        (spool / "result/evidence.sealed.json").write_bytes(raw)
        (spool / "manifest.json").write_text("PRIVATE")
        out = root / "public"
        out.mkdir()
        canary.export(spool, out, 8)
        self.assertEqual({p.name for p in out.iterdir()}, {"evidence.sealed.json", "checksums.json"})
        (spool / "result/evidence.sealed.json").write_text('{"html":"PRIVATE"}')
        with self.assertRaises(ValueError):
            canary.export(spool, out, 8)

    def test_lost_claim_ack_cannot_create_second_assignment(self):
        self.backend.request.side_effect = OSError("lost connection")
        self.assertEqual(self.request("/v1/ddw/claim"), 502)
        self.backend.request.side_effect = None
        self.assertEqual(self.request("/v1/ddw/claim"), 409)
        self.backend.request.assert_called_once()

    def test_denied_isolation_probe_prevents_client_and_acquisition(self):
        previous = Path.cwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, previous)
        env = {"DDW_ENDPOINT": "https://coordinator.test.ts.net/v1/ddw", "DDW_WORKER_ID": "w",
               "DDW_JOB_ID": "8", "GITHUB_SHA": "a" * 40, "DDW_TOKEN": "test-token",
               "DDW_SIGNING_KEY": "test-signer"}
        with patch.dict(os.environ, env), patch.object(canary, "probe", side_effect=ValueError("denied")), \
                patch.object(canary, "Client") as client, self.assertRaises(ValueError):
            canary.main()
        client.assert_not_called()
        receipt = json.loads(Path("ddw-runtime-artifacts/receipt.json").read_bytes())
        self.assertEqual(receipt["acquisition"], "not_started")
        self.assertEqual(receipt["import"], "not_checked")

    def test_nonprivate_coordinator_is_rejected_before_network(self):
        for url in ["http://coordinator.ts.net/v1/ddw", "https://example.com/v1/ddw",
                    "https://coordinator.ts.net:8003/v1/ddw", "https://coordinator.ts.net/v1/ddw?extra=1"]:
            with self.subTest(url=url), patch.object(canary, "build_opener") as opener:
                with self.assertRaises(ValueError):
                    canary.probe(url, "worker", "token", 8)
                opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
