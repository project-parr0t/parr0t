# Unity 2022.3 Mono TextAsset prototype

This sanitized profile contains only the direct UnityFS workflow validated for a Unity 2022.3 Mono player:

- discover one `*_Data` player layout and require the `2022.3` series;
- identify a single language bundle containing ordered INI `TextAsset` locale tables;
- emit a source/target inventory while treating the player root as read-only;
- rebuild a copy with exactly one target locale `TextAsset` changed;
- verify IDs, order, placeholders, object structure, other object bytes, other locale bytes, and source immutability.

The implementation was validated with `UnityPy==1.25.4`; install that exact dependency in the private workspace. The profile does not bundle third-party binaries.

`unpack.py` accepts `INPUT_GAME_ROOT OUTPUT_DIRECTORY`. The target locale defaults to `ko` and may be set with `GAME_L10N_TARGET_LOCALE`. When automatic source-locale selection is ambiguous, set `GAME_L10N_SOURCE_LOCALE` explicitly.

`pack.py` accepts `REQUEST_JSON OUTPUT_DIRECTORY`. The request uses schema version 1 and supplies `game_root`, `segments_path`, `reviewed_path`, `source_bundle_relative`, `source_bundle_sha256`, and optionally `report_path`. `GAME_L10N_WORKDIR` must identify the private workspace and `GAME_L10N_STAGE` must be `pack`.

This is deliberately not a complete Unity text-coverage adapter. Managed literals, non-INI assets, runtime-generated text, UI discovery, and an independent second inventory remain outside the prototype. Callers must keep static coverage incomplete until those paths are independently accounted for.

Two separate libraries extend the same Unity 2022.3 identity:

- `unity_serialized.py`: `pack_string`, `string_at` and `unique_boundary` provide explicitly bound string helpers. `rebuild_serialized(raw_bytes, changed_objects)` supports opaque replacement in standalone v22 little-endian serialized files only, preserving unowned bytes and protected metadata. `replace_font_payload(raw_bytes, object_id, font_bytes, family_names)` replaces an embedded Font payload and family names with typetree verification. These functions do not discover targets or handle UnityFS containers.
- `unity_layout.py`: `Rect` and `resolve_rect_size(rectangles, node_id, canvas_size)` resolve anchor-based child dimensions, with roots representing the caller's canvas reference. `FontMetrics` takes codepoint advances and line-height units; `TextLayout` supplies wrapping and best-fit bounds. `screen_text(text, rect_size, metrics, layout, strip_rich_text=False)` returns conservative measurements, overflow and missing codepoints. No bundled fonts or mandatory font parser are required. Its `runtime_verified` is always false.

See the SDK's `docs/unity-2022.3.md` for usable interfaces, a synthetic example and exact limits. Private inventories, target selection, patch orchestration, font sourcing and runtime observations remain caller responsibilities. Run `python selftest.py` in this tools directory for dependency-free smoke checks. Metric screening cannot prove Unity rendering, runtime font ingestion or screen coverage.
