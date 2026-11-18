"""Run the bound package CLI or an exact approved offline Python script."""
from pathlib import Path
import runpy
import sys


def offline_task() -> None:
    """Scope cooperative approved scripts; this is not an OS security sandbox."""
    import hashlib
    import json
    import os
    import sysconfig

    def deny() -> None:
        raise PermissionError("E_OFFLINE_PROCESS_SCOPE: unbound file, network or process access")

    if len(sys.argv) < 5 or sys.argv[3] != "--":
        deny()
    task_path = Path(sys.argv[2]).resolve(strict=True)
    task = json.loads(task_path.read_text(encoding="utf-8"))
    argv = sys.argv[4:]
    binding = next((item for item in task.get("offline_processing", [])
                    if item.get("argv") == argv), None)
    script = Path(argv[0])
    if (binding is None or not script.is_absolute() or script.resolve(strict=True) != script
            or script.suffix.lower() != ".py"):
        deny()
    with script.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != binding.get("sha256"):
            deny()
    workspace = next((parent.parent for parent in task_path.parents
                      if parent.name == ".game-l10n"), None)
    if workspace is None:
        deny()
    reads = {task_path, script}
    for asset in task.get("bound_local_assets", []):
        asset_path = Path(asset["path"])
        if not asset_path.is_absolute() or asset_path.resolve(strict=True) != asset_path:
            deny()
        with asset_path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != asset["sha256"]:
                deny()
        reads.add(asset_path)
    for key, value in task.items():
        if (key.startswith("input_") or key == "source_artifact") and isinstance(value, str):
            target = (workspace / value).resolve()
            if not target.is_relative_to(workspace):
                deny()
            reads.add(target)
    writes = {(workspace / task[key]).resolve() for key in ("output_artifact", "output_envelope")
              if isinstance(task.get(key), str)}
    output_root = ((workspace / task["output_image_root"]).resolve()
                   if isinstance(task.get("output_image_root"), str) else None)
    if any(not target.is_relative_to(workspace) for target in writes):
        deny()
    if output_root is not None and not output_root.is_relative_to(workspace):
        deny()
    if "expires_at_utc" in binding:
        from datetime import datetime, timezone

        try:
            expires = datetime.fromisoformat(binding["expires_at_utc"])
            if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                deny()
        except (TypeError, ValueError):
            deny()
    if binding.get("reusable") is not True:
        with task_path.open("rb") as handle:
            task_digest = hashlib.file_digest(handle, "sha256").hexdigest()
        identity = hashlib.sha256(json.dumps(
            [str(task_path), task_digest, argv], ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        directory = workspace / ".game-l10n/native/offline-processing"
        directory.mkdir(parents=True, exist_ok=True)
        if directory.resolve() != directory:
            deny()
        try:
            with (directory / f"{identity}.json").open("x", encoding="utf-8") as receipt:
                json.dump({"schema": "parr0t.offline-processing-use.v1",
                           "process_id": os.getpid()}, receipt)
        except FileExistsError:
            deny()
    runtime = {Path(value).resolve() for key, value in sysconfig.get_paths().items()
               if key in {"stdlib", "platstdlib", "purelib", "platlib"}}
    runtime.add(Path(__file__).resolve().parents[2])
    for value in sys.path:
        candidate = Path(value).resolve()
        if candidate.is_relative_to(Path(sys.base_prefix).resolve()) or candidate.is_relative_to(Path(sys.prefix).resolve()):
            runtime.add(candidate)
    # Never pass provider credentials or host session-routing environment to a script.
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"}}
    os.environ.clear()
    os.environ.update(environment)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    def allowed(value, *, writing=False):
        if isinstance(value, int):
            return value in {0, 1, 2} and not writing
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        target = Path(os.fsdecode(value)).resolve()
        output = target in writes or (output_root is not None and target.is_relative_to(output_root))
        return output if writing else output or target in reads or any(
            target.is_relative_to(root) for root in runtime)

    def audit(event, args):
        if (event.startswith(("socket.", "subprocess.", "ctypes."))
                or event in {"os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec", "os.spawn", "os.chdir"}):
            deny()
        if event == "open":
            writing = bool(args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if not allowed(args[0], writing=writing):
                deny()
        elif event in {"os.listdir", "os.scandir"}:
            if not allowed(args[0]):
                deny()
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
            if not allowed(args[0], writing=True):
                deny()
        elif event in {"os.rename", "os.link", "os.symlink"}:
            if not all(allowed(value, writing=True) for value in args[:2]):
                deny()

    sys.addaudithook(audit)
    sys.argv = argv
    runpy.run_path(str(script), run_name="__main__")


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.argv[1:2] == ["--offline-task"]:
    offline_task()
else:
    runpy.run_module("game_l10n.cli", run_name="__main__")
