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
game-l10n wolf prepare-batches `
  --workspace <agent-workspace> `
  --source wolf-json `
  --copy-to translations\wolf-json `
  --output batches

game-l10n translate `
  --workspace <agent-workspace> `
  --input inputs\wolf-segments.json `
  --context-input inputs\full-ordered-script.json `
  --output translations\reviewed.json
```

The context-first translation/review orchestrator remains authoritative. `prepare-batches` is the engine mapping layer: it keeps stable IDs and exact JSON references so reviewed targets can be applied without fuzzy matching.

After filling each batch's `target` fields:

```powershell
game-l10n wolf apply-batches `
  --workspace <agent-workspace> `
  --batches batches `
  --translated-json translations\wolf-json
```

Control-code or newline mismatches abort before any translated JSON document is written.

## Manual translator queue

Mark deferred batch items with `"manual": true`, then export the queue and a harmless same-format example:

```powershell
game-l10n wolf manual-export `
  --workspace <agent-workspace> `
  --batches batches `
  --output translations\manual_translation_queue.json `
  --sample-output translations\manual_translation_queue_sample.json
```

Merge a completed queue only after its IDs, sources, control codes, and newline counts validate:

```powershell
game-l10n wolf manual-merge `
  --workspace <agent-workspace> `
  --queue translations\manual_translation_queue.json `
  --batches batches
```

## Protect and audit runtime identifiers

```powershell
game-l10n wolf protect-runtime `
  --workspace <agent-workspace> `
  --wolfdawn <wolfdawn-executable> `
  --original-data <game-root>\Data `
  --translated-json translations\wolf-json

# Inject the protected JSON into build\Data with the reviewed WolfDawn workflow.

game-l10n wolf audit-runtime `
  --workspace <agent-workspace> `
  --wolfdawn <wolfdawn-executable> `
  --original-data <game-root>\Data `
  --localized-data build\Data
```

An audit failure is a build failure. Do not dismiss a reported key change merely because the affected string is visible in-game.

## Pack WOLF Pro archives

WOLF Pro refuses to mix archive encryption versions. The adapter invokes an external packer with the contract `pack <directory> <output.wolf> <Game.wolf>` and always produces the three replacement archives together:

```powershell
game-l10n wolf pack-pro `
  --workspace <agent-workspace> `
  --packer <pro-packer-executable> `
  --built-data build\Data `
  --game-archive <game-root>\Data\Game.wolf `
  --output output\replacement-archives
```

Review the output before installing it. Keep a recoverable copy of the original archives and never copy packer binaries or game-derived data into this SDK repository.
