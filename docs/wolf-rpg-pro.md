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

`prepare-batches` produces `inputs/wolf-primary.json` from the semantic batch collector and `inputs/wolf-independent.json` through a separately implemented raw-JSON traversal, then records the shared inventory attestation. Both traversals use the public normalized role specification in `game_l10n.wolf.scope`, not a claim to support every upstream WolfDawn schema. They must agree on stable ID, source, and order. An explicit `--exclude-json` entry leaves static coverage incomplete instead of silently shrinking the denominator.

### Display eligibility and unsupported formats

The supported SDK-owned normalized contracts are:

- `names.json` / `names[]`: `safety: safe` names remain possible display dependencies. Safety alone does not prove a particular screen renders a name.
- Structured `scenes[].lines[]`: event lines remain possible display dependencies because this extraction does not retain the originating WScript opcode. Other nested `source`/`text` objects are not eligible just because they contain source-language characters, a speaker, or a familiar key.
- `text/**/__strings.json` / `files[].lines[]`: plain dialogue and the payloads of `@putselect`, `@select`, `@toki-teach`, `@placename`, `@subtitle`, `@jimaku`, `@mono_txt`, and the `,msg,` field of `@sumo_actionlist` have supported display roles. Command prefixes/suffixes are not translated. These same payload rules apply to normalized scene lines, including player-visible system/error messages.
- Empty lines and script comments (`#`, `::`) are control-only; `@mes` is speaker context, not an independent translation target. Unknown commands are unresolved, not proven non-display. Directory/file names such as `debug`, `engine`, or `module` never establish visibility.

Source-language selection runs only after role classification. Context neighbors use eligible source-language display payloads, never unknown commands, comments, or non-target language noise. The primary collector and independent audit each retain an ordered raw census with physical references, source digests, role categories and classifications. Inventories declare `coverage.display_scope` and source-bound `metadata.display_scope_evidence`; fingerprints bind the retained structural evidence, not a runtime visibility claim. Unknown shapes and unsafe names stay raw/unclassified, outside translation/context/review, and emit `WOLF_UNCLASSIFIED_FORMAT_ROLE` with counts while holding static completeness. They are not silently dropped or fabricated as internal-only.

Pending shapes require the existing isolated SDK/extractor-preparation workflow to add an evidence-backed format/consumer rule and independent regression, then regenerate and reconcile inventories. No per-game human whitelist or per-row model classification is required. For an already attested job, preserve canonical occurrence IDs, source and history using Core's `coverage.scope_correction` contract; this fresh-extraction role repair does not rewrite existing inventories or erase historical targets.

Batch v2 assigns a stable ID to each source occurrence instead of collapsing repeated text. The primary inventory preserves speaker, eligible neighboring lines, extraction order, engine kind, and exact JSON references while the independent audit checks supported roles separately. Applying batches mutates only admitted target fields; unsupported files remain byte-for-byte unchanged, and unsupported fields in changed JSON documents retain their original values (JSON formatting may change).

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

The importer accepts `reviewed`, explicitly user-authorized `accepted_unreviewed`, and exact `source_preserved` rows. Missing translation results preserve their source instead of invalidating the whole delivery. A candidate with a control-code or newline mismatch also preserves the source for that target; unknown IDs, changed sources, and invalid statuses still fail before any batch file is written. The returned counts distinguish applied unreviewed targets, missing translations, and structural rejections. A verified inventory fingerprint is mandatory.

An unresolved `metadata.blocking_on_failure=true` target normally holds import, application, and packing until a later user-requested resume resolves it. Explicit user consent via `failure_queue.allow_incomplete_artifact=true` can instead authorize the exact safe incomplete result bound to the current Core completion attestation. Import checks the actual `--reviewed` path; application and packing revalidate that existing bound artifact without inventing a new result path. Consent alone does not bypass current inventory, full-scope alignment, target-language policy, artifact path/hash/size, or integrity checks. An alternate result file (even with identical bytes), stale artifact, or unapproved completion remains blocked. This authorization does not clear the failure queue or turn unresolved targets into completed translations, and it does not grant permission to execute external tools.

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

`pack-pro` rechecks the current inventory and blocking-failure gate (including any artifact-bound incomplete authorization) before invoking the external packer. Runtime coverage remains a separate claim: packing and a clean static audit do not prove that every screen or state was observed in a real execution.

## Core update compatibility

Use the standalone engine module commands above. The framework CLI and root package remain identical to main. Import engine APIs from the engine subpackage.

The engine-owned public-tree regression gate rejects wolf-json, batches, and audit artifacts. Keep these directories external and exclude wolf-json from any workspace Git configuration. The core package version is not an engine SDK version.
