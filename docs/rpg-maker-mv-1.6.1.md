# RPG Maker MV 1.6.1 localization adapter

This branch adds a provenance-free data-plane adapter for RPG Maker MV 1.6.1. It inherits context analysis, native-pass merging, provider fallback, compact review, candidate generations, checkpoints, and budget accounting from `main`. The adapter owns only engine discovery, ordered extraction, deterministic application, output verification, and packaging.

The original game tree is read-only. Every baseline, translation result, rebuilt JSON file, report, and archive must remain in the external agent workspace.

## Safe stage order

Run the stages in this order and do not overlap their barriers:

1. Extract the full ordered script and immutable apply manifest.
2. Complete and attest full-context analysis.
3. Complete the native primary translation pass and merge exclusive shards in source order.
4. Route only unresolved targets through configured translation fallbacks and bounded grace retries.
5. Review the current candidate generation with compact verdicts; rewrite only flagged targets and confirm the replacement generation.
6. Build the patch from accepted results. Missing, unsuccessful, or deterministically invalid targets preserve the exact source by default.
7. Verify that every output parses, only manifest paths changed, and the source tree is unchanged.
8. Create a deterministic ZIP from the verified patch tree.

If an upstream candidate changes after review, discard that review and judge the new candidate generation. A provider refusal is a routing event, not a reason to omit a target.

## Workspace commands

Initialize an external workspace and register the source game as a read-only root using the core workflow. Then extract:

```powershell
python -m game_l10n.rpg_maker_mv extract `
  --workspace <agent-workspace> `
  --game-root <configured-game-root> `
  --output engine-extract
```

The output contains:

- `segments.jsonl`: ordered translation targets for the core pipeline;
- `context-script.json`: ordered event context for full-script analysis;
- `apply-manifest.json`: stable IDs, JSON paths, mirrors, spans, baseline hashes, and patch paths;
- `baseline/`: immutable source JSON copies used for precondition checks.

After the core translation and review pipeline produces its result file, build a separate patch tree:

```powershell
python -m game_l10n.rpg_maker_mv build `
  --workspace <agent-workspace> `
  --game-root <configured-game-root> `
  --manifest engine-extract\apply-manifest.json `
  --results reviewed-results.json `
  --output patch-tree `
  --report reports\build.json
```

By default, missing or unsuccessful results and translations with deterministic integrity failures keep their source text. Use `--require-all-results` or `--fail-on-invalid` only when an incomplete build must stop instead.

There is no delivery override for a candidate with mismatched control tokens or event-line structure. Such a candidate either preserves the source or, with `--fail-on-invalid`, stops the build. Review completion cannot override this engine-safety gate.

Verify and package:

```powershell
python -m game_l10n.rpg_maker_mv verify `
  --workspace <agent-workspace> `
  --game-root <configured-game-root> `
  --manifest engine-extract\apply-manifest.json `
  --patch patch-tree

python -m game_l10n.rpg_maker_mv pack `
  --workspace <agent-workspace> `
  --game-root <configured-game-root> `
  --manifest engine-extract\apply-manifest.json `
  --patch patch-tree `
  --output output\translation-patch.zip
```

The pack command repeats verification before writing the ZIP. The archive preserves paths relative to the patch root so it can be inspected before installation. Installation into the game directory is intentionally outside this adapter and requires a separate user request.

## Engine integrity rules

- Consecutive Show Text (`401`) and Scrolling Text (`405`) commands form one target. The translated newline count must exactly match the number of command paths.
- Show Choices (`102`) text and matching When Choice (`402`) labels are updated together. A source mismatch between them aborts extraction or application.
- A `D_TEXT` plugin command stores a visible payload inside a larger command string. Only the recorded payload span is replaced.
- Supported script calls store display text in quoted JavaScript literals. Only the literal span is replaced; the original single- or double-quote style and required escaping are preserved.
- Preconditions are always checked against the immutable baseline document, never against a document already modified by an earlier target.
- Repeated assignments to one JSON path are allowed only when they resolve to the same value. Conflicting assignments fail before any output is published.
- Verification reparses every JSON output, compares its structure with the baseline, rejects changes outside manifest-owned paths, and rechecks source hashes.

All committed tests use synthetic identifiers and text. Do not add extracted files, manifests, reports, hashes, target counts, real paths, or runtime observations from a localization job to this branch.

## Core update compatibility

Use the standalone engine module commands above. The framework CLI and root package remain identical to main. Import engine APIs from the engine subpackage.
