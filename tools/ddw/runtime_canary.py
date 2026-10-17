"""One hosted Runtime canary: isolation probes, existing client, safe artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from phd_searcher.ddw.client import Client, NoRedirect


def probe(endpoint: str, worker_id: str, token: str, job_id: int) -> dict:
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or parsed.path != "/v1/ddw" or parsed.query or parsed.fragment
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not parsed.hostname or not parsed.hostname.endswith(".ts.net")):
        raise ValueError("private_https_required")
    origin = f"https://{parsed.hostname}"
    opener = build_opener(ProxyHandler({}), NoRedirect())
    headers = {"Authorization": "Bearer " + token, "X-DDW-Worker": worker_id,
               "Content-Type": "application/json"}
    checks = [
        ("GET", "/", 403), ("GET", "/dashboard", 403), ("GET", "/docs", 403),
        ("GET", "/v1/ddw/status", 403), ("GET", "/v1/ddw/events", 403),
        ("POST", "/v1/ddw/workers", 403), ("POST", "/v1/ddw/jobs", 403),
        ("POST", f"/v1/ddw/jobs/{job_id}/cancel", 403),
        ("POST", f"/v1/ddw/jobs/{job_id + 1}/1/result", 403),
        ("POST", f"/v1/ddw/jobs/{job_id}/2/result", 403),
        ("POST", "/v1/ddw/claim?extra=1", 403),
    ]
    for method, path, expected in checks:
        request = Request(origin + path, data=b"{}" if method == "POST" else None,
                          method=method, headers=headers)
        try:
            with opener.open(request, timeout=5):
                raise ValueError("forbidden_route_reachable")
        except HTTPError as exc:
            if exc.code != expected or exc.headers.get("X-PHDBOT-DDW-Gateway") != "scoped-runtime-v1":
                raise ValueError("unexpected_isolation_response") from None
            exc.close()
    # Missing credentials must never reach the coordinator/claim a job.
    try:
        with opener.open(Request(endpoint + "/claim", data=b"{}",
                                 headers={"Content-Type": "application/json"}), timeout=5):
            raise ValueError("anonymous_claim_allowed")
    except HTTPError as exc:
        if exc.code != 401 or exc.headers.get("X-PHDBOT-DDW-Gateway") != "scoped-runtime-v1":
            raise ValueError("anonymous_claim_probe_failed") from None
        exc.close()
    for port in (22, 80, 8003, 8766, 5432, 6333, 11434):
        try:
            connection = socket.create_connection((parsed.hostname, port), timeout=1)
        except (TimeoutError, ConnectionRefusedError):
            continue
        else:
            connection.close()
            raise ValueError("non_worker_port_reachable")
    return {"denied_http_routes": len(checks), "anonymous_claim_denied": True,
            "non_worker_ports_unreachable": 7}


def export(spool: Path, destination: Path, job_id: int) -> None:
    """Only authenticated-envelope-shaped ciphertext, never the entire outbox."""
    for root in (spool, spool.parent / f"{spool.name}-job-{job_id}-1"):
        path = root / "result" / "evidence.sealed.json"
        if not path.exists():
            continue
        raw = path.read_bytes()
        value = json.loads(raw)
        if (len(raw) > 12_000_000 or set(value) != {"version", "envelope", "signature"}
                or set(value["envelope"]) != {"version", "context", "nonce", "wrapped_key", "ciphertext"}):
            raise ValueError("unsafe_artifact_shape")
        (destination / "evidence.sealed.json").write_bytes(raw)
        (destination / "checksums.json").write_text(json.dumps({"evidence.sealed.json": hashlib.sha256(raw).hexdigest()}))


def main() -> None:
    os.umask(0o077)
    endpoint, identity = os.environ["DDW_ENDPOINT"], os.environ["DDW_WORKER_ID"]
    job_id, commit = int(os.environ["DDW_JOB_ID"]), os.environ["GITHUB_SHA"]
    token, key = os.environ.pop("DDW_TOKEN"), os.environ.pop("DDW_SIGNING_KEY").encode()
    if not token or not key or job_id <= 0:
        raise ValueError("missing_canary_inputs")
    out = Path("ddw-runtime-artifacts")
    out.mkdir(exist_ok=False)
    result = {"job_id": job_id, "source_commit": commit, "acquisition": "not_started",
              "import": "not_checked", "searchability": "not_checked"}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ddw-runtime-") as temporary:
        spool = Path(temporary) / "outbox"
        try:
            result["isolation"] = probe(endpoint, identity, token, job_id)
            client = Client(endpoint, identity, token, key, spool, commit)
            state = client.step()  # Exactly one step: no serve/poll/retry/acquire loop.
            if state != "accepted":
                raise ValueError("canary_not_accepted")
            archive = spool.parent / f"outbox-job-{job_id}-1"
            ack = json.loads((archive / "ack.json").read_bytes())
            result.update(acquisition="complete", **{"import": ack["state"]})
            # Deliver the same ciphertext, without a second claim or fetch.
            replay = client.request(f"/jobs/{job_id}/1/result", (archive / "result/evidence.sealed.json").read_bytes())
            if not replay or replay.get("state") != "accepted" or replay.get("replayed") is not True:
                raise ValueError("idempotent_replay_failed")
            result["idempotent_replay"] = True
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            raise
        finally:
            export(spool, out, job_id)
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            (out / "receipt.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # No traceback/payload/URLs/credentials in public CI logs.
        print(json.dumps({"state": "canary_stopped", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
