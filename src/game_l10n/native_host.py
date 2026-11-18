"""Opt-in native host API transport. No model response crosses the CLI boundary."""
from __future__ import annotations

import json
import http.client
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .store import stable_hash

PROFILE_SCHEMA = "parr0t.native-host-profile.v1"
HOST_PROTOCOL = "parr0t.native-host-api.v1"
PROBE_SCHEMA = "parr0t.native-host-probe.v1"
HOST_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}")
OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
MODEL_SNAPSHOT_PATH = ".game-l10n/native/host-models.json"
MODEL_SNAPSHOT_SCHEMA = "parr0t.native-host-models.v1"


def host_model_snapshot(work_dir: Path) -> dict | None:
    """Read public host identities only; availability never grants authorization."""
    from .model_policy import exact_native_model
    path = work_dir / MODEL_SNAPSHOT_PATH
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or set(value) - {"schema", "host", "session_id", "process_id", "models", "current_model"}
            or value["schema"] != MODEL_SNAPSHOT_SCHEMA or value["host"] != "omp"
            or not isinstance(value["session_id"], str) or not value["session_id"].strip()
            or type(value["process_id"]) is not int or value["process_id"] < 1
            or not isinstance(value["models"], list)
            or any(
                not isinstance(row, dict) or set(row) != {"provider", "id", "selector"}
                or not exact_native_model(row["provider"]) or "/" in row["provider"]
                or not exact_native_model(row["id"])
                or row["selector"] != row["provider"] + "/" + row["id"]
                for row in value["models"]
            )
            or ("current_model" in value and not exact_native_model(value["current_model"]))
        ):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("E_NATIVE_HOST_MODELS_INVALID") from None
    return value


def resolve_native_model(work_dir: Path, host: str, model: str) -> str:
    """Resolve bare OMP IDs by one exact catalog match, never by provider guessing."""
    from .model_policy import exact_native_model
    if not exact_native_model(model):
        raise ValueError("E_NATIVE_RUNTIME_INVALID: exact model selector required")
    if host != "omp":
        return model
    try:
        snapshot = host_model_snapshot(work_dir)
    except ValueError:
        if "/" in model:
            return model
        raise
    rows = snapshot["models"] if snapshot else []
    if any(row["selector"] == model or model.startswith(row["provider"] + "/") for row in rows):
        return model
    matches = {row["selector"] for row in rows if row["id"] == model}
    if not matches:
        # Host qualifiers decorate an exact identity; they are not a provider prefix hint.
        identity = model.split("@", 1)[0]
        if "@" not in model and ":" in identity:
            identity = identity.rsplit(":", 1)[0]
        suffix = model[len(identity):]
        if suffix:
            matches = {row["selector"] + suffix for row in rows if row["id"] == identity}
    if len(matches) > 1:
        raise ValueError("E_NATIVE_MODEL_AMBIGUOUS: select the exact provider-qualified model")
    if matches:
        return matches.pop()
    if "/" in model:
        return model  # Explicit qualified choices do not require catalog membership.
    raise ValueError("E_NATIVE_MODEL_METADATA_REQUIRED: no exact host model identity match")


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _profile_path(work_dir: Path, host: str) -> Path:
    if not isinstance(host, str) or not HOST_ID.fullmatch(host):
        raise ValueError("E_NATIVE_HOST_PROFILE_INVALID")
    return work_dir / ".game-l10n/native/hosts" / (host + ".json")


