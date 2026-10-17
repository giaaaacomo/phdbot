"""Portable DDW experiment: explicit inputs, isolated pages, immutable receipts."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import ipaddress
import json
import math
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from phd_searcher.ddw import CONTRACT_VERSION
from phd_searcher.pipeline.schema_quality import schema_quality_issues

MAX_BYTES = 1_000_000


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def build_id() -> str:
    shared = Path(__file__).parents[1] / "pipeline" / "schema_quality.py"
    return digest(Path(__file__).read_bytes() + shared.read_bytes())


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def public_url(url: str, hosts: list[str]) -> tuple[str, str]:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or not host or host not in hosts or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None or parsed.fragment
            or any(ord(c) < 33 or ord(c) > 126 for c in url)):
        raise ValueError("url_not_allowed")
    return host, parsed.path or "/"


def validate(task: dict[str, Any]) -> None:
    if set(task) != {"version", "cycle", "expires_at", "timeout_seconds", "allowed_hosts", "pages"}:
        raise ValueError("invalid_task_fields")
    if task["version"] != CONTRACT_VERSION or not isinstance(task["cycle"], str) or not 1 <= len(task["cycle"]) <= 128:
        raise ValueError("invalid_contract")
    expiry = datetime.fromisoformat(task["expires_at"])
    if expiry.tzinfo is None:
        raise ValueError("expiry_requires_timezone")
    timeout = task["timeout_seconds"]
    if type(timeout) not in (int, float) or not 0.05 <= timeout <= 10:
        raise ValueError("invalid_timeout")
    hosts = task["allowed_hosts"]
    if not isinstance(hosts, list) or len(hosts) > 8 or any(not isinstance(h, str) for h in hosts):
        raise ValueError("invalid_hosts")
    pages = task["pages"]
    if not isinstance(pages, list) or not 1 <= len(pages) <= 8:
        raise ValueError("invalid_page_count")
    urls = set()
    for page in pages:
        if set(page) not in ({"url", "schema", "schema_sha256"}, {"url", "schema", "schema_sha256", "html", "html_sha256"}):
            raise ValueError("invalid_page_fields")
        public_url(page["url"], hosts)
        if page["url"] in urls:
            raise ValueError("duplicate_url")
        urls.add(page["url"])
        schema = page["schema"]
        if digest(encoded(schema)) != page["schema_sha256"]:
            raise ValueError("schema_hash_mismatch")
        # A documented subset of PHDBOT's CSS schema, never silently approximate
        # nested/regex/transformation/rendering semantics from the full engine.
        if set(schema) - {"name", "baseSelector", "fields", "baseFields"} or schema_quality_issues(schema):
            raise ValueError("unsupported_schema")
        if not isinstance(schema.get("baseSelector"), str) or not 1 <= len(schema["baseSelector"]) <= 300:
            raise ValueError("invalid_selector")
        names = set()
        for field in [*schema.get("fields", []), *schema.get("baseFields", [])]:
            if set(field) - {"name", "selector", "type", "attribute"} or field.get("type") not in {"text", "attribute"}:
                raise ValueError("unsupported_field")
            if not isinstance(field.get("name"), str) or field["name"] in names:
                raise ValueError("invalid_field_name")
            names.add(field["name"])
            if field in schema.get("fields", []) and not isinstance(field.get("selector"), str):
                raise ValueError("missing_field_selector")
            if field["type"] == "attribute" and not isinstance(field.get("attribute"), str):
                raise ValueError("missing_attribute")
        if "title" not in names or len(names) > 16:
            raise ValueError("invalid_fields")
        if "html" in page:
            raw = page["html"].encode()
            if len(raw) > MAX_BYTES or digest(raw) != page["html_sha256"]:
                raise ValueError("fixture_hash_or_size")


def fetch(url: str, hosts: list[str], timeout: float) -> tuple[bytes, dict[str, Any]]:
    return fetch_public(url, hosts, timeout, max_bytes=MAX_BYTES, content_type="text/html")


def fetch_public(url: str, hosts: list[str], timeout: float, *, max_bytes: int, content_type: str) -> tuple[bytes, dict[str, Any]]:
    host, path = public_url(url, hosts)
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ips = [str(entry[4][0]) for entry in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("non_public_address")
    # Connect to the validated address, retain hostname for TLS and Host header.
    # No proxies, redirects, cookies, authentication, browser or subsequent DNS.
    connection = http.client.HTTPSConnection(host, timeout=timeout)
    raw_socket = socket.create_connection((ips[0], 443), timeout=timeout)
    try:
        connection.sock = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=host)
        query = urlsplit(url).query
        connection.request("GET", path + ("?" + query if query else ""), headers={
            "User-Agent": "PHDBOT-DDW-experiment/1", "Accept": content_type, "Accept-Encoding": "identity",
        })
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"http_{response.status}")
        if response.getheader("Content-Encoding", "identity") != "identity":
            raise ValueError("unsupported_encoding")
        if content_type not in response.getheader("Content-Type", "").lower():
            raise ValueError("unsupported_content_type")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("body_limit")
        return body, {"status": response.status, "content_type": response.getheader("Content-Type")}
    finally:
        connection.close()
        raw_socket.close()


def extract(raw: bytes, schema: dict[str, Any]) -> list[dict[str, str]]:
    soup = BeautifulSoup(raw, "html.parser")
    bases = soup.select(schema["baseSelector"], limit=501)
    if not bases or len(bases) > 500:
        raise ValueError("empty_or_excessive_extraction")
    result = []
    for base in bases:
        item = {}
        for base_field, fields in ((True, schema.get("baseFields", [])), (False, schema.get("fields", []))):
            for field in fields:
                node = base if base_field else base.select_one(field["selector"])
                value = "" if node is None else (node.get_text(strip=True) if field["type"] == "text"
                                                    else node.get(field["attribute"], ""))
                if not isinstance(value, str) or len(value) > 20_000:
                    raise ValueError("unsupported_extracted_value")
                item[field["name"]] = value
        if not item.get("title", "").strip():
            raise ValueError("missing_title")
        result.append(item)
    return result


def page_result(task: dict[str, Any], index: int, network: bool) -> dict[str, Any]:
    page = task["pages"][index]
    observed_at = now()
    if "html" in page:
        raw, metadata = page["html"].encode(), {"transport": "fixture"}
    elif network:
        raw, metadata = fetch(page["url"], task["allowed_hosts"], task["timeout_seconds"])
    else:
        raise ValueError("network_disabled")
    return {"url": page["url"], "observed_at": observed_at, "metadata": metadata,
            "schema_sha256": page["schema_sha256"], "body_sha256": digest(raw),
            "body_base64": base64.b64encode(raw).decode(), "items": extract(raw, page["schema"])}


def child(task_path: Path, index: int, output: Path, network: bool) -> None:
    task = json.loads(task_path.read_bytes())
    validate(task)
    if sys.platform != "win32":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
        seconds = math.ceil(task["timeout_seconds"]) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
    try:
        result = {"state": "ok", "data": page_result(task, index, network)}
        if len(encoded(result)) > 4 * MAX_BYTES:
            raise ValueError("output_limit")
    except Exception as exc:
        # Do not include arbitrary exception text/HTML/response headers in logs.
        result = {"state": "error", "error_type": type(exc).__name__}
    write_json(output, result)


def run_page(task_path: Path, index: int, output: Path, network: bool, timeout: float) -> dict[str, Any]:
    command = [sys.executable, "-m", "phd_searcher.ddw.worker", "_page", str(task_path), str(index), str(output)]
    if network:
        command.append("--allow-network")
    env = {"PYTHONPATH": str(Path(__file__).parents[2]), "PYTHONIOENCODING": "utf-8"}
    for key in ("SYSTEMROOT", "PATH"):
        if key in os.environ:
            env[key] = os.environ[key]
    process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=timeout)
        if process.returncode != 0 or not output.exists():
            return {"state": "error", "error_type": "ChildFailure"}
        return cast(dict[str, Any], json.loads(output.read_bytes()))
    except subprocess.TimeoutExpired:
        return {"state": "timeout", "error_type": "DeadlineExceeded"}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def verify(directory: Path, expected_work_key: str) -> dict[str, Any]:
    task = json.loads((directory / "task.json").read_bytes())
    validate(task)
    receipt = json.loads((directory / "receipt.json").read_bytes())
    if digest(encoded(task)) != expected_work_key or receipt["work_key"] != expected_work_key:
        raise ValueError("work_identity_mismatch")
    if receipt["version"] != CONTRACT_VERSION or receipt["state"] not in {"complete", "partial", "interrupted", "expired"}:
        raise ValueError("unfinished_receipt")
    pages = receipt["pages"]
    if [page["index"] for page in pages] != list(range(len(task["pages"]))):
        raise ValueError("incomplete_page_accounting")
    for page in pages:
        if page["state"] not in {"ok", "error", "timeout", "unprocessed"}:
            raise ValueError("invalid_page_state")
        if page["state"] == "ok":
            path = directory / f"page-{page['index']}.json"
            if path.stat().st_size > 4 * MAX_BYTES:
                raise ValueError("output_limit")
            raw = path.read_bytes()
            if digest(raw) != page["sha256"]:
                raise ValueError("result_hash_mismatch")
            result = json.loads(raw)["data"]
            source = task["pages"][page["index"]]
            body = base64.b64decode(result["body_base64"], validate=True)
            if (len(body) > MAX_BYTES or digest(body) != result["body_sha256"]
                    or result["url"] != source["url"] or result["schema_sha256"] != source["schema_sha256"]):
                raise ValueError("result_identity_mismatch")
    if (receipt["state"] == "complete") != all(page["state"] == "ok" for page in pages):
        raise ValueError("false_completion")
    return cast(dict[str, Any], receipt)


def run(task: dict[str, Any], spool: Path, attempt: int = 1, network: bool = False,
        *, blocked_hosts: frozenset[str] = frozenset()) -> tuple[Path, dict[str, Any]]:
    validate(task)
    root = spool / digest(encoded(task))
    root.mkdir(parents=True, exist_ok=True)
    claim = root / "active"
    try:
        claim.mkdir()
    except FileExistsError as exc:
        raise ValueError("work_already_claimed_inspect_before_recovery") from exc
    try:
        return _run_claimed(task, spool, attempt, network, blocked_hosts)
    finally:
        claim.rmdir()


def _run_claimed(task: dict[str, Any], spool: Path, attempt: int, network: bool,
                 blocked_hosts: frozenset[str]) -> tuple[Path, dict[str, Any]]:
    validate(task)
    if attempt not in (1, 2, 3):
        raise ValueError("attempt_budget")
    work_key = digest(encoded(task))
    directory = spool / work_key / str(attempt)
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        directory.mkdir()  # exclusive claim; never steal an unfinished attempt
    except FileExistsError:
        receipt = verify(directory, work_key)
        if receipt["build_id"] != build_id():
            raise ValueError("build_mismatch") from None
        return directory, receipt
    write_json(directory / "task.json", task)
    receipt = {"version": CONTRACT_VERSION, "work_key": work_key, "attempt": attempt,
               "build_id": build_id(), "started_at": now(), "state": "running",
               "pages": [{"index": i, "state": "unprocessed"} for i in range(len(task["pages"]))]}
    write_json(directory / "receipt.json", receipt)
    expiry = datetime.fromisoformat(task["expires_at"])
    started = time.monotonic()
    try:
        for i, _page in enumerate(task["pages"]):
            if datetime.now(UTC) >= expiry:
                receipt["state"] = "expired"
                break
            if time.monotonic() - started >= 60:
                break
            timeout = min(task["timeout_seconds"], 60 - (time.monotonic() - started),
                          (expiry - datetime.now(UTC)).total_seconds())
            if timeout <= 0:
                receipt["state"] = "expired"
                break
            path = directory / f"page-{i}.json"
            page_started = time.monotonic()
            if network and "html" not in _page and urlsplit(_page["url"]).hostname in blocked_hosts:
                result = {"state": "error", "error_type": "AccessPolicyBlocked"}
            else:
                result = run_page(directory / "task.json", i, path, network, timeout)
            entry = {"index": i, "state": result["state"],
                     "elapsed_seconds": round(time.monotonic() - page_started, 6)}
            if result["state"] == "ok":
                entry["sha256"] = digest(path.read_bytes())
            else:
                entry["error_type"] = result["error_type"]
            receipt["pages"][i] = entry
            write_json(directory / "receipt.json", receipt)
            # Serial requests, including across different assigned hosts.
            if network and i + 1 < len(task["pages"]):
                time.sleep(1)
    except KeyboardInterrupt:
        receipt["state"] = "interrupted"
    finally:
        if receipt["state"] == "running":
            receipt["state"] = "complete" if all(p["state"] == "ok" for p in receipt["pages"]) else "partial"
        receipt["finished_at"] = now()
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(directory / "receipt.json", receipt)
    return directory, verify(directory, work_key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["run", "verify", "_page"])
    parser.add_argument("task_or_directory", type=Path)
    parser.add_argument("extra", nargs="*")
    parser.add_argument("--spool", type=Path, default=Path("ddw-output"))
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args()
    if args.mode == "_page":
        child(args.task_or_directory, int(args.extra[0]), Path(args.extra[1]), args.allow_network)
        return
    if args.mode == "verify":
        receipt = verify(args.task_or_directory, args.extra[0])
    else:
        if args.task_or_directory.stat().st_size > 9 * MAX_BYTES:
            parser.error("task_too_large")
        def interrupt(_signum: int, _frame: object) -> None:
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupt)
        _, receipt = run(json.loads(args.task_or_directory.read_bytes()), args.spool, args.attempt, args.allow_network)
    print(json.dumps({k: receipt[k] for k in ("work_key", "attempt", "state", "build_id")}))
    raise SystemExit(0 if receipt["state"] == "complete" else 2)


if __name__ == "__main__":
    main()
