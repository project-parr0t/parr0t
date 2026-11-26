"""Create private inspection sheets from the exact assets in a Windows image relay."""
from __future__ import annotations

import argparse
import base64
import json
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import tempfile

# Use the installed Windows WIC codecs via WPF. Never pass game paths as code.
_RENDER = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName PresentationCore, WindowsBase
$inputData = ConvertFrom-Json ([Console]::In.ReadToEnd())
$hash = [Security.Cryptography.SHA256]::Create()
$rows = [Collections.Generic.List[object]]::new()
$sheets = [Collections.Generic.List[string]]::new()
$assets = @($inputData.assets)
try {
  for ($first = 0; $first -lt $assets.Count; $first += 9) {
    $visual = [Windows.Media.DrawingVisual]::new()
    $context = $visual.RenderOpen()
    $name = ('sheet-{0:d4}.png' -f (1 + [int]($first / 9)))
    $brush = [Windows.Media.Brushes]
    $context.DrawRectangle($brush::DimGray, $null, [Windows.Rect]::new(0,0,1170,840))
    try {
      for ($slot = 0; $slot -lt 9 -and $first + $slot -lt $assets.Count; $slot++) {
        $asset = $assets[$first + $slot]
        $col = $slot % 3
        $line = [math]::Floor($slot / 3)
        $x = 390 * $col; $y = 280 * $line
        $row = [ordered]@{ordinal=$asset.ordinal; status='decode_failed'}
        $stream = $null
        try {
          $bytes = [IO.File]::ReadAllBytes($asset.path)
          if (([BitConverter]::ToString($hash.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() -ne $asset.sha256) {
            $row.status = 'changed'
          } else {
            $stream = [IO.MemoryStream]::new($bytes, $false)
            $decoder = [Windows.Media.Imaging.BitmapDecoder]::Create($stream, 'PreservePixelFormat', 'OnLoad')
            $frame = $decoder.Frames[0]
            $w = $frame.PixelWidth; $h = $frame.PixelHeight
            $scale = [math]::Min(380.0 / $w, 250.0 / $h)
            $pw = $w * $scale; $ph = $h * $scale
            $context.DrawImage($frame, [Windows.Rect]::new($x + (390-$pw)/2, $y + (260-$ph)/2, $pw, $ph))
            $row = [ordered]@{ordinal=$asset.ordinal; status='previewed'; sheet=$name; width=$w; height=$h; frames=$decoder.Frames.Count}
            # Some WIC codecs expose only the first frame even for animated WebP.
            if ($bytes.Length -ge 21 -and [Text.Encoding]::ASCII.GetString($bytes, 8, 8) -eq 'WEBPVP8X' -and ($bytes[20] -band 2)) {
              $row.status = 'animated_first_frame_only'
            }
          }
        } catch [IO.IOException] {
        } catch [NotSupportedException] {
        } catch [FormatException] {
        } finally {
          if ($null -ne $stream) { $stream.Dispose() }
        }
        $label = [Windows.Media.FormattedText]::new([string]$asset.ordinal, [Globalization.CultureInfo]::InvariantCulture,
          [Windows.FlowDirection]::LeftToRight, [Windows.Media.Typeface]::new('Segoe UI'), 14, $brush::White, 1.0)
        $context.DrawText($label, [Windows.Point]::new($x + 8, $y + 260))
        $rows.Add($row)
      }
    } finally { $context.Close() }
    $target = [Windows.Media.Imaging.RenderTargetBitmap]::new(1170, 840, 96, 96, [Windows.Media.PixelFormats]::Pbgra32)
    $target.Render($visual)
    $encoder = [Windows.Media.Imaging.PngBitmapEncoder]::new()
    $encoder.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($target))
    $file = [IO.File]::Open([IO.Path]::Combine($inputData.output, $name), 'CreateNew')
    try { $encoder.Save($file) } finally { $file.Dispose() }
    $sheets.Add($name)
  }
} finally { $hash.Dispose() }
ConvertTo-Json -InputObject @{assets=@($rows.ToArray()); sheets=@($sheets.ToArray())} -Compress -Depth 4
"""


_ORIGINALS = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName PresentationCore, WindowsBase
$inputData = ConvertFrom-Json ([Console]::In.ReadToEnd())
$rows = [Collections.Generic.List[object]]::new()
$hash = [Security.Cryptography.SHA256]::Create()
try {
  foreach ($asset in $inputData.assets) {
    $row = [ordered]@{ordinal=$asset.ordinal; status='decode_failed'}
    $rows.Add($row)
    $stream = $null
    try {
      $source = [IO.FileInfo]::new($asset.path)
      if ($source.Length -gt 67108864) { $row.status = 'too_large'; continue }
      $bytes = [IO.File]::ReadAllBytes($asset.path)
      if (([BitConverter]::ToString($hash.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() -ne $asset.sha256) {
        $row.status = 'changed'; continue
      }
      $stream = [IO.MemoryStream]::new($bytes, $false)
      $decoder = [Windows.Media.Imaging.BitmapDecoder]::Create($stream, 'PreservePixelFormat', 'OnLoad')
      if ($decoder.Frames.Count -ne 1 -or ($bytes.Length -ge 21 -and
          [Text.Encoding]::ASCII.GetString($bytes, 8, 8) -eq 'WEBPVP8X' -and ($bytes[20] -band 2))) {
        $row.status = 'animated'; continue
      }
      $frame = $decoder.Frames[0]
      if ([long]$frame.PixelWidth * $frame.PixelHeight -gt 16777216) { $row.status = 'too_large'; continue }
      $name = ('ordinal-{0:d4}.png' -f [int]$asset.ordinal)
      $encoder = [Windows.Media.Imaging.PngBitmapEncoder]::new()
      $encoder.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($frame, $null, $null, $null))
      $file = [IO.File]::Open([IO.Path]::Combine($inputData.output, $name), 'CreateNew')
      try { $encoder.Save($file) } finally { $file.Dispose() }
      $row.status = 'original_scale'; $row.file = $name
      $row.width = $frame.PixelWidth; $row.height = $frame.PixelHeight
    } catch [IO.IOException] {
    } catch [NotSupportedException] {
    } catch [FormatException] {
    } finally {
      if ($null -ne $stream) { $stream.Dispose() }
    }
  }
} finally { $hash.Dispose() }
ConvertTo-Json -InputObject @{assets=@($rows.ToArray())} -Compress -Depth 4
"""


def _render(output: Path, assets: list[dict], *, original_scale: bool = False) -> dict:
    # The child runs only the fixed script and receives only its exact bound inputs.
    env = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'PATH')
           if key in os.environ}
    powershell = Path(env['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run(
        [str(powershell), '-NoProfile', '-NonInteractive', '-STA', '-EncodedCommand',
         base64.b64encode((_ORIGINALS if original_scale else _RENDER).encode('utf-16le')).decode('ascii')],
        input=json.dumps({'output': str(output), 'assets': assets}).encode('ascii'),
        capture_output=True, env=env, cwd=output, check=False,
    )
    if result.returncode:
        raise ValueError('E_WINDOWS_IMAGE_PREVIEW_RENDER_FAILED')
    return json.loads(result.stdout)


def _bind_preview_sheets(
    work_dir: Path, index: Path, sheets: list[int], bound_assets: list[dict],
) -> dict:
    from .store import stable_hash

    error = "E_WINDOWS_IMAGE_PREVIEW_BINDING_INVALID"
    base = work_dir / ".game-l10n/data-plane/windows-image-preview"
    if (
        index.resolve() != index or index.parent.parent != base or index.name != "index.json"
        or not sheets or any(type(n) is not int or n < 1 for n in sheets)
        or len(set(sheets)) != len(sheets)
    ):
        raise ValueError(error)
    body = index.read_bytes()
    report = json.loads(body)
    if (
        not isinstance(report, dict) or report.get("schema") != "parr0t.windows-image-inspection-preview.v1"
        or report.get("task_id") != index.parent.name or len(index.parent.name) != 64
        or report.get("asset_binding") != stable_hash(bound_assets)
        or not isinstance(report.get("sheets"), list) or not isinstance(report.get("assets"), list)
    ):
        raise ValueError(error)
    selected = {}
    for number in sheets:
        name = f"sheet-{number:04d}.png"
        path = index.with_name(name)
        if name not in report["sheets"] or path.resolve() != path:
            raise ValueError(error)
        image = path.read_bytes()
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(error)
        selected[name] = {"path": path.relative_to(work_dir).as_posix(),
                          "sha256": sha256(image).hexdigest(), "asset_ordinals": []}
    seen = set()
    for row in report["assets"]:
        if not isinstance(row, dict):
            raise ValueError(error)
        if row.get("sheet") not in selected or row.get("status") != "previewed":
            continue
        ordinal = row.get("ordinal")
        if type(ordinal) is not int or not 1 <= ordinal <= len(bound_assets) or ordinal in seen:
            raise ValueError(error)
        seen.add(ordinal)
        selected[row["sheet"]]["asset_ordinals"].append(ordinal)
    if not seen:
        raise ValueError(error)
    return {"index": index.relative_to(work_dir).as_posix(), "index_sha256": sha256(body).hexdigest(),
            "sheets": list(selected.values()), "asset_count": len(seen)}


def _select_preview_ordinals(binding: dict, ordinals: list[int] | None) -> dict:
    if ordinals is None:
        return binding
    error = "E_WINDOWS_IMAGE_PREVIEW_BINDING_INVALID"
    if not ordinals or any(type(n) is not int or n < 1 for n in ordinals):
        raise ValueError(error)
    pending = set(ordinals)
    if len(pending) != len(ordinals):
        raise ValueError(error)
    sheets = []
    for item in binding["sheets"]:
        selected = [n for n in item["asset_ordinals"] if n in pending]
        if selected:
            pending.difference_update(selected)
            sheets.append({**item, "asset_ordinals": selected})
    if pending:
        raise ValueError(error)
    return {**binding, "sheets": sheets, "asset_count": len(ordinals)}


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from game_l10n.data_plane_task import _load_bound_data_plane_relay
    from game_l10n.font_coverage import _font_probe_counts
    from game_l10n.store import stable_hash

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--relay', type=Path, required=True)
    parser.add_argument('--original-scale', action='store_true')
    args = parser.parse_args()
    work = args.workspace.resolve()
    relay, task = _load_bound_data_plane_relay(work, work / args.relay)
    script = Path(__file__).resolve()
    binding = {'argv': [str(script), *sys.argv[1:]], 'sha256': sha256(script.read_bytes()).hexdigest()}
    if (os.name != 'nt' or relay['worker_role'] != 'image_ui_inventory' or task.get('purpose')
            or not any(all(entry.get(key) == value for key, value in binding.items())
                       for entry in task.get('offline_processing', []))):
        raise ValueError('E_WINDOWS_IMAGE_PREVIEW_NOT_BOUND')
    if args.original_scale:
        selected = task.get('image_preview')
        if not task.get('image_cohort_observations') or not selected:
            raise ValueError('E_WINDOWS_IMAGE_PREVIEW_NOT_BOUND')
        ordinals = [n for sheet in selected['sheets'] for n in sheet['asset_ordinals']]
        if not 1 <= len(ordinals) <= 32 or len(ordinals) != len(set(ordinals)):
            raise ValueError('E_WINDOWS_IMAGE_PREVIEW_NOT_BOUND')
        assets = [{**task['bound_local_assets'][n - 1], 'ordinal': n} for n in ordinals]
        output = work / '.game-l10n/data-plane/windows-image-detail' / task['task_id']
        if output.resolve() != output or output.exists():
            raise ValueError('E_WINDOWS_IMAGE_PREVIEW_OUTPUT_NOT_NEW')
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output.parent) as directory:
            stage = Path(directory) / 'originals'
            stage.mkdir()
            index = _render(stage, assets, original_scale=True)
            (stage / 'index.json').write_text(json.dumps(index, sort_keys=True), encoding='utf-8')
            stage.rename(output)
        print(json.dumps({'original_scale_dir': output.relative_to(work).as_posix(),
                          'image_rows': len(index['assets'])}))
        return
    output = work / '.game-l10n/data-plane/windows-image-preview' / task['task_id']
    if output.resolve() != output or output.exists():
        raise ValueError('E_WINDOWS_IMAGE_PREVIEW_OUTPUT_NOT_NEW')
    output.parent.mkdir(parents=True, exist_ok=True)
    images, fonts = [], []
    for ordinal, asset in enumerate(task['bound_local_assets'], start=1):
        path = Path(asset['path'])
        if path.resolve() != path:
            raise ValueError('E_WINDOWS_IMAGE_PREVIEW_ASSET_CHANGED')
        if path.suffix.casefold() in {'.ttf', '.otf', '.ttc', '.otc', '.woff', '.woff2', '.fnt'}:
            row = {'ordinal': ordinal, 'status': 'font_unverified'}
            try:
                if sha256(path.read_bytes()).hexdigest() == asset['sha256']:
                    row['coverage'] = _font_probe_counts(path)
                    row['status'] = 'font_cmap_checked'
            except (OSError, ValueError):
                pass
            fonts.append(row)
        else:
            images.append({**asset, 'ordinal': ordinal})
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        stage = Path(directory) / 'preview'
        stage.mkdir()
        index = _render(stage, images)
        index.update({'schema': 'parr0t.windows-image-inspection-preview.v1', 'task_id': task['task_id'],
                      'asset_binding': stable_hash(task['bound_local_assets']), 'fonts': fonts})
        (stage / 'index.json').write_text(json.dumps(index, sort_keys=True), encoding='utf-8')
        stage.rename(output)
    print(json.dumps({'preview_dir': output.relative_to(work).as_posix(), 'image_rows': len(index['assets']),
                      'sheets': len(index['sheets']), 'font_rows': len(fonts)}))


if __name__ == '__main__':
    main()
