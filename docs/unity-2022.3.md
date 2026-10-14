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

This profile does not inventory managed literals, non-INI `TextAsset` data, other serialized string fields, runtime-generated text, or UI layout behavior. Its extraction summary therefore declares `static_complete=false`. A localization task still needs a separately implemented complete ordered inventory and must reconcile both inventories before context analysis or translation.

The profile rejects IL2CPP players and Unity versions outside the 2022.3 series. Use another compatible SDK branch or create a separately validated profile rather than weakening those checks.

## Private workspace interface

Run `tools/unpack.py INPUT_GAME_ROOT OUTPUT_DIRECTORY`. The target locale defaults to `ko`; set `GAME_L10N_TARGET_LOCALE` when another target is required. Set `GAME_L10N_SOURCE_LOCALE` only when automatic source selection is ambiguous.

Run `tools/pack.py REQUEST_JSON OUTPUT_DIRECTORY` with `GAME_L10N_WORKDIR` bound to the private workspace and `GAME_L10N_STAGE=pack`. The schema-version-1 request supplies `game_root`, `segments_path`, `reviewed_path`, `source_bundle_relative`, `source_bundle_sha256`, and optionally `report_path`. All outputs must remain in the private workspace.
