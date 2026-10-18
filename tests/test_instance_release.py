from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from game_l10n.instance_release import (
    INSTANCE_RELEASE_NAME,
    INSTANCE_RELEASE_SCHEMA,
    InstanceReleaseError,
    validate_instance_release,
)
from game_l10n.release_checkpoint import CHECKPOINT_NAME, CHECKPOINT_SCHEMA
from game_l10n.steam import MANIFEST_NAME, build_steam_package


def _digest(value: bytes | str) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _git(root: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return process.stdout.strip()


def _checkpoint() -> dict[str, object]:
    target = "TARGET_FIXTURE_TEXT"
    return {
        "schema": CHECKPOINT_SCHEMA,
        "patch_id": "fixture-localization",
        "release_id": "r2",
        "sdk": {
            "branch": "sdk-fixture-1.0",
            "commit": "a" * 40,
            "adapter_fingerprint": _digest("fixture-adapter"),
        },
        "game_builds": [
            {"id": "fixture-build-1", "binding_fingerprint": _digest("fixture-binding")}
        ],
        "source_inventory": {
            "method": "fixture-primary",
            "fingerprint": _digest("fixture-inventory"),
            "target_count": 1,
        },
        "context": {
            "fingerprint": _digest("fixture-context"),
            "review_policy_fingerprint": _digest("fixture-review-policy"),
        },
        "entries": [
            {
                "id": "fixture-target-1",
                "source_sha256": _digest("SOURCE_FIXTURE_TEXT"),
                "target": target,
                "target_sha256": _digest(target),
                "location_fingerprint": _digest("fixture-location"),
                "dependency_fingerprint": _digest("fixture-dependency"),
                "review_fingerprint": _digest("fixture-review"),
                "status": "reviewed",
                "dependencies": [],
            }
        ],
        "locale_fallback": {
            "status": "unsupported",
            "mode": "none",
            "fallback_locale": "",
            "placeholder": "",
            "verified": False,
            "reason_code": "fixture_no_safe_runtime_fallback",
        },
    }


def _write_fixture(root: Path, *, readme_replacement: bool = False) -> tuple[Path, Path]:
    instance = root / "instance"
    private = root / "private"
    payload = private / "payload"
    instance.mkdir()
    payload.mkdir(parents=True)
    _git(instance, "init")
    _git(instance, "config", "user.email", "fixture@example.invalid")
    _git(instance, "config", "user.name", "Fixture")
    _git(instance, "config", "commit.gpgsign", "false")
    (instance / "README.md").write_text("# Existing project\n\nOriginal instructions.\n", encoding="utf-8")
    _git(instance, "add", "README.md")
    _git(instance, "commit", "-m", "Initial project")
    base_commit = _git(instance, "rev-parse", "HEAD")

    checkpoint_bytes = (
        json.dumps(_checkpoint(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    private.mkdir(exist_ok=True)
    private_checkpoint = private / "release-checkpoint.json"
    private_checkpoint.write_bytes(checkpoint_bytes)
    (payload / "fixture.txt").write_text("PATCHED_FIXTURE", encoding="utf-8")
    spec = {
        "patch_id": "fixture-localization",
        "release_id": "r2",
        "display_name": "Fixture Game",
        "checkpoint_file": "release-checkpoint.json",
        "steam": {
            "app_id": "123456",
            "install_dir": "Fixture Game",
            "required_files": [],
        },
    }
    spec_path = private / "package.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    package = private / "Fixture-Game-Korean-r2.zip"
    build_steam_package(payload, spec_path, package)
    with zipfile.ZipFile(package, "r") as archive:
        manifest_bytes = archive.read(MANIFEST_NAME)

    (instance / CHECKPOINT_NAME).write_bytes(checkpoint_bytes)
    if readme_replacement:
        readme = "# Generated replacement\n"
    else:
        readme = "# Existing project\n\nOriginal instructions.\n\n## Install\n\nUse the Release asset.\n"
    (instance / "README.md").write_text(readme, encoding="utf-8")
    package_bytes = package.read_bytes()
    metadata = {
        "schema": INSTANCE_RELEASE_SCHEMA,
        "patch_id": "fixture-localization",
        "release_id": "r2",
        "display_name": "Fixture Game",
        "language": "Korean",
        "patch_version": "r2",
        "tag": "r2",
        "release_title": "Fixture Game-Korean-r2 (Game Version 1.0.0)",
        "target": {"os": "windows", "architecture": "x64", "prerequisites": []},
        "supported_game_builds": [
            {
                "id": "fixture-build-1",
                "display_version": "1.0.0",
                "display_type": "version",
            }
        ],
        "commits": {"sdk": "a" * 40, "translation_source": None},
        "asset": {
            "format": "parr0t-steam-package/v4",
            "name": package.name,
            "bytes": len(package_bytes),
            "sha256": _digest(package_bytes),
            "manifest_sha256": _digest(manifest_bytes),
        },
        "checkpoint": {
            "schema": CHECKPOINT_SCHEMA,
            "bytes": len(checkpoint_bytes),
            "sha256": _digest(checkpoint_bytes),
        },
        "readme": {"base_commit": base_commit, "mode": "append_only"},
        "glue_paths": [],
    }
    (instance / INSTANCE_RELEASE_NAME).write_text(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _git(instance, "add", "README.md", CHECKPOINT_NAME, INSTANCE_RELEASE_NAME)
    _git(instance, "commit", "-m", "Prepare release index")
    return instance, package


class InstanceReleaseTests(unittest.TestCase):
    def test_gate_binds_lean_branch_to_exact_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance, package = _write_fixture(Path(directory))
            result = validate_instance_release(
                instance,
                package,
                user_confirmed_success=True,
                user_approved_publication=True,
            )

        self.assertEqual(result["status"], "ready")
        self.assertEqual(
            result["release_title"],
            "Fixture Game-Korean-r2 (Game Version 1.0.0)",
        )
        self.assertEqual(result["patch_version"], "r2")
        self.assertEqual(result["language"], "Korean")
        self.assertEqual(
            result["tracked_files"],
            ["parr0t-instance-release.json", "parr0t-release-checkpoint.json", "README.md"],
        )

    def test_gate_rejects_distribution_artifact_in_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance, package = _write_fixture(Path(directory))
            shutil.copy2(package, instance / package.name)
            _git(instance, "add", package.name)
            _git(instance, "commit", "-m", "Incorrectly commit asset")

            with self.assertRaisesRegex(InstanceReleaseError, "forbidden artifact"):
                validate_instance_release(
                    instance,
                    package,
                    user_confirmed_success=True,
                    user_approved_publication=True,
                )

    def test_gate_rejects_replaced_readme(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance, package = _write_fixture(Path(directory), readme_replacement=True)

            with self.assertRaisesRegex(InstanceReleaseError, "append-only"):
                validate_instance_release(
                    instance,
                    package,
                    user_confirmed_success=True,
                    user_approved_publication=True,
                )

    def test_gate_rejects_asset_changed_after_metadata_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance, package = _write_fixture(Path(directory))
            with package.open("ab") as stream:
                stream.write(b"POST_VALIDATION_DRIFT")

            with self.assertRaisesRegex(InstanceReleaseError, "asset binding"):
                validate_instance_release(
                    instance,
                    package,
                    user_confirmed_success=True,
                    user_approved_publication=True,
                )

    def test_gate_requires_separate_success_and_publication_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance, package = _write_fixture(Path(directory))

            with self.assertRaisesRegex(InstanceReleaseError, "success"):
                validate_instance_release(
                    instance,
                    package,
                    user_confirmed_success=False,
                    user_approved_publication=True,
                )
            with self.assertRaisesRegex(InstanceReleaseError, "publication"):
                validate_instance_release(
                    instance,
                    package,
                    user_confirmed_success=True,
                    user_approved_publication=False,
                )

    def test_gate_rejects_semver_patch_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance, package = _write_fixture(Path(directory))
            metadata_path = instance / INSTANCE_RELEASE_NAME
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["patch_version"] = "v1.2.0"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            _git(instance, "add", INSTANCE_RELEASE_NAME)
            _git(instance, "commit", "-m", "Use invalid semantic version")

            with self.assertRaisesRegex(InstanceReleaseError, "positive revision"):
                validate_instance_release(
                    instance,
                    package,
                    user_confirmed_success=True,
                    user_approved_publication=True,
                )

    def test_gate_rejects_title_without_language_revision_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance, package = _write_fixture(Path(directory))
            metadata_path = instance / INSTANCE_RELEASE_NAME
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["release_title"] = "Fixture Game Patch r2 (Game Version 1.0.0)"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            _git(instance, "add", INSTANCE_RELEASE_NAME)
            _git(instance, "commit", "-m", "Use invalid release title")

            with self.assertRaisesRegex(InstanceReleaseError, "release_title must be exactly"):
                validate_instance_release(
                    instance,
                    package,
                    user_confirmed_success=True,
                    user_approved_publication=True,
                )

    def test_gate_accepts_game_build_title_variant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance, package = _write_fixture(Path(directory))
            metadata_path = instance / INSTANCE_RELEASE_NAME
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["supported_game_builds"][0]["display_type"] = "build"
            metadata["supported_game_builds"][0]["display_version"] = "3340"
            metadata["release_title"] = "Fixture Game-Korean-r2 (Game Build 3340)"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            _git(instance, "add", INSTANCE_RELEASE_NAME)
            _git(instance, "commit", "-m", "Use game build title")

            result = validate_instance_release(
                instance,
                package,
                user_confirmed_success=True,
                user_approved_publication=True,
            )

        self.assertEqual(
            result["release_title"],
            "Fixture Game-Korean-r2 (Game Build 3340)",
        )


if __name__ == "__main__":
    unittest.main()
