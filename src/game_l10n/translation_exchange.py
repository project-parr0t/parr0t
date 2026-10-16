from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .control_plane import CONTROL_ENVELOPE_SCHEMA, validate_control_envelope
from .errors import GameL10nError
from .models import FailureCode, Segment
from .privacy import is_within


EXCHANGE_SCHEMA = "parr0t.translation-exchange/v1"
RECEIPT_SCHEMA = "parr0t.translation-exchange-receipt/v1"
REQUESTS_NAME = "requests.jsonl"
SUCCESS_NAME = "success.jsonl"
FAILURE_NAME = "failure.jsonl"
MANIFEST_NAME = "exchange-manifest.json"
RECEIPT_NAME = "exchange-receipt.json"
ENVELOPE_NAME = "control-envelope.json"
EXCHANGE_MODES = frozenset({"manual", "fallback"})
SOURCE_QUEUES = frozenset({"primary_pending", "translation_fallback_pending"})
FAILURE_CODES = frozenset(
    item.value
    for item in FailureCode
    if item
    not in {
        FailureCode.SOURCE_PRESERVED,
        FailureCode.USER_ACCEPTED_AFTER_REVIEW_REFUSAL,
    }
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class TranslationExchangeError(GameL10nError):
    pass


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def _jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(_canonical(row) for row in rows)


def _read_jsonl(path: Path, *, allowed_keys: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeError as error:
        raise TranslationExchangeError(f"Exchange file is not UTF-8: {path.name}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise TranslationExchangeError(f"Blank JSONL row is not allowed: {path.name}:{line_number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise TranslationExchangeError(f"Invalid JSONL row: {path.name}:{line_number}") from error
        if not isinstance(row, dict) or set(row) != allowed_keys:
            raise TranslationExchangeError(f"Unexpected exchange row shape: {path.name}:{line_number}")
        rows.append(row)
    return rows


def _context(segment: Segment) -> dict[str, Any]:
    value = {
        "speaker": segment.speaker,
        "scene": segment.scene,
        "previous": segment.previous,
        "next": segment.next,
        "metadata": segment.metadata,
    }
    try:
        _canonical(value)
    except (TypeError, ValueError) as error:
        raise TranslationExchangeError(f"Segment context is not JSON serializable: {segment.id}") from error
    return value


def prepare_translation_exchange(
    work_dir: Path,
    segments: list[Segment],
    output_dir: Path,
    *,
    exchange_id: str,
    mode: str,
    source_queue: str,
) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    output_dir = output_dir.resolve()
    if not is_within(output_dir, work_dir):
        raise TranslationExchangeError("Translation exchange must stay inside the private workspace")
    if output_dir.exists():
        if not output_dir.is_dir() or any(output_dir.iterdir()):
            raise TranslationExchangeError("Translation exchange directory must be new or empty")
    if not _SAFE_ID.fullmatch(exchange_id):
        raise TranslationExchangeError("exchange_id must be a safe identifier of at most 80 characters")
    if mode not in EXCHANGE_MODES:
        raise TranslationExchangeError("mode must be manual or fallback")
    if source_queue not in SOURCE_QUEUES:
        raise TranslationExchangeError("source_queue is not a supported translation queue")
    if not segments or len(segments) > 2000:
        raise TranslationExchangeError("An exchange must contain 1 to 2000 ordered targets")
    target_ids = [segment.id for segment in segments]
    if len(target_ids) != len(set(target_ids)) or any(
        not isinstance(item, str)
        or not item
        or len(item) > 512
        or any(character.isspace() or ord(character) < 32 for character in item)
        for item in target_ids
    ):
        raise TranslationExchangeError("Exchange target IDs must be unique opaque IDs")

    rows: list[dict[str, Any]] = []
    for ordinal, segment in enumerate(segments):
        context = _context(segment)
        rows.append(
            {
                "target_id": segment.id,
                "ordinal": ordinal,
                "source": segment.source,
                "source_sha256": _sha256(segment.source.encode("utf-8")),
                "context": context,
                "context_sha256": _sha256(_canonical(context)),
            }
        )
    requests = _jsonl_bytes(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_bytes(output_dir / REQUESTS_NAME, requests)
    _atomic_bytes(output_dir / SUCCESS_NAME, b"")
    _atomic_bytes(output_dir / FAILURE_NAME, b"")
    manifest = {
        "schema": EXCHANGE_SCHEMA,
        "exchange_id": exchange_id,
        "mode": mode,
        "source_queue": source_queue,
        "target_count": len(rows),
        "request_sha256": _sha256(requests),
        "files": {
            "requests": REQUESTS_NAME,
            "success": SUCCESS_NAME,
            "failure": FAILURE_NAME,
            "receipt": RECEIPT_NAME,
            "control_envelope": ENVELOPE_NAME,
        },
    }
    _atomic_json(output_dir / MANIFEST_NAME, manifest)
    return {
        "schema": EXCHANGE_SCHEMA,
        "exchange_id": exchange_id,
        "mode": mode,
        "target_count": len(rows),
        "bundle": output_dir.relative_to(work_dir).as_posix(),
        "next_action": "complete_success_and_failure_jsonl_without_changing_requests_or_manifest",
    }


def _load_manifest(bundle: Path) -> dict[str, Any]:
    path = bundle / MANIFEST_NAME
    if not path.is_file():
        raise TranslationExchangeError("Exchange manifest is missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema",
        "exchange_id",
        "mode",
        "source_queue",
        "target_count",
        "request_sha256",
        "files",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise TranslationExchangeError("Exchange manifest shape is invalid")
    if payload["schema"] != EXCHANGE_SCHEMA or payload["mode"] not in EXCHANGE_MODES:
        raise TranslationExchangeError("Exchange manifest schema or mode is unsupported")
    if not isinstance(payload["exchange_id"], str) or not _SAFE_ID.fullmatch(payload["exchange_id"]):
        raise TranslationExchangeError("Exchange ID is invalid")
    if payload["source_queue"] not in SOURCE_QUEUES:
        raise TranslationExchangeError("Exchange source queue is unsupported")
    if not isinstance(payload["target_count"], int) or not 1 <= payload["target_count"] <= 2000:
        raise TranslationExchangeError("Exchange target count is invalid")
    if not isinstance(payload["request_sha256"], str) or not _DIGEST.fullmatch(payload["request_sha256"]):
        raise TranslationExchangeError("Exchange request digest is invalid")
    expected_files = {
        "requests": REQUESTS_NAME,
        "success": SUCCESS_NAME,
        "failure": FAILURE_NAME,
        "receipt": RECEIPT_NAME,
        "control_envelope": ENVELOPE_NAME,
    }
    if payload["files"] != expected_files:
        raise TranslationExchangeError("Exchange filenames are fixed and must not be changed")
    return payload


def seal_translation_exchange(work_dir: Path, bundle: Path) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    bundle = bundle.resolve()
    if not is_within(bundle, work_dir) or not bundle.is_dir():
        raise TranslationExchangeError("Translation exchange must be a private workspace directory")
    manifest = _load_manifest(bundle)
    request_path = bundle / REQUESTS_NAME
    success_path = bundle / SUCCESS_NAME
    failure_path = bundle / FAILURE_NAME
    if not request_path.is_file() or not success_path.is_file() or not failure_path.is_file():
        raise TranslationExchangeError("Exchange request, success, and failure files are required")
    request_bytes = request_path.read_bytes()
    if _sha256(request_bytes) != manifest["request_sha256"]:
        raise TranslationExchangeError("Immutable exchange requests were modified")

    request_rows = _read_jsonl(
        request_path,
        allowed_keys={"target_id", "ordinal", "source", "source_sha256", "context", "context_sha256"},
    )
    if len(request_rows) != manifest["target_count"]:
        raise TranslationExchangeError("Exchange target count no longer matches the manifest")
    requests: dict[str, dict[str, Any]] = {}
    for ordinal, row in enumerate(request_rows):
        target_id = row["target_id"]
        if not isinstance(target_id, str) or not target_id or target_id in requests or row["ordinal"] != ordinal:
            raise TranslationExchangeError("Exchange requests must keep unique IDs and stable order")
        if not isinstance(row["source"], str) or _sha256(row["source"].encode("utf-8")) != row["source_sha256"]:
            raise TranslationExchangeError("Exchange source digest is invalid")
        if not isinstance(row["context"], dict) or _sha256(_canonical(row["context"])) != row["context_sha256"]:
            raise TranslationExchangeError("Exchange context digest is invalid")
        requests[target_id] = row

    success_rows = _read_jsonl(
        success_path,
        allowed_keys={"target_id", "status", "text", "route", "attempt", "source_sha256", "context_sha256"},
    )
    failure_rows = _read_jsonl(
        failure_path,
        allowed_keys={"target_id", "status", "failure_code", "route", "attempt", "source_sha256", "context_sha256", "retryable"},
    )
    resolved: set[str] = set()
    for row in success_rows:
        target_id = row["target_id"]
        if not isinstance(target_id, str):
            raise TranslationExchangeError("Success target ID is invalid")
        request = requests.get(target_id)
        if request is None or target_id in resolved:
            raise TranslationExchangeError("Success target is unknown or duplicated")
        if row["status"] != "success" or not isinstance(row["text"], str) or not row["text"].strip():
            raise TranslationExchangeError("Success rows require non-empty translated text")
        if not isinstance(row["route"], str) or not row["route"] or len(row["route"]) > 120:
            raise TranslationExchangeError("Success route is invalid")
        if not isinstance(row["attempt"], int) or isinstance(row["attempt"], bool) or row["attempt"] < 1:
            raise TranslationExchangeError("Success attempt must be a positive integer")
        if row["source_sha256"] != request["source_sha256"] or row["context_sha256"] != request["context_sha256"]:
            raise TranslationExchangeError("Success row is not bound to the current source and context")
        resolved.add(target_id)
    for row in failure_rows:
        target_id = row["target_id"]
        if not isinstance(target_id, str):
            raise TranslationExchangeError("Failure target ID is invalid")
        request = requests.get(target_id)
        if request is None or target_id in resolved:
            raise TranslationExchangeError("Failure target is unknown or duplicated")
        if (
            row["status"] != "failed"
            or not isinstance(row["failure_code"], str)
            or row["failure_code"] not in FAILURE_CODES
        ):
            raise TranslationExchangeError("Failure row status or code is invalid")
        if not isinstance(row["route"], str) or not row["route"] or len(row["route"]) > 120:
            raise TranslationExchangeError("Failure route is invalid")
        if not isinstance(row["attempt"], int) or isinstance(row["attempt"], bool) or row["attempt"] < 1:
            raise TranslationExchangeError("Failure attempt must be a positive integer")
        if not isinstance(row["retryable"], bool):
            raise TranslationExchangeError("Failure retryable must be a boolean")
        if row["source_sha256"] != request["source_sha256"] or row["context_sha256"] != request["context_sha256"]:
            raise TranslationExchangeError("Failure row is not bound to the current source and context")
        resolved.add(target_id)

    pending = len(requests) - len(resolved)
    unresolved = len(failure_rows) + pending
    response_sha256 = _sha256(success_path.read_bytes() + b"\0" + failure_path.read_bytes())
    receipt_relative = (bundle / RECEIPT_NAME).relative_to(work_dir).as_posix()
    envelope_relative = (bundle / ENVELOPE_NAME).relative_to(work_dir).as_posix()
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "exchange_id": manifest["exchange_id"],
        "mode": manifest["mode"],
        "status": "complete" if unresolved == 0 else "fallback_required",
        "target_count": len(requests),
        "success_count": len(success_rows),
        "failure_count": len(failure_rows),
        "pending_count": pending,
        "request_sha256": manifest["request_sha256"],
        "response_sha256": response_sha256,
        "control_envelope": envelope_relative,
    }
    _atomic_json(bundle / RECEIPT_NAME, receipt)
    transitions: list[dict[str, Any]] = []
    if success_rows:
        transitions.append({"from": manifest["source_queue"], "to": "review_pending", "count": len(success_rows)})
    if unresolved:
        transitions.append(
            {"from": manifest["source_queue"], "to": "translation_fallback_pending", "count": unresolved}
        )
    envelope = {
        "schema": CONTROL_ENVELOPE_SCHEMA,
        "worker_role": "translator",
        "stage": "translation",
        "status": "success" if unresolved == 0 else "fallback_required",
        "target_ids": list(requests),
        "counts": {
            "total": len(requests),
            "success": len(success_rows),
            "error": len(failure_rows),
            "pending": pending,
            "fallback_required": unresolved,
        },
        "issue_codes": [] if unresolved == 0 else ["fallback_required"],
        "artifact_paths": [receipt_relative],
        "queue_transitions": transitions,
        "route_evidence_path": "",
    }
    _atomic_json(bundle / ENVELOPE_NAME, envelope)
    validate_control_envelope(
        work_dir,
        bundle / ENVELOPE_NAME,
        expected_role="translator",
        expected_stage="translation",
    )
    return {
        "schema": RECEIPT_SCHEMA,
        "exchange_id": manifest["exchange_id"],
        "status": receipt["status"],
        "target_count": len(requests),
        "success_count": len(success_rows),
        "failure_count": len(failure_rows),
        "pending_count": pending,
        "control_envelope": envelope_relative,
    }
