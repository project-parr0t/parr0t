# Unreal Engine 5.4 profile tools

This profile implements the verified reusable subset for an Unreal Engine 5.4 Windows build that uses an IoStore container plus a readable Pak companion containing LocRes/LocMeta localization data.

`unpack.py` validates the engine version and IoStore container, extracts the companion Pak, selects one source-locale LocRes, and writes an ordered LocRes segment set. It deliberately declares static coverage incomplete because LocRes alone cannot prove coverage of textures, widgets, Blueprint literals, or runtime-generated text. A localization job must still supply and reconcile a separately implemented complete inventory before translation.

`pack.py` accepts `parr0t.unreal-5.4.pack/v1`, applies only complete `reviewed` or exact `source_preserved` results, updates LocMeta culture metadata, copies explicitly declared workspace overlays, builds an additive Pak V11 container, and requires a byte-identical unpack round trip.

The tools require:

- `GAME_L10N_WORKDIR` pointing at the private external workspace;
- `GAME_L10N_RETOC` pointing at a compatible Retoc executable for extraction;
- `GAME_L10N_REPAK` pointing at a compatible Repak executable for extraction and packing;
- optional explicit `GAME_L10N_UNREAL_UTOC`, `GAME_L10N_UNREAL_PAK`, or `GAME_L10N_UNREAL_LOCRES` selections when discovery is ambiguous;
- optional `GAME_L10N_SOURCE_LOCALE` and `GAME_L10N_TARGET_LOCALE` during extraction.

External binaries and every game-derived file stay outside the SDK branch. Packaging for players is handled by the engine-neutral `game-l10n steam-package` command after this profile has produced and verified a payload.
