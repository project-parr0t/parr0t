"""Start a fresh, guarded OMP control plane without replaying an old session."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from .control_plane import CONTROL_HANDOFF_PATH, control_plane_entry_state, control_plane_state


def _startup_prompt(workspace: Path, core: Path, mode: str = "rotation") -> str:
    return (
        "Parr0t의 기존 게임 현지화 작업을 새 text-free 최상위 제어 세션으로 이어가라. "
        "이 세션은 이전 세션의 대화·요약·하위 에이전트 기록을 상속하거나 열람하지 않는다. "
        f"코어 위치: {core.as_posix()}. 외부 작업 폴더: {workspace.as_posix()}. "
        "먼저 코어 AGENTS.md, docs/agent-usage.md, docs/control-plane-contract.md 및 "
        "작업 폴더 AGENTS.md를 읽고 메타데이터 상태만 확인하라. "
        +
        (
            f"정식 인계 영수증 {CONTROL_HANDOFF_PATH}만 사용하여 새 세션 재개 절차를 수행하라. "
            "실제로 새 세션임을 확인한 뒤에만 fresh-session-confirmed를 사용하라. "
            if mode == "rotation" else
            "새 history-free 작업 세션이다. status의 entry gate가 pending이면 계약을 읽고 "
            "source-unopened 조건을 확인한 뒤 enter-control-plane을 수행하라. "
            "이미 ready이면 재진입하거나 resume-control-plane을 실행하지 마라. "
            "메타데이터와 승인된 정책만 확인하고 기록된 사용자 작업 의도가 없으면 "
            "현지화 작업을 시작하지 말고 사용자에게 현재 요청을 물어라. "
        )
        +
        "게임 폴더는 읽기 전용이며 최상위는 게임 내용·번역 가능성을 직접 조사하지 않는다. "
        "기존 한국어·엔진·전체 번역 범위는 코어가 준비한 격리 릴레이/데이터 워커만 조사한다. "
        "기존 한국어 개선을 포함하여 기록된 사용자 요청 범위만 수행하고 범위를 새로 추가하거나 줄이지 마라. "
        "기존 SDK와 캐시·완료된 증거·대상 ID를 재사용하고 보고된 미완료 범위만 이어가라. "
        "코드·식별자·플레이스홀더·제어 코드·게임 동작을 보존하라. "
        "폰트·이미지 작업은 사용자 범위와 계약이 허용할 때만 수행하라. "
        "설정된 기본 번역 재시도와 사전 폴백을 완료한 뒤 남은 항목에만 설정된 후순위 폴백 경로를 사용하라. "
        "실패 항목 때문에 다른 항목을 중단하거나 범위를 줄이지 마라. "
        "현재 세션의 OMP 실행 가드가 등록된 후 native-runtime을 다시 바인딩하고 기존 정확한 모델 매핑을 보존하라. "
        "릴레이/워커는 launch_contract의 메시지 격리·parr0t_exec 실행 규약을 따르며 agent:// 메시지를 보내지 않는다. "
        "최상위가 새 릴레이를 시작할 때 코어가 반환한 상대 릴레이 경로를 PARR0T_OMP_RELAY=경로 형식의 단독 줄로 전달하라. "
        "게임에 설치·실행하지 말고 외부 작업 폴더에 복원 가능한 패치를 완성·검증한 뒤 보고하라. "
        "기존 게임 작업의 Git 선택 및 잠긴 대상 언어·제공자 정책을 보존하라. 사용량 상한을 새로 만들거나 요청하지 마라. "
        "중간 계획이나 단계 완료를 최종 완료로 취급하지 말고 local_patch_ready 또는 실제 외부 차단까지 진행하라."
    )


def _plan_command(workspace: Path, model: str, mode: str = "rotation") -> tuple[list[str], Path, Path]:
    """Validate prerequisites and plan a fresh session without writing anything."""
    workspace = workspace.resolve()
    if mode not in {"rotation", "workspace"}:
        raise ValueError("E_OMP_CONTROL_MODE_REQUIRED")
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._/+\-]*"
        r"(?:@[A-Za-z0-9][A-Za-z0-9._/\-]*)?(?::[A-Za-z0-9][A-Za-z0-9._\-]*)?", model
    ):
        raise ValueError("E_OMP_CONTROL_EXACT_MODEL_REQUIRED")
    if not (workspace / "game-l10n.json").is_file():
        raise ValueError("E_OMP_CONTROL_WORKSPACE_REQUIRED")
    state = control_plane_state(workspace)
    control_plane_entry_state(workspace)
    if mode == "rotation":
        if state["status"] != "rotation_required":
            raise ValueError("E_OMP_CONTROL_ROTATION_REQUIRED")
        if not (workspace / CONTROL_HANDOFF_PATH).is_file():
            raise ValueError("E_OMP_CONTROL_HANDOFF_REQUIRED")
    elif state["status"] == "rotation_required":
        raise ValueError("E_OMP_CONTROL_ROTATION_REQUIRED: use --mode rotation")
    executable = shutil.which("omp")
    if executable is None:
        raise ValueError("E_OMP_CONTROL_EXECUTABLE_REQUIRED")
    core = Path(__file__).resolve().parents[2]
    guard = Path(__file__).with_name("templates") / "omp-isolation-guard.mjs"
    if not guard.is_file():
        raise ValueError("E_OMP_CONTROL_GUARD_REQUIRED")
    launch_dir = workspace / ".game-l10n" / "omp-control" / uuid4().hex
    session_dir = launch_dir / "sessions"
    command = [
        executable, "--cwd", str(workspace), "--model", model,
        "--session-dir", str(session_dir), "--no-prewalk", "--no-title",
        "--trusted-extension", str(guard.resolve()),
        "--parr0t-workspace", str(workspace), _startup_prompt(workspace, core, mode),
    ]
    return command, launch_dir, guard


def _launch(
    workspace: Path, model: str, startup_seconds: float = 45.0,
    *, mode: str = "rotation", foreground: bool = False,
) -> dict:
    workspace = workspace.resolve()
    if os.name != "nt" and not foreground:
        raise ValueError("E_OMP_CONTROL_TERMINAL_REQUIRED: use --foreground")
    command, launch_dir, guard = _plan_command(workspace, model, mode)
    session_dir = launch_dir / "sessions"
    session_dir.mkdir(parents=True)
    record = {
        "schema": "parr0t.omp-control-launch.v1", "status": "starting",
        "model": model, "session_directory": str(session_dir),
        "handoff_receipt": CONTROL_HANDOFF_PATH if mode == "rotation" else None,
        "mode": mode, "process_id": None,
    }
    record_path = launch_dir / "launch.json"
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    environment = dict(os.environ)
    # Registration belongs to the new process, never the caller's host session.
    environment.pop("PARR0T_OMP_GUARD_SESSION", None)
    environment.pop("PARR0T_OMP_GUARD_WORKSPACE", None)
    environment["PARR0T_OMP_CONTROL_DIRECTORY"] = str(launch_dir)
    process = subprocess.Popen(
        command, cwd=workspace, env=environment,
        creationflags=0 if foreground else subprocess.CREATE_NEW_CONSOLE, close_fds=True,
    )
    record["launcher_process_id"] = process.pid
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if foreground:
        record["exit_code"] = process.wait()
        record["status"] = "exited"
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return {**record, "launch_record": str(record_path)}
    registration = launch_dir / "ready.json"
    guard_digest = sha256(guard.read_bytes()).hexdigest()
    deadline = time.monotonic() + startup_seconds
    while time.monotonic() < deadline:
        if registration.is_file():
            try:
                proof = json.loads(registration.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                proof = {}
            if (
                isinstance(proof, dict)
                and proof.get("schema") == "parr0t.omp-control-ready.v1"
                and proof.get("launch_id") == launch_dir.name
                and type(proof.get("process_id")) is int and proof["process_id"] > 0
                and proof.get("guard_sha256") == guard_digest
                and isinstance(proof.get("session_id"), str)
                and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", proof["session_id"])
            ):
                record["status"] = "guard_registered"
                record["process_id"] = proof["process_id"]
                record["session_id"] = proof["session_id"]
                break
        if process.poll() not in {None, 0}:
            record["status"] = "startup_failed"
            break
        time.sleep(0.1)
    else:
        # This is a startup observation limit, not a worker or job deadline.
        # Keep the process and its identity; never restart it automatically.
        record["status"] = "startup_pending"
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return {**record, "launch_record": str(record_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--model", required=True, help="Exact approved provider/model identifier")
    parser.add_argument("--mode", choices=("rotation", "workspace"), default="rotation")
    parser.add_argument("--foreground", action="store_true", help="Use the current terminal")
    parser.add_argument("--print-command", action="store_true", help="Print JSON argv without launching")
    args = parser.parse_args(argv)
    try:
        if args.print_command:
            command, _, _ = _plan_command(args.workspace, args.model, args.mode)
            print(json.dumps(command, ensure_ascii=False))
            return 0
        result = _launch(args.workspace, args.model, mode=args.mode, foreground=args.foreground)
    except (OSError, ValueError) as error:
        issue = str(error).split(":", 1)[0]
        if not re.fullmatch(r"E_[A-Z_]+", issue):
            issue = "E_OMP_CONTROL_START_FAILED"
        print(json.dumps({"status": "error", "issue_code": issue}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    if args.foreground:
        return result["exit_code"]
    return 0 if result["status"] == "guard_registered" else 1


if __name__ == "__main__":
    raise SystemExit(main())
