"""Encode the checked RGBA PNG staging files of one Windows image relay."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

PURPOSE = "Lossless WebP encoding of validated local PNGs for approved game image regions and synthetic fixtures"
DATA_SCOPE = "Local workspace validated PNG input and separate WebP output only; no network, credentials, game-root writes, installation, or redistribution"


def _encode(executable: Path, source: Path, output: Path, env: dict[str, str]) -> None:
    argv = [str(executable), "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-protocol_whitelist", "file", "-f", "image2", "-pattern_type", "none",
            "-c:v", "png", "-i", str(source), "-frames:v", "1", "-an", "-sn", "-dn",
            "-c:v", "libwebp", "-lossless", "1", "-pix_fmt", "bgra", "-f", "webp", str(output)]
    result = subprocess.run(argv, cwd=source.parent, env=env, capture_output=True, check=False)
    if result.returncode or not output.is_file():
        raise ValueError("E_BOUND_WEBP_ENCODE_FAILED")


def _dimensions(body: bytes) -> tuple[int, int]:
    if (len(body) < 26 or body[:4] != b"RIFF" or body[8:16] != b"WEBPVP8L"
            or int.from_bytes(body[4:8], "little") + 8 != len(body) or body[20] != 0x2f):
        raise ValueError("E_BOUND_WEBP_NOT_LOSSLESS")
    size, bits = struct.unpack_from("<IxI", body, 16)
    if size < 5 or 20 + size + (size & 1) != len(body) or bits >> 29:
        raise ValueError("E_BOUND_WEBP_NOT_LOSSLESS")
    return (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1


def _canonical_png(body: bytes, dimensions: tuple[int, int]) -> bytes:
    from game_l10n.image_region_pixels import _decode_png

    data = _decode_png(body, dimensions)
    width, height = dimensions
    data[::width * 4 + 1] = bytes(height)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(payload, zlib.crc32(kind))))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b""))


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from game_l10n.data_plane_task import _load_bound_data_plane_relay
    from game_l10n.image_region_pixels import _MAX_FILE
    from game_l10n.third_party_approval import require_third_party_approval

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--relay", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    work = args.workspace.resolve()
    relay_path = (work / args.relay).resolve()
    relay, task = _load_bound_data_plane_relay(work, relay_path)
    script = Path(__file__).resolve()
    binding = {"argv": [str(script), *sys.argv[1:]], "sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
    if (os.name != "nt" or relay["worker_role"] != "image_localizer"
            or not any(all(row.get(key) == value for key, value in binding.items())
                       for row in task.get("offline_processing", []))):
        raise ValueError("E_BOUND_WEBP_ENCODER_NOT_BOUND")
    executable = args.executable
    if executable.resolve(strict=True) != executable or executable.name.casefold() != "ffmpeg.exe":
        raise ValueError("E_BOUND_WEBP_EXECUTABLE_INVALID")
    approval = (work / args.approval).resolve(strict=True)
    if not approval.is_relative_to(work / ".game-l10n" / "third-party-approvals"):
        raise ValueError("E_BOUND_WEBP_APPROVAL_INVALID")
    record = json.loads(approval.read_bytes())
    if not record.get("name", "").startswith("FFmpeg "):
        raise ValueError("E_BOUND_WEBP_APPROVAL_INVALID")
    identity = {key: record[key] for key in ("name", "publisher", "source", "version")}
    inventory_path = work / task["input_image_inventory"]
    inventory_body = inventory_path.read_bytes()
    if hashlib.sha256(inventory_body).hexdigest() != task["input_image_inventory_sha256"]:
        raise ValueError("E_BOUND_WEBP_INVENTORY_CHANGED")
    output = work / task["output_image_root"]
    if output.resolve() != output or not output.is_relative_to(work) or not output.is_dir():
        raise ValueError("E_BOUND_WEBP_OUTPUT_INVALID")
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
    encoded = 0
    assets = json.loads(inventory_body)["assets"]
    for index, asset in enumerate(assets, 1):
        if asset["format"].casefold() not in {"webp", "image/webp"}:
            continue
        name = f"asset-{index:04d}"
        source, destination = output / (name + ".png"), output / (name + ".webp")
        if not source.is_file():
            continue
        if source.resolve() != source or destination.resolve() != destination or destination.exists():
            raise ValueError("E_BOUND_WEBP_OUTPUT_INVALID")
        with source.open("rb") as stream:
            png = stream.read(_MAX_FILE + 1)
        if len(png) > _MAX_FILE:
            raise ValueError("E_BOUND_WEBP_PNG_TOO_LARGE")
        dimensions = asset["width"], asset["height"]
        png = _canonical_png(png, dimensions)
        with TemporaryDirectory(dir=output) as temporary:
            stage = Path(temporary)
            validated, packed = stage / "input.png", stage / "encoded.webp"
            validated.write_bytes(png)
            with executable.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != args.sha256:
                    raise ValueError("E_BOUND_WEBP_EXECUTABLE_CHANGED")
            require_third_party_approval(work, approval, action="execute", **identity,
                                        digest="sha256:" + args.sha256, purpose=PURPOSE,
                                        data_scope=DATA_SCOPE, consume=True)
            _encode(executable, validated, packed, env)
            with packed.open("rb") as stream:
                result = stream.read(_MAX_FILE + 1)
            if len(result) > _MAX_FILE or _dimensions(result) != dimensions:
                raise ValueError("E_BOUND_WEBP_DIMENSIONS_CHANGED")
            packed.rename(destination)
        encoded += 1
    if not encoded:
        raise ValueError("E_BOUND_WEBP_NO_STAGED_PNG")
    print(json.dumps({"encoded_webp": encoded, "status": "encoded_not_visually_reviewed"}))


if __name__ == "__main__":
    main()
