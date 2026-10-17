"""Portable pull worker: authenticated HTTP, durable encrypted outbox, no DB or LLM."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cryptography.hazmat.primitives import serialization

from phd_searcher.ddw import acquire, worker


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        return None


class Client:
    def __init__(
        self,
        endpoint: str,
        worker_id: str,
        token: str,
        signing_key: bytes,
        spool: Path,
        source_commit: str,
        *,
        allow_loopback_http: bool = False,
    ) -> None:
        url = urlsplit(endpoint)
        if (
            url.username
            or url.password
            or url.query
            or url.fragment
            or (
                url.scheme != "https"
                and not (
                    allow_loopback_http and url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}
                )
            )
        ):
            raise ValueError("coordinator_requires_https")
        self.endpoint, self.worker_id, self.token = endpoint.rstrip("/"), worker_id, token
        self.key, self.spool, self.commit = signing_key, spool, source_commit
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        spool.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(spool, 0o700)

    def request(
        self, path: str, data: bytes = b"{}", *, content_type: str = "application/json"
    ) -> dict[str, Any] | None:
        request = Request(
            self.endpoint + path,
            data=data,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": content_type,
                "X-DDW-Worker": self.worker_id,
            },
        )
        with self.opener.open(request, timeout=20) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("coordinator_response_limit")
            return cast(dict[str, Any] | None, json.loads(raw))

    def step(self) -> str:
        """At most one acquisition. Lost acknowledgments resend the stored ciphertext."""
        pending = self.spool / "pending.json"
        result_file = self.spool / "result" / "evidence.sealed.json"
        if pending.exists():
            claim = json.loads(pending.read_bytes())
            # An interrupted acquisition is never silently repeated within one attempt.
            if not result_file.exists() and not (self.spool / "failure.json").exists():
                worker.write_json(self.spool / "failure.json", {"code": "worker_interrupted"})
        else:
            claim = self.request("/claim")
            if claim is None:
                return "idle"
            manifest = claim["manifest"]
            if manifest["context"]["source_commit"] != self.commit or claim["build_id"] != worker.build_id():
                raise ValueError("unapproved_worker_build")
            key = serialization.load_pem_private_key(self.key, password=None)
            public_key = key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            if worker.digest(public_key) != claim["signer_sha256"]:
                raise ValueError("worker_signer_mismatch")
            worker.write_json(pending, claim)
            public = self.spool / "recipient.pem"
            public.write_text(claim["recipient_public_pem"])
            worker.write_json(self.spool / "manifest.json", manifest)
            try:
                if datetime.now(UTC) >= datetime.fromisoformat(claim["lease_until"]):
                    raise ValueError("expired_lease")
                os.environ["DDW_SOURCE_COMMIT"] = self.commit
                summary = acquire.execute(
                    acquire.read_manifest(self.spool / "manifest.json", public),
                    public.read_bytes(),
                    self.spool / "result",
                    self.key,
                )
                if summary["state"] != "complete":
                    code = (
                        "policy_changed"
                        if any(not p.get("preflight_accepted") for p in summary["policies"])
                        else "acquisition_incomplete"
                    )
                    worker.write_json(self.spool / "failure.json", {"code": code})
            except (OSError, ValueError, KeyError):
                worker.write_json(self.spool / "failure.json", {"code": "transport_error"})
        job_id, attempt = claim["job_id"], claim["manifest"]["context"]["attempt"]
        failure = self.spool / "failure.json"
        if failure.exists():
            outcome = self.request(f"/jobs/{job_id}/{attempt}/fail", failure.read_bytes())
        else:
            outcome = self.request(f"/jobs/{job_id}/{attempt}/result", result_file.read_bytes())
        # Keep receipts/artifacts after acknowledgment; never overwrite another attempt.
        if outcome is None:
            raise ValueError("missing_delivery_receipt")
        worker.write_json(self.spool / "ack.json", outcome)
        archived = self.spool.parent / (self.spool.name + f"-job-{job_id}-{attempt}")
        self.spool.rename(archived)
        self.spool.mkdir(mode=0o700)
        return str(outcome["state"])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coordinator", required=True, help="https://host/v1/ddw")
    p.add_argument("--worker-id", required=True)
    p.add_argument("--token-file", type=Path, required=True)
    p.add_argument("--signing-key", type=Path, required=True)
    p.add_argument("--source-commit", required=True)
    p.add_argument("--spool", type=Path, required=True)
    p.add_argument("--serve", action="store_true", help="bounded jobs, sleep when idle; default one step")
    p.add_argument("--allow-loopback-http", action="store_true", help="isolated local testing only")
    args = p.parse_args()
    os.umask(0o077)
    client = Client(
        args.coordinator,
        args.worker_id,
        args.token_file.read_text().strip(),
        args.signing_key.read_bytes(),
        args.spool,
        args.source_commit,
        allow_loopback_http=args.allow_loopback_http,
    )
    while True:
        try:
            state = client.step()
            print(json.dumps({"state": state}), flush=True)
        except HTTPError as exc:
            print(json.dumps({"state": "delivery_deferred", "http_status": exc.code}), flush=True)
            if (400 <= exc.code < 500 and exc.code != 429) or not args.serve:
                raise SystemExit(1) from None
        except OSError:
            print(json.dumps({"state": "coordinator_unavailable"}), flush=True)
            if not args.serve:
                raise SystemExit(1) from None
        if not args.serve:
            break
        time.sleep(15)


if __name__ == "__main__":
    main()
