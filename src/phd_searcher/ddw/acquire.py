"""Bounded acquisition with encrypted file delivery; no canonical import."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from phd_searcher.ddw import delivery, worker
from phd_searcher.ddw.delivery import MAX_ENVELOPE, MAX_PLAIN, seal, unseal  # noqa: F401
from phd_searcher.ddw.policy import policy


def read_manifest(path: Path, recipient: Path, *, check_expiry: bool = True) -> dict[str, Any]:
    manifest = cast(dict[str, Any], json.loads(path.read_bytes()))
    task = manifest["task"]
    worker.validate(task)
    if (manifest["version"] != 1 or manifest["operation"] not in {"private-evidence-canary", "ddw-evidence-v1"}
            or not 1 <= len(task["pages"]) <= 10
            or worker.digest(worker.encoded(task)) != manifest["task_sha256"]
            or worker.digest(recipient.read_bytes()) != manifest["recipient_sha256"]
            or any("html" in page for page in task["pages"])):
        raise ValueError("invalid_evidence_assignment")
    if check_expiry and datetime.now(UTC) >= datetime.fromisoformat(task["expires_at"]):
        raise ValueError("expired_evidence_assignment")
    return manifest


def check_policies(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], frozenset[str]]:
    task = manifest["task"]
    policies, blocked = [], set()
    # One robots read/host; every URL was prechecked against these exact bytes.
    # Changed policy fails closed instead of relying on a stale authorisation.
    for host, expected in manifest["robots_sha256"].items():
        url = next(page["url"] for page in task["pages"] if urlsplit(page["url"]).hostname == host)
        observed = policy(url)
        accepted = observed["allowed"] and observed.get("sha256") == expected
        observed["preflight_accepted"] = accepted
        if not accepted:
            blocked.add(host)
        policies.append(observed)
    return policies, frozenset(blocked)


def execute(manifest: dict[str, Any], public_pem: bytes, output: Path,
            signing_private_pem: bytes | None = None) -> dict[str, Any]:
    context = manifest.get("context")
    if manifest["operation"] == "ddw-evidence-v1" and (not isinstance(context, dict) or signing_private_pem is None
                or context.get("operation") != "ddw-evidence-v1"
                or context.get("task_sha256") != manifest["task_sha256"]
                or context.get("recipient_sha256") != manifest["recipient_sha256"]
                or context.get("source_commit") != os.environ.get("DDW_SOURCE_COMMIT")
                or type(context.get("attempt")) is not int or context["attempt"] < 1):
        raise ValueError("invalid_central_assignment")
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    task = manifest["task"]
    policies, blocked = check_policies(manifest)
    with tempfile.TemporaryDirectory(prefix="phdbot-private-evidence-") as temporary:
        acquisition_started = time.perf_counter()
        directory, receipt = worker.run(task, Path(temporary), attempt=context["attempt"] if context else 1,
                                        network=True, blocked_hosts=blocked)
        acquisition_seconds = time.perf_counter() - acquisition_started
        verification_started = time.perf_counter()
        worker.verify(directory, manifest["task_sha256"])
        verification_seconds = time.perf_counter() - verification_started
        payload = {"task": task, "receipt": receipt,
                   "results": {str(entry["index"]): json.loads((directory / f"page-{entry['index']}.json").read_bytes())
                               for entry in receipt["pages"] if entry["state"] == "ok"}}
        context = context or {"operation": "private-evidence-canary", "task_sha256": manifest["task_sha256"],
                   "recipient_sha256": manifest["recipient_sha256"],
                   "source_commit": os.environ.get("DDW_SOURCE_COMMIT", "local-uncommitted")}
        seal_started = time.perf_counter()
        sealed = seal(payload, public_pem, context)
        sealing_seconds = time.perf_counter() - seal_started
        if signing_private_pem is not None:
            sealed = delivery.sign(sealed, signing_private_pem)
        worker.write_json(output / "evidence.sealed.json", sealed)
        summary = {"version": 1, "context": context, "build_id": receipt["build_id"],
                   "state": receipt["state"], "started_at": receipt["started_at"],
                   "finished_at": receipt["finished_at"], "policies": policies,
                   "pages": receipt["pages"], "acquisition_seconds": acquisition_seconds,
                   "verification_seconds": verification_seconds, "sealing_seconds": sealing_seconds,
                   "elapsed_seconds": time.perf_counter() - started,
                   "private_json_bytes": len(worker.encoded(payload)),
                   "compressed_private_bytes": len(gzip.compress(worker.encoded(payload))),
                   "sealed_bytes": (output / "evidence.sealed.json").stat().st_size,
                   "html_request_limit": len(task["pages"]), "robots_request_limit": len(policies),
                   "retry_limit": 0, "plaintext_exported": False}
        worker.write_json(output / "summary.json", summary)
        worker.write_json(output / "checksums.json", {name: worker.digest((output / name).read_bytes())
                                                     for name in ("summary.json", "evidence.sealed.json")})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("recipient", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--signing-key", type=Path, help="assigned private Ed25519 key; never publish")
    args = parser.parse_args()
    if not args.allow_network:
        parser.error("explicit --allow-network required")
    result = execute(read_manifest(args.manifest, args.recipient), args.recipient.read_bytes(), args.output,
                     args.signing_key.read_bytes() if args.signing_key else None)
    print(json.dumps({"state": result["state"], "pages": result["pages"]}))
    raise SystemExit(0 if any(p["state"] == "ok" for p in result["pages"]) else 2)


if __name__ == "__main__":
    main()
