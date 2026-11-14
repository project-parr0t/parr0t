"""Synthetic host registration; does not replace any isolation validation."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from game_l10n.control_plane import control_plane_state
from game_l10n.omp_isolation import ISOLATION_ENV, ISOLATION_PATH, ISOLATION_SCHEMA, guard_sha256


def install_omp_isolation(work_dir: Path, *, session_id: str | None = None) -> dict:
    """Write a proof bound to the actual installed template and current generation.

    Caller must bind PARR0T_OMP_GUARD_SESSION to returned session_id. For host
    smoke setup, invoke this inside that process and export the returned session.
    This synthetic fixture is not a substitute for loading the real host guard.
    """
    proof = {
        "schema": ISOLATION_SCHEMA,
        "control_generation": control_plane_state(work_dir)["generation"],
        "session_id": session_id or uuid4().hex,
        "process_id": os.getpid(),
        "guard_sha256": guard_sha256(),
    }
    path = work_dir / ISOLATION_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proof), encoding="utf-8")
    return proof


def bind_omp_isolation(test_case, work_dir: Path) -> dict:
    proof = install_omp_isolation(work_dir)
    environment = patch.dict(os.environ, {ISOLATION_ENV: proof["session_id"]})
    environment.start()
    test_case.addCleanup(environment.stop)
    return proof
