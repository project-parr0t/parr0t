# Unity 2022.3 Mono text SDK tools

This profile contains reusable public-format code and unmistakably synthetic preparation fixtures only. Game content, inventories, selectors, cached bytes, translations and execution evidence belong in an external workspace.

## Preparation and admission

Use Core with the extractor-preparation protocol, propagated through the official branch updater. Activate the selected exact SDK before implementation. A deficient SDK must complete a bound `source_probe --purpose extractor_preparation` task before either full inventory.

The worker runs `prepare.py --workspace <workspace> [--dependency-path <authorized-exact-cache>]`. It stages implemented tools, synthetic binaries, fixed extraction/round-trip expectations and `tools/extractor-preparation.json`. Then Core's `check-extractor-preparation --workspace <workspace> --relay <bound-relay>` executes/admit checks. Changed tools, fixtures or requirements invalidate readiness. This is not another full-game scan, a model call or an inventory/static/runtime coverage claim.

Dependencies are fixed in `profiles/unity-2022.3-requirements.txt`; use only separately authorized bytes. No dependency, font, image or audio asset is bundled/acquired by these commands. The primary serialized loader rejects UnityPy versions other than 1.25.4. Do not override a selected SDK's Core runtime with a different import path to evade activation checks.

## Extraction

With `GAME_L10N_WORKDIR` set, run `unpack.py INPUT_GAME_ROOT OUTPUT_DIRECTORY`. Set `GAME_L10N_INVENTORY_METHOD=primary` or `independent`. Each method independently parses immutable source bytes; outputs are `inventory.json`, source snapshot/manifest and normalized `evidence.sqlite3`. Warm scans reuse matching method/source/implementation bindings, not counterpart decisions.

`unity_managed` handles CLR metadata, literal/field/call relationships and embedded resources. `unity_objects` handles serialized v22 LE objects, typed/physical script bindings, PPtrs, JSON/INI payload entries and UnityFS v6–8. `unity_native` handles Windows UI resources and `app.info`; `unity_native_calls` handles bounded PE64 native UI references. Equal text at different physical positions is never collapsed. Shared method/type proofs use references instead of repeated large payloads.

Raw strings alone are neither display targets nor omissions. Structurally bound UI text is retained; unresolved actual display dependencies remain pending. Independent methods share public-format/low-level helpers, not source-derived inventory outputs. No image pixels, OCR, audio decoding or runtime observation occurs.

## Packing and existing libraries

With `GAME_L10N_WORKDIR` and `GAME_L10N_STAGE=pack`, run `pack.py REQUEST_JSON OUTPUT_DIRECTORY`. Inputs include `game_root`, `segments_path`, `reviewed_path`, `source_manifest_path`, and optional natively assembled `managed_types` for stripped objects. Core's eligible scope, exact source/result mapping, protected tokens and source hashes are enforced. Only new copied payload files and `pack-report.json` are written. Signatures are never stripped; unsafe layouts/projections reject rather than fake success.

`unity_textasset` retains bounded INI parsing/rewriting helpers and the older explicit locale-table library; it is not the default completeness route. `unity_serialized` retains opaque standalone object/font rebuilding. `unity_layout` supplies conservative geometry/glyph screening with `runtime_verified=False`; no font or license is bundled.

The seven preparation format checks and focused `tests.test_unity_managed`, `tests.test_unity_objects`, and `tests.test_unity_inventory` exercise actual synthetic bytes, selected replacements and duplicate preservation. Existing serialized/layout/INI library tests remain applicable. See `docs/unity-2022.3.md` for contracts and exact limits. Passing static/synthetic checks never means the supplied game was installed, launched or visually verified.

