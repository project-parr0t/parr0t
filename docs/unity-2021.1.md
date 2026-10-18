# Unity 2021.1 offline serialization SDK

This branch derives from the engine-neutral framework. It provides the bounded
Mono serialization support exercised on the Unity 2021.1 compatibility line.
It does not claim compatibility with other engine lines, IL2CPP, arbitrary
managed-reference registry versions, or runtime locale injection.

Install the exact build dependencies from `requirements-unity.txt` into a
separate Python environment. They are build tools; a player's installation does
not require Python. No game assemblies or assets are distributed by this SDK.

The public Python interfaces are:

- `game_l10n.unity.schema.ManagedSchemas`: parse read-only assembly metadata,
  normalize generated collection schemas, and use the release native prefix.
  Supply an explicit list of allowed source roots and call `verify_unchanged`
  after the offline operation. Assembly metadata is parsed without loading game
  assemblies into a managed execution context.
- `game_l10n.unity.serialization.read_complete_typetree`: read complete typed
  objects, including indexed version-one managed references through the terminal
  frame. Embedded reference schemas take priority over generated metadata.
- `rewrite_fields`: guard every source string and field path, require a lossless
  initial serialization round trip, rebuild, and compare the complete typed
  result. It returns bytes and never saves a source object.
- `rewrite_registry_fields`: handle the version-one registry writer limitation
  by testing aligned string substitutions against the complete expected typed
  object. A nonunique or unverifiable edit fails closed.
- `game_l10n.unity.catalog.patch_catalog`: recompute the UnityFS block-data CRC,
  size, and content-derived cache identity for one exact bundle entry. Catalog
  keys, paths, labels, dependencies, providers, and unrelated option records
  remain unchanged. CRC checks remain enabled.

Title-specific discovery, stable target IDs, checkpoint binding, ownership,
original-file digests, and output manifests remain outside this branch. Callers
must validate source bindings, write only new output artifacts, reread serialized
containers, and compare untouched object bytes. The SDK provides no installation
or process-launch path and does not create core verified-pack state by itself.

Run the engine and core suites with `python -m unittest discover -s tests`, then
run the repository privacy checker, engine exception checker, and
`game-l10n verify-sdk-promotion` against the promotion commit. All engine fixtures
are synthetic and generate their data in memory.

The deterministic schema, registry, source-guard, and catalog-integrity conditions
are classified as automated fixes with regression checks. Conditions that depend
on a particular title remain outside the SDK; unestablished runtime causes are
not published as engine guidance. The engine exception runbook is intentionally
empty.
