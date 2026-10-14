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

This is deliberately not a complete Unity text-coverage adapter. Managed literals, non-INI assets, runtime-generated text, UI layout analysis, and an independent second inventory remain outside the prototype. Callers must keep static coverage incomplete until those paths are independently accounted for.
