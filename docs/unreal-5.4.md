# Unreal Engine 5.4 LocRes and Pak profile

This branch contains a provenance-free profile for the verified Unreal Engine 5.4 Windows layout in which a readable Pak companion contains LocRes/LocMeta localization resources alongside an IoStore container.

## Supported contract

The profile supports a build only when all of these conditions hold:

- the shipping executable reports the public `++UE5+Release-5.4` compatibility line;
- one project directory contains `Content/Paks` with a non-global `.utoc` and one readable `.pak`, or the private workspace selects each ambiguous container explicitly;
- the configured source locale resolves to exactly one non-Engine LocRes resource in the unpacked Pak;
- a matching LocMeta resource exists;
- the private workspace provides compatible Retoc and Repak executables;
- packing receives a complete result set whose entries are `reviewed` or exact `source_preserved` values.

Extraction verifies the selected IoStore table, unpacks the Pak companion, produces stable IDs from LocRes namespace/key pairs, and declares non-LocRes visible assets and runtime-generated text as excluded. The output is therefore not a complete source-inventory attestation by itself. A localization task must reconcile it with a separately implemented complete inventory before context analysis or translation.

Packing writes one configured target-culture LocRes and LocMeta, copies only explicitly declared workspace overlays, creates an additive `_P.pak` using Pak V11, and requires every unpacked output byte to match the staging tree. It does not install the Pak, edit Steam configuration, create a launcher, or start the game.

## Private workspace interface

Set `GAME_L10N_WORKDIR` to the external workspace. Set `GAME_L10N_RETOC` and `GAME_L10N_REPAK` to compatible external tools. When automatic discovery is ambiguous, set `GAME_L10N_UNREAL_UTOC`, `GAME_L10N_UNREAL_PAK`, or `GAME_L10N_UNREAL_LOCRES` to the exact selected input. External tool binaries and game-derived data remain outside this branch.

Run extraction with:

```text
python profiles/sdk-unreal-5.4/tools/unpack.py <read-only-game-root> <workspace-output>
```

Run packing with:

```text
python profiles/sdk-unreal-5.4/tools/pack.py <workspace-request.json> <workspace-output>
```

The pack request schema is `parr0t.unreal-5.4.pack/v1`. It names the extraction root, reviewed result file, project and localization target components, target locale, additive Pak filename, path-hash seed, compression mode, and any explicit overlay source/destination pairs. All sources and outputs must stay within the private workspace.

Player-facing delivery is a separate engine-neutral step. Feed the verified Pak payload to `game-l10n steam-package` rather than adding title-specific installer logic to this profile.

## Deliberate exclusions

- The profile does not claim that LocRes covers textures, widgets, Blueprint literals, subtitles stored outside LocRes, or runtime-generated strings.
- It does not decrypt encrypted containers or guess keys.
- It does not choose among multiple containers or source resources without an explicit private-workspace selection.
- It does not vendor Retoc or Repak executables.
- It does not install, update, remove, or launch a real game.
