"""Carry actual operator CLI configuration; policy identity never grants a model."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from .store import stable_hash

CONFIG_ENV = "PARR0T_OMP_OPERATOR_CONFIGS"
POLICY_ENV = "PARR0T_OMP_MODEL_POLICY_FINGERPRINT"


def operator_config_paths() -> list[Path]:
    raw = os.environ.get(CONFIG_ENV)
    if not raw:
        return []
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or any(
            not isinstance(value, str) or "\0" in value or not Path(value).is_absolute()
            for value in values
        ):
            raise ValueError()
        paths = [Path(value).resolve(strict=True) for value in values]
        if any(not value.is_file() for value in paths):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise ValueError("E_OMP_OPERATOR_CONFIG_INVALID") from None
    return paths


def host_policy_fingerprint(selected: str | None = None) -> str | None:
    selected = selected if selected is not None else os.environ.get(POLICY_ENV)
    if selected is not None and (
        not isinstance(selected, str) or re.fullmatch(r"[a-f0-9]{64}", selected) is None
    ):
        raise ValueError("E_NATIVE_HOST_POLICY_FINGERPRINT_INVALID")
    configurations = []
    for path in operator_config_paths():
        with path.open("rb") as stream:
            configurations.append([str(path), hashlib.file_digest(stream, "sha256").hexdigest()])
    if configurations:
        return stable_hash({"operator_configs": configurations, "observed_policy": selected})
    return selected
