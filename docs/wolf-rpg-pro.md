# WOLF RPG Pro localization adapter

This adapter implements a provenance-free WOLF RPG SDK workflow without distributing a game, extracted text, translation, encryption key, WolfDawn binary, or Pro packer.

## Why the extra integrity pass exists

WOLF projects often display strings that are also program data. A translated choice can be written to `S[]` or `CSelf[]` and later compared with the original literal. Translating only one side makes a valid-looking choice take the failure branch. Event names, database type/row/field names, and image or script paths have the same risk.

The adapter therefore uses two checks:

1. Before injection, `protect-runtime` decompiles the original event data and restores translated JSON leaves that are comparison keys or resource paths.
2. After injection, `audit-runtime` decompiles both trees and compares event calls, database selectors, branch-key writers, resource paths, and text-script command structure.

The original game directory is treated as read-only. All generated data stays in the external agent workspace.

## Workspace layout

```text
wolf-work/
  wolf-json/          WolfDawn extraction (private)
  translations/      mutable extraction copy (private)
  batches/            agent/manual batches (private)
  build/Data/         injected unpacked build (private)
  audit/wolf/         decompiled comparison reports (private)
  output/             reviewed replacement archives (private)
```

Initialize the workspace and record the user's Git choice as described in the main README. Add the original game folder with `--game-root`; the WOLF commands will accept source reads only from that folder or the workspace.

## Translation batches

```powershell
python -m game_l10n.wolf prepare-batches `
  --workspace <agent-workspace> `
  --source wolf-json `
  --copy-to translations\wolf-json `
  --output batches

game-l10n translate `
  --workspace <agent-workspace> `
  --input inputs\wolf-primary.json `
  --context-input inputs\wolf-primary.json `
  --output translations\reviewed.json
```

`prepare-batches` now produces `inputs/wolf-primary.json` from the semantic batch collector and `inputs/wolf-independent.json` through a separately implemented raw-JSON shape audit, then records the shared inventory attestation. The two paths must agree on stable ID, source, and order. An explicit `--exclude-json` entry is declared as user-visible and leaves static coverage incomplete instead of silently shrinking the denominator.

The context-first translation/review orchestrator remains authoritative. Batch v2 assigns a stable ID to each source occurrence instead of collapsing repeated text, so the same words may receive different translations in different scenes. The primary inventory preserves speaker, neighboring lines, extraction order, engine kind, and exact JSON references while the independent audit checks that the collector did not omit a supported source shape.

Import the deterministic delivery result into the batches, then apply it:

```powershell
python -m game_l10n.wolf import-reviewed `
  --workspace <agent-workspace> `
  --reviewed translations\reviewed.json `
  --batches batches

python -m game_l10n.wolf apply-batches `
  --workspace <agent-workspace> `
  --batches batches `
  --translated-json translations\wolf-json
```

The importer accepts `reviewed`, explicitly user-authorized `accepted_unreviewed`, and exact `source_preserved` rows. Missing translation results preserve their source instead of invalidating the whole delivery. A candidate with a control-code or newline mismatch also preserves the source for that target; unknown IDs, changed sources, and invalid statuses still fail before any batch file is written. The returned counts distinguish applied unreviewed targets, missing translations, and structural rejections. A verified inventory fingerprint is mandatory, and any unresolved `metadata.blocking_on_failure=true` target holds import, application, and packing until a later user-requested resume resolves it.

## Manual translator queue

Mark deferred batch items with `"manual": true`, then export the queue and a harmless same-format example:

```powershell
python -m game_l10n.wolf manual-export `
  --workspace <agent-workspace> `
  --batches batches `
  --output translations\manual_translation_queue.json `
  --sample-output translations\manual_translation_queue_sample.json
```

Merge a completed queue only after its IDs, sources, control codes, and newline counts validate:

```powershell
python -m game_l10n.wolf manual-merge `
  --workspace <agent-workspace> `
  --queue translations\manual_translation_queue.json `
  --batches batches
```

## Protect and audit runtime identifiers

```powershell
python -m game_l10n.wolf protect-runtime `
  --workspace <agent-workspace> `
  --wolfdawn <wolfdawn-executable> `
  --original-data <game-root>\Data `
  --translated-json translations\wolf-json

# Inject the protected JSON into build\Data with the reviewed WolfDawn workflow.

python -m game_l10n.wolf audit-runtime `
  --workspace <agent-workspace> `
  --wolfdawn <wolfdawn-executable> `
  --original-data <game-root>\Data `
  --localized-data build\Data
```

An audit failure is a build failure. Do not dismiss a reported key change merely because the affected string is visible in-game.

## Pack WOLF Pro archives

WOLF Pro refuses to mix archive encryption versions. The adapter invokes an external packer with the contract `pack <directory> <output.wolf> <Game.wolf>` and always produces the three replacement archives together:

```powershell
python -m game_l10n.wolf pack-pro `
  --workspace <agent-workspace> `
  --packer <pro-packer-executable> `
  --built-data build\Data `
  --game-archive <game-root>\Data\Game.wolf `
  --output output\replacement-archives
```

Review the output before installing it. Keep a recoverable copy of the original archives and never copy packer binaries or game-derived data into this SDK repository.

`pack-pro` rechecks the current inventory and blocking-failure gate before invoking the external packer. Runtime coverage remains a separate claim: packing and a clean static audit do not prove that every screen or state was observed in a real execution.

## Core update compatibility

Use the standalone engine module commands above. The framework CLI and root package remain identical to main. Import engine APIs from the engine subpackage.

The engine-owned public-tree regression gate rejects wolf-json, batches, and audit artifacts. Keep these directories external and exclude wolf-json from any workspace Git configuration. The core package version is not an engine SDK version.
