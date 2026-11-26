"""Prepare and encode the exact WebP assets bound to a Windows image relay."""
from __future__ import annotations

import argparse
import base64
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

_DECODE = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName PresentationCore, WindowsBase
$dimensions = (ConvertFrom-Json ([Console]::In.ReadLine()))
$stream = [IO.File]::OpenRead('input.webp')
try {
  $decoder = [Windows.Media.Imaging.BitmapDecoder]::Create($stream, 'PreservePixelFormat', 'OnLoad')
  if ($decoder.Frames.Count -ne 1) { throw 'frame count' }
  $f = $decoder.Frames[0]
  if ($f.PixelWidth -ne $dimensions[0] -or $f.PixelHeight -ne $dimensions[1]) { throw 'dimensions' }
  $frame = [Windows.Media.Imaging.FormatConvertedBitmap]::new($f, [Windows.Media.PixelFormats]::Bgra32, $null, 0)
  $stride = 4 * $frame.PixelWidth
  $pixels = [byte[]]::new($stride * $frame.PixelHeight)
  $frame.CopyPixels($pixels, $stride, 0)
  [Console]::OpenStandardOutput().Write($pixels, 0, $pixels.Length)
} finally { $stream.Dispose() }
"""


def _read_webp(body: bytes, dimensions: tuple[int, int], env: dict[str, str], cwd: Path) -> bytearray:
    width, height = dimensions
    if (len(body) < 26 or body[:4] != b"RIFF" or body[8:12] != b"WEBP"
            or int.from_bytes(body[4:8], "little") + 8 != len(body)
            or body[12:16] not in {b"VP8 ", b"VP8L", b"VP8X"}
            or (body[12:16] == b"VP8X" and body[20] & 2)
            or width < 1 or height < 1 or width * height > 16_777_216):
        raise ValueError("E_BOUND_WEBP_SOURCE_INVALID")
    # Use a private snapshot; never let the decoder follow a source path from a process argument.
    with TemporaryDirectory(dir=cwd) as temporary:
        source = Path(temporary) / "input.webp"
        source.write_bytes(body)
        shell = Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        proc = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                               base64.b64encode(_DECODE.encode("utf-16le")).decode("ascii")],
                              cwd=temporary, env=env, input=(json.dumps(dimensions) + "\n").encode("ascii"),
                              capture_output=True, check=False)
    if proc.returncode or len(proc.stdout) != width * height * 4:
        raise ValueError("E_BOUND_WEBP_DECODE_FAILED")
    result = bytearray(proc.stdout)
    result[0::4], result[2::4] = result[2::4], result[0::4]
    return result


def _check_pixels(before: bytearray, after: bytearray, width: int, regions: list[list[int]]) -> None:
    if before[3::4] != after[3::4]:
        raise ValueError("E_BOUND_WEBP_ALPHA_CHANGED")
    if before == after:
        raise ValueError("E_BOUND_WEBP_UNCHANGED")
    original, edited = memoryview(before), memoryview(after)
    stride = width * 4
    for row in range(len(before) // stride):
        start = row * stride
        end = start + stride
        if original[start:end] == edited[start:end]:
            continue
        cursor = start
        for left, right in sorted((start + x * 4, start + (x + w) * 4)
                                  for x, y, w, h in regions if y <= row < y + h):
            if cursor < left and original[cursor:left] != edited[cursor:left]:
                raise ValueError("E_BOUND_WEBP_OUTSIDE_TEXT_CHANGED")
            cursor = max(cursor, right)
        if original[cursor:end] != edited[cursor:end]:
            raise ValueError("E_BOUND_WEBP_OUTSIDE_TEXT_CHANGED")


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
    return _png(data, dimensions)


def _png(data: bytearray, dimensions: tuple[int, int]) -> bytes:
    width, height = dimensions

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
    from game_l10n.image_localization import _validate_inventory

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--relay", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--prepare-only", action="store_true")
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
    if output.resolve() != output or not output.is_relative_to(work):
        raise ValueError("E_BOUND_WEBP_OUTPUT_INVALID")
    output.mkdir(parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
    encoded = 0
    _, inventory, _, sources = _validate_inventory(work, inventory_path, [Path(item) for item in task["source_roots"]])
    assets = inventory["assets"]
    for index, asset in enumerate(assets, 1):
        if asset["format"].casefold() not in {"webp", "image/webp"}:
            continue
        name = f"asset-{index:04d}"
        original = sources.roots[asset["source_root_index"]] / asset["relative_path"]
        with original.open("rb") as stream:
            original_body = stream.read(_MAX_FILE + 1)
        if len(original_body) > _MAX_FILE or hashlib.sha256(original_body).hexdigest() != asset["source_sha256"]:
            raise ValueError("E_BOUND_WEBP_SOURCE_CHANGED")
        dimensions = asset["width"], asset["height"]
        before = _read_webp(original_body, dimensions, env, output)
        if args.prepare_only:
            width, height = dimensions
            stride = width * 4
            scanlines = bytearray((stride + 1) * height)
            for row in range(height):
                start = row * (stride + 1) + 1
                scanlines[start:start + stride] = before[row * stride:(row + 1) * stride]
            preview = output / (name + ".source.png")
            with preview.open("xb") as stream:
                stream.write(_png(scanlines, dimensions))
            encoded += 1
            continue
        source, destination = output / (name + ".png"), output / (name + ".webp")
        if not source.is_file():
            continue
        if source.resolve() != source or destination.resolve() != destination or destination.exists():
            raise ValueError("E_BOUND_WEBP_OUTPUT_INVALID")
        with source.open("rb") as stream:
            png = stream.read(_MAX_FILE + 1)
        if len(png) > _MAX_FILE:
            raise ValueError("E_BOUND_WEBP_PNG_TOO_LARGE")
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
            _check_pixels(before, _read_webp(result, dimensions, env, stage), dimensions[0],
                          [region["bbox"] for region in asset["text_regions"]])
            packed.rename(destination)
        encoded += 1
    if not encoded:
        raise ValueError("E_BOUND_WEBP_NO_STAGED_PNG")
    print(json.dumps({"prepared_source_pngs": encoded} if args.prepare_only else
                     {"encoded_webp": encoded, "status": "pixel_checked_not_visually_reviewed"}))


if __name__ == "__main__":
    main()
