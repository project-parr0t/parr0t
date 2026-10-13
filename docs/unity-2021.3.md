# Unity 2021.3 Mono and IL2CPP patch adapter

This branch supports reusable patch construction for Unity 2021.3 Mono and IL2CPP players that expose a managed mod-loader boundary. It does not claim that one binary can patch every Unity title.

## Reuse boundary

The adapter generalizes these engine-facing operations:

- discover a `*_Data/Managed` player layout and its exact Unity version;
- distinguish Mono from IL2CPP before attempting a managed build;
- validate the IL2CPP metadata header and report its format version;
- generate IL2CPP interop assemblies offline through a compatible BepInEx toolchain;
- compile a BepInEx IL2CPP bridge against read-only loader and .NET 6 references;
- detect Unity Localization, TextMeshPro, Addressables, and Harmony assemblies;
- compile a workspace-only loader bridge against read-only managed references;
- map reviewed targets to String Table entries, TextAsset keys, and exact literal keys;
- build and verify a deterministic sidecar ZIP without modifying the installed player;
- reject accidental redistribution of game and Unity runtime assemblies.

The following remain loader- or project-specific and must stay in the private agent workspace:

- the mod-loader entry class and lifecycle methods;
- types and methods used to reach dialogue, documents, menus, or dynamic UI;
- resource names, font locations, scene paths, and loader manifest fields;
- extracted source text, reviewed text, runtime payloads, compiled binaries, and patch archives.

BepInEx, MelonLoader, and native project loaders use different entry contracts. They may share this adapter's discovery, compiler, payload, package, and verification stages while providing a small private bridge for their own loader.

## Prerequisites

- A private agent workspace created by the framework.
- The Unity player root configured as a read-only `game_root`.
- A Unity 2021.3 Mono or IL2CPP build.
- A .NET SDK containing Roslyn.
- A loader-specific C# bridge in the workspace. The SDK never stores that bridge if it contains project-specific type or resource names.
- For IL2CPP, a BepInEx IL2CPP distribution and matching Unity base libraries extracted inside the private workspace.

## Discover

```powershell
game-l10n unity discover `
  --workspace <agent-workspace> `
  --game-root <configured-player-root>
```

The full path report stays under `reports/` in the private workspace. The command succeeds for a supported Unity 2021.3 Mono layout or a Unity 2021.3 IL2CPP layout with a valid metadata header. An IL2CPP report includes `metadata_version`; version 31 is accepted when the selected Cpp2IL/interop toolchain supports it.

## Prepare translation inventory

An extractor or private bridge must create normal framework segments with stable IDs and Unity location metadata. Each segment uses one of these metadata contracts:

```json
{
  "unity_kind": "string_table",
  "collection": "fixture_collection",
  "entry_id": "1001",
  "is_smart": true,
  "ui_path": "FixtureRoot/FixturePanel/FixtureLabel"
}
```

```json
{"unity_kind": "text_asset", "asset_key": "fixture_asset"}
```

```json
{"unity_kind": "literal", "source_key": "SOURCE_FIXTURE_LABEL"}
```

The location metadata is data-plane input. It remains in the workspace and is never promoted into the SDK branch.

Run the normal context-first translation and compact review pipeline, then compile only reviewed or source-preserved results:

```powershell
game-l10n unity compile-payload `
  --workspace <agent-workspace> `
  --segments inputs\fixture-segments.json `
  --reviewed outputs\fixture-reviewed.json `
  --locale qaa-FIXTURE `
  --output build\fixture-runtime.json
```

`--allow-unreviewed` exists for an explicitly accepted exceptional workflow. It is intentionally off by default.

## Build a private Mono loader bridge

```powershell
game-l10n unity build-managed `
  --workspace <agent-workspace> `
  --managed-root <configured-player-root>\Fixture_Data\Managed `
  --source bridge\FixtureBridge.cs `
  --reference Unity.Localization `
  --reference Unity.TextMeshPro `
  --reference 0Harmony `
  --output build\Fixture.Localization.dll
```

The compiler receives argument arrays directly, reads the installed managed assemblies as references, and verifies that every reference is byte-for-byte unchanged afterward. Loader-specific references are supplied with repeated `--reference` arguments.

## Generate IL2CPP interop assemblies

Keep the player files read-only and extract the compatible BepInEx IL2CPP distribution and Unity base libraries into the private workspace. Then run:

```powershell
game-l10n unity generate-interop `
  --workspace <agent-workspace> `
  --game-root <configured-player-root> `
  --loader-root dependencies\fixture-loader `
  --unity-base-libs dependencies\fixture-unity-libs `
  --output build\fixture-interop
```

The adapter reads the canonical IL2CPP metadata header, compiles a temporary offline runner, and performs Cpp2IL plus interop generation without launching the game. It snapshots the player inputs and toolchain references before execution and fails if any read-only input changes. The output directory must not already exist, so a failed or repeated run cannot silently merge incompatible assemblies.

The BepInEx distribution must contain the public Cpp2IL, LibCpp2IL, AsmResolver, and Il2CppInterop generator assemblies expected by the adapter. If the generator rejects the reported metadata version, replace the private workspace toolchain with a compatible release rather than modifying player metadata.

## Build a private IL2CPP loader bridge

```powershell
game-l10n unity build-il2cpp `
  --workspace <agent-workspace> `
  --loader-root dependencies\fixture-loader `
  --source bridge\FixtureBridge.cs `
  --output build\Fixture.Localization.dll
```

The build uses an explicit, bounded set of .NET 6 references from the pack beside the selected SDK and the BepInEx IL2CPP bridge references. Additional loader references can be supplied with repeated `--reference` arguments, and additional framework references with repeated `--framework-reference` arguments. As with the Mono path, compilation happens only in the private workspace and every reference is verified unchanged.

## Pack and verify

Create a workspace-only patch specification:

```json
{
  "schema_version": 1,
  "package": {"id": "fixture.localization", "version": "1.0.0"},
  "install": {"root": "Mods", "directory": "FixtureLocalization"},
  "files": [
    {"source": "build/Fixture.Localization.dll", "destination": "Fixture.Localization.dll", "role": "entry_assembly"},
    {"source": "bridge/fixture-mod.json", "destination": "fixture-mod.json", "role": "loader_manifest"},
    {"source": "build/fixture-runtime.json", "destination": "fixture-runtime.json", "role": "localization_payload"}
  ]
}
```

Then create and verify the sidecar archive:

```powershell
game-l10n unity pack `
  --workspace <agent-workspace> `
  --spec patch\fixture-spec.json `
  --output output\fixture-patch.zip

game-l10n unity verify `
  --workspace <agent-workspace> `
  --archive output\fixture-patch.zip
```

The archive is never installed automatically. Installation requires a separate user request after review. The ZIP and its verification manifest remain outside this SDK repository.

## What this adapter deliberately does not do

- It does not overwrite a player executable, managed assembly, asset bundle, save, or existing mod.
- It does not redistribute `Assembly-CSharp`, Unity assemblies, or framework assemblies from the installed player.
- It does not guess project-specific hooks from class names.
- It does not treat the presence of Unity Localization as proof that every visible string comes from a String Table.
- It does not install or redistribute BepInEx, Unity base libraries, or generated interop assemblies as though they were owned by the SDK.
- It does not choose a BepInEx/Cpp2IL release for an unknown metadata version; compatibility must be pinned and verified in the private workspace.