def _validate_endpoint(endpoint: str, api_key_env: str | None) -> None:
    if not isinstance(endpoint, str) or (api_key_env is not None and not isinstance(api_key_env, str)):
        raise ValueError("E_NATIVE_HOST_PROFILE_INVALID")
    try:
        parts = urllib.parse.urlsplit(endpoint)
        _ = parts.port
    except ValueError:
        raise ValueError("E_NATIVE_HOST_PROFILE_INVALID") from None
    if (parts.scheme not in {"http", "https"} or not parts.netloc
            or not parts.hostname or any(c.isspace() for c in endpoint)
            or parts.username or parts.password or parts.query or parts.fragment
            or (api_key_env is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]*", api_key_env))):
        raise ValueError("E_NATIVE_HOST_PROFILE_INVALID")
    if api_key_env and parts.scheme != "https" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("E_NATIVE_HOST_CREDENTIAL_TRANSPORT_INVALID")


def configure_native_host(work_dir: Path, host: str, endpoint: str,
                          api_key_env: str | None = None) -> dict:
    _validate_endpoint(endpoint, api_key_env)
    payload = {"schema": PROFILE_SCHEMA, "host": host, "endpoint": endpoint.rstrip("/"),
               "api_key_env": api_key_env, "protocol": HOST_PROTOCOL}
    value = {**payload, "fingerprint": stable_hash(payload)}
    path = _profile_path(work_dir, host)
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != value:
        raise ValueError("E_NATIVE_HOST_PROFILE_IMMUTABLE: use another host id")
    _write(path, value)
    return {"host": host, "profile": path.relative_to(work_dir).as_posix(),
            "fingerprint": value["fingerprint"], "ready": False}


def native_host_profile(work_dir: Path, host: str) -> dict:
    try:
        value = json.loads(_profile_path(work_dir, host).read_text(encoding="utf-8"))
        unsigned = {key: item for key, item in value.items() if key != "fingerprint"}
        if (set(value) != {"schema", "host", "endpoint", "api_key_env", "protocol", "fingerprint"}
                or value["schema"] != PROFILE_SCHEMA or value["host"] != host
                or value["protocol"] != HOST_PROTOCOL or value["fingerprint"] != stable_hash(unsigned)):
            raise ValueError()
        _validate_endpoint(value["endpoint"], value["api_key_env"])
        return value
    except (OSError, ValueError, TypeError, AttributeError):
        raise ValueError("E_NATIVE_HOST_PROFILE_INVALID") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NativeHostClient:
    def __init__(self, profile: dict):
        self.profile = profile

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        variable = self.profile["api_key_env"]
        if variable:
            key = os.environ.get(variable)
            if not key:
                raise ValueError("E_NATIVE_HOST_CREDENTIAL_REQUIRED")
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(self.profile["endpoint"] + path,
                  data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                  headers=headers, method=method)
        try:
            # No implicit generation deadline. Do not log bodies, credentials,
            # host exception prose, or raw worker responses to the parent.
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=None) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
            raise ValueError("E_NATIVE_HOST_TRANSPORT_ERROR") from None

    def capabilities(self) -> dict:
        value = self.request("GET", "/capabilities")
        required = ("independent_workers", "broker_only_delivery", "fresh_history",
                    "explicit_models", "atomic_wave_start", "idempotent_requests", "shared_workspace")
        if (value.get("protocol") != HOST_PROTOCOL or value.get("host") != self.profile["host"]
                or value.get("worker_runtime") != "native_agent_api"
                or any(value.get(key) is not True for key in required)
                or type(value.get("max_workers")) is not int or value["max_workers"] < 1
                or not isinstance(value.get("models"), list)
                or not all(isinstance(model, str) for model in value["models"])):
            raise ValueError("E_NATIVE_HOST_CAPABILITY_UNSUPPORTED")
        return {key: value[key] for key in ("max_workers", "models")}

    def wave(self, request_id: str, payload: dict, *, reconcile: bool) -> dict:
        if not OPAQUE_ID.fullmatch(request_id):
            raise ValueError("E_NATIVE_HOST_REPLY_INVALID")
        if reconcile:
            value = self.request("GET", "/waves/by-request/" + request_id)
            if value.get("request_id") != request_id:
                raise ValueError("E_NATIVE_HOST_REPLY_INVALID")
            if value.get("status") != "not_found":
                return value
            if value.get("retry_safe") is not True:
                raise ValueError("E_NATIVE_HOST_LAUNCH_UNCERTAIN")
        return self.request("POST", "/waves", payload)


def probe_path(work_dir: Path, host: str, route: str) -> Path:
    if not re.fullmatch(r"native-[a-z0-9][a-z0-9._-]*", route):
        raise ValueError("E_NATIVE_HOST_PROBE_INVALID")
    return _profile_path(work_dir, host).with_name(host + "-" + route + "-probe.json")


def verified_host_probe(work_dir: Path, runtime: dict, route: str) -> dict:
    profile = native_host_profile(work_dir, runtime["host"])
    try:
        value = json.loads(probe_path(work_dir, runtime["host"], route).read_text(encoding="utf-8"))
        unsigned = {key: item for key, item in value.items() if key != "fingerprint"}
        if (value.get("schema") != PROBE_SCHEMA or value.get("ready") is not True
                or value.get("fingerprint") != stable_hash(unsigned)
                or value.get("profile_fingerprint") != profile["fingerprint"]
                or value.get("model") != runtime["models"][route]
                or value.get("route") != route
                or value.get("control_generation") != runtime["control_generation"]
                or type(value.get("max_workers")) is not int or value["max_workers"] < 1):
            raise ValueError()
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("E_NATIVE_HOST_PROBE_REQUIRED: run probe-native-host --live for the selected route") from None
