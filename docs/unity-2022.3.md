# Unity 2022.3 Mono TextAsset prototype

This branch records the bounded direct-bundle workflow validated for Unity 2022.3 Mono players. It is a reusable prototype, not a claim that every Unity 2022.3 title uses the same localization layout.

## Supported contract

The profile under `profiles/sdk-unity-2022.3` supports a player only when all of these conditions hold:

- exactly one `*_Data` directory identifies a Unity `2022.3.x` Mono player;
- one UnityFS bundle contains at least two locale-named `TextAsset` objects;
- the source and target assets are UTF-8 INI tables with identical section/key order;
- a non-locale reference table uniquely identifies the source locale, or `GAME_L10N_SOURCE_LOCALE` is supplied;
- `UnityPy==1.25.4` is installed in the private workspace.

The extractor writes an ordered source/target inventory outside the player root. The packer accepts reviewed or exact source-preserved results, rewrites only the configured target locale asset in a copied bundle, and verifies:

- exact ID coverage and contiguous order;
- source/result alignment and placeholder/control-token preservation;
- unchanged Unity object structure;
- byte-identical non-target objects and locale assets;
- unchanged source-tree hashes before and after processing.

Neither command installs the payload or launches the player.

## Deliberate exclusions

This profile does not inventory managed literals, non-INI `TextAsset` data, other serialized string fields, runtime-generated text, or UI layout behavior. Its extraction summary therefore declares `static_complete=false`. The separate library helpers below do not expand the extractor's discovery coverage. A localization task still needs a separately implemented complete ordered inventory and must reconcile both inventories before context analysis or translation.

The profile rejects IL2CPP players and Unity versions outside the 2022.3 series. Use another compatible SDK branch or create a separately validated profile rather than weakening those checks.

## Private workspace interface

Run `tools/unpack.py INPUT_GAME_ROOT OUTPUT_DIRECTORY`. The target locale defaults to `ko`; set `GAME_L10N_TARGET_LOCALE` when another target is required. Set `GAME_L10N_SOURCE_LOCALE` only when automatic source selection is ambiguous.

Run `tools/pack.py REQUEST_JSON OUTPUT_DIRECTORY` with `GAME_L10N_WORKDIR` bound to the private workspace and `GAME_L10N_STAGE=pack`. The schema-version-1 request supplies `game_root`, `segments_path`, `reviewed_path`, `source_bundle_relative`, `source_bundle_sha256`, and optionally `report_path`. All outputs must remain in the private workspace.

## Bounded standalone library

Import `unity_serialized` from the profile's `tools` directory. It requires the same private `UnityPy==1.25.4` dependency. `environment(raw_bytes)` accepts only standalone serialized files with version 22 and little-endian metadata; it rejects UnityFS containers and other layouts. This is a Unity 2022.3 capability bound, not support for every engine version that happens to use v22.

- `pack_string(text)` encodes a UTF-8 length-prefixed, four-byte-padded string. `string_at(payload, offset)` parses one explicitly bound string and returns `(text, padded_end)`; malformed boundaries and invalid UTF-8 are rejected.
- `unique_boundary(payload, encoded_bytes)` returns one exact `(start, end)` match and rejects missing, empty or ambiguous bindings. The caller must establish which field the bytes represent.
- `rebuild_serialized(raw_bytes, {object_id: replacement_bytes})` relocates opaque payloads while retaining the original metadata, gaps and unowned object bytes. Only table offsets, sizes and the file-size header change. It verifies object structure, replacement bytes, unowned payloads and protected metadata on round trip. Invalid regions and unknown IDs are rejected. It neither discovers translation targets nor serializes unrelated objects.
- `replace_font_payload(raw_bytes, object_id, font_bytes, family_names)` changes only an explicitly selected embedded `Font` object's `m_FontData` and `m_FontNames`, verifies its typetree round trip and uses the same opaque rebuild. The caller supplies legally usable font bytes and names; no font or license is bundled. A successful round trip does not prove runtime ingestion or glyph rendering.

Keep target maps, selectors, paths, source/target text and all build orchestration in the private workspace. Callers write returned bytes only to new workspace artifacts and integrate the framework's completion and verified-pack gates themselves. The helpers do not install or launch anything.

## Pure UI screening library

`unity_layout` has no UnityPy, font-file or other external dependency. Supply public-shaped values from a private inventory; the helper does not inspect assets. `Rect(parent, anchor_min, anchor_max, size_delta)` represents a node; a node with `parent=None` represents the canvas reference rectangle. `resolve_rect_size(rectangles, node_id, canvas_size)` resolves child anchor spans plus size deltas through the full parent chain. Missing parents, cycles, nonfinite values and non-positive dimensions are rejected. Rotation, scaling, pivots, driven layout groups and runtime transforms are outside this calculation.

`FontMetrics(units_per_em, line_height, advances, fallback_advance)` receives a codepoint-to-advance mapping and conservative line-height units chosen by the caller. `TextLayout(font_size, wrap=True, truncate_vertical=True, best_fit=False, best_fit_min=1, best_fit_max=1)` defines the screening bounds. `screen_text(text, rect_size, metrics, layout, strip_rich_text=False)` returns the tested size, line measurements, conservative overflow state, missing codepoints and `runtime_verified=False`. Best fit selects the largest fitting integer size; if no size fits, it reports the minimum size and overflow. Optional tag stripping is approximate. A minimal synthetic use is:

```python
from unity_layout import Rect, FontMetrics, TextLayout, resolve_rect_size, screen_text

rectangles = {"canvas": Rect(), "label": Rect("canvas", size_delta=(-20, -20))}
size = resolve_rect_size(rectangles, "label", (200, 100))
metrics = FontMetrics(1000, 1200, {ord(ch): 500 for ch in "SOURCE"}, 1000)
result = screen_text("SOURCE", size, metrics, TextLayout(16))
```

The estimate uses codepoint advances and conservative character wrapping. It does not model Unity word wrapping, shaping, kerning, full rich-text semantics, runtime font fallbacks, dynamic text or nonreference screen sizes. Missing glyphs and overflow are screening findings, not authorization to change authored typography. Actual rendering and runtime coverage require separately authorized user-run observations under `docs/engine-faq.md`.

Run the public profile's `tools/selftest.py` for dependency-free interface checks. The focused synthetic suite is `python -m unittest tests.test_unity_serialized tests.test_unity_layout tests.test_unity_2022_3_profile -v` from the SDK checkout.
