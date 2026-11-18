"""Preserve the original text-free user request outside reusable core code."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from .control_plane import _atomic_json
from .errors import ConfigurationError
from .store import exclusive_file_lock

USER_REQUEST_SCHEMA = "parr0t.user-request.v1"
USER_REQUEST_PATH = ".game-l10n/user-request.json"
USER_REQUEST_DIRECTORY = ".game-l10n/user-requests"


def _request_bytes(path: Path) -> bytes:
    try:
        if path.is_symlink():
            raise ValueError("linked request")
        data = path.read_bytes()
        text = data.decode("utf-8")
        if not text.strip() or "\0" in text:
            raise ValueError("empty or invalid request")
        return data
    except (OSError, ValueError) as exc:
        raise ConfigurationError("E_USER_REQUEST_INVALID: a complete UTF-8 request is required") from exc


def user_request_binding(work_dir: Path) -> dict:
    """Return only verified metadata; never echo the user's request in status."""
    path = work_dir / USER_REQUEST_PATH
    try:
        path.lstat()
    except FileNotFoundError as exc:
        raise ConfigurationError("E_USER_REQUEST_REQUIRED: record the original text-free user request") from exc
    try:
        if path.is_symlink():
            raise ValueError("linked binding")
        binding = json.loads(path.read_text(encoding="utf-8"))
        fields = {"schema", "path", "sha256", "bytes", "user_requested", "text_free_confirmed"}
        if not isinstance(binding, dict) or set(binding) != fields:
            raise ValueError("invalid binding")
        digest = binding["sha256"]
        if (
            binding["schema"] != USER_REQUEST_SCHEMA
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or binding["path"] != f"{USER_REQUEST_DIRECTORY}/{digest}.txt"
            or type(binding["bytes"]) is not int or binding["bytes"] <= 0
            or binding["user_requested"] is not True
            or binding["text_free_confirmed"] is not True
        ):
            raise ValueError("invalid binding")
        payload = work_dir / binding["path"]
        payload.resolve().relative_to(work_dir.resolve())
        data = _request_bytes(payload)
        if len(data) != binding["bytes"] or sha256(data).hexdigest() != digest:
            raise ValueError("changed request")
    except (OSError, ValueError) as exc:
        raise ConfigurationError("E_USER_REQUEST_INVALID: original request binding changed") from exc
    return binding


def record_user_request(
    work_dir: Path, source: Path, *, user_requested: bool, text_free_confirmed: bool,
) -> dict:
    """Bind once; a restart must not replace the original with a summary or logs."""
    if user_requested is not True or text_free_confirmed is not True:
        raise ConfigurationError("E_USER_REQUEST_ASSERTION_REQUIRED: confirm original text-free user instructions")
    work_dir = work_dir.resolve()
    if not (work_dir / "game-l10n.json").is_file():
        raise ConfigurationError("E_USER_REQUEST_WORKSPACE_REQUIRED")
    source = source if source.is_absolute() else work_dir / source
    try:
        source.resolve().relative_to(work_dir)
    except ValueError as exc:
        raise ConfigurationError("E_USER_REQUEST_INPUT_REQUIRED: stage the original request inside the workspace") from exc
    data = _request_bytes(source)
    digest = sha256(data).hexdigest()
    binding = {
        "schema": USER_REQUEST_SCHEMA,
        "path": f"{USER_REQUEST_DIRECTORY}/{digest}.txt",
        "sha256": digest, "bytes": len(data),
        "user_requested": True, "text_free_confirmed": True,
    }
    with exclusive_file_lock(work_dir / ".game-l10n/user-request.lock", issue_code="E_USER_REQUEST_BUSY"):
        metadata = work_dir / USER_REQUEST_PATH
        try:
            metadata.lstat()
        except FileNotFoundError:
            pass
        else:
            previous = user_request_binding(work_dir)
            if previous != binding:
                raise ConfigurationError("E_USER_REQUEST_CONFLICT: the original request is immutable")
            return previous
        payload = work_dir / binding["path"]
        payload.parent.mkdir(parents=True, exist_ok=True)
        try:
            with payload.open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            if _request_bytes(payload) != data:
                raise ConfigurationError("E_USER_REQUEST_INVALID: request snapshot changed")
        _atomic_json(metadata, binding)
    return binding
