# Parr0t agent operation guide

`AGENTS.md` defines invariants; load stage documents on demand.

## Supported control paths

Parr0t supports Codex App/CLI and OpenCodex-compatible proxies. Proxies change routing only.

## Start or resume

Resolve these locations before acting:

- repository: framework and selected SDK;
- game root: read-only input;
- workspace: all private and generated data.

Text translation is default; never consider OCR or image translation unless requested.

For a new job:

1. Confirm repository/workspace access and only that the source path exists; do not inspect its contents.
2. Initialize with that path and run status. For `establish_text_free_control_plane`, read its contracts and run `game-l10n enter-control-plane --workspace <workspace> --contracts-read --source-unopened`. If the parent already saw game content, record a leak and stop.
3. Ask once whether Git may be used for this game task; record only the answer. Disabled forbids Git mutation/publication in that workspace, not authenticated SDK access, download, inventory, or activation. Never infer the answer from transport or missing `.git`.
4. Follow only status `next_actions` and contracts. If `data_plane_task` exists, run its prepare, relay, and seal commands; never request an exception. Only `local_patch_ready` is terminal.

For an existing job, start with status and reuse valid checkpoints. Do not repeat work with unchanged fingerprints.

## State machine

```text
workspace and authorization
  -> SDK identity and selection
  -> exact SDK implementation activation
  -> two independent ordered inventories
  -> inventory reconciliation
  -> typed context analysis
  -> primary translation
  -> translation fallback for failures only
  -> complete independent compact review
  -> selective rewrite and confirmation
  -> review fallback for failures only
  -> deterministic validation
  -> recoverable packed patch or held checkpoint
  -> local_patch_ready
```

Do not reorder barriers. Failures affect only their target and route; they cannot cancel a batch, exclude content, or shrink the manifest. Blocking unresolved targets hold the patch; exhausted nonblocking targets preserve source visibly.

## Operating rules

- Select and activate before data-plane work. Bind exact matches to a Git checkout/worktree or authenticated portable snapshot. Portable activation records the full commit ID and verifies normalized content. Other identities are read-only `derive-new` references, never promotion targets. Pass-through tools are placeholders.
- Inventory completeness requires two independent ordered manifests. Alignment, review, and runtime observation are separate claims.
- At 10,000+ records, scan once per method. Continue cached gap-only reconciliation while the core gap score falls; stop only for a status-proven blocker, an applicable deterministic gate, or the configured unchanged-progress limit. A root call/token turn limit is applicable only when the user explicitly requested its value and the manifest records that approval. Never rescan or infer a limit from elapsed time, context pressure, or model judgment.
- Keep relevant narrative/voice, functional/UI, knowledge, adjacency, glossary, and immutable evidence target-scoped.
- The text-free parent handles only IDs, counts, states, queues, paths, and codes. It dispatches only core relays and never inspects game-root contents; a missing SDK changes nothing.
- Presets fix provider roles. Keys do not change mode, role, or service. Parent sees only `fallback_required`, never rationale.
- Translation is faithful transformation, not content generation. Independent review checks meaning, omission, voice, tone, function, conditions, units, terminology, allusions, placeholders, markup, control codes, ordering, and material layout risk.
- Build new outputs in the workspace. Automated checks use synthetic targets and never install or launch the real game.
- Apply proportional verification and stop after the changed layer passes. Keep small or tightly coupled work in the current agent; parallelize only independent bulk shards or genuinely independent research.
- Native passes are root-to-relay. Dispatch only the fresh relay path from `native-pass-step`. Review ceiling: 2,048; initial tasks: 256 records/65,536 tokens. Core alone emits failed-only child manifests.

## Status-driven routes

Use the routing table in repository `AGENTS.md`. Common short paths are:

- full job: `sdk-selection.md` → `coverage-gates.md` → `review-pipeline.md`;
- native worker/control-plane work: `control-plane-contract.md`, `execution-budget.md`, and `detail-contract.md`;
- manual/API fallback exchange: `translation-exchange.md`;
- exact user-approved wording correction: `minimal-corrections.md` without model or context calls;
- dialogue/reference flag without a stable ID: use `minimal-corrections.md` and its local correction UI once; accept its frozen export and never repeat a global inventory, text, or vector search;
- prior release, game update, or selective continuation: `release-checkpoint.md`;
- composed UI slots: `segment-metadata.md`;
- real runtime action: `runtime-approval.md` only after the matching explicit request;
- Windows delivery: `installer-contract.md` plus the selected Steam or filesystem packaging contract;
- publication: `instance-release.md` only after the user commands publication;
- SDK branch maintenance/update: `branch-update-contract.md`;
- reusable SDK promotion: SDK branch and engine FAQ contracts only after confirmed patch success.

After context, status routes translation/review. Missing, malformed, or stale attestations are never `ready`; gaps route to targeted repair and static completion to validation/packing. Attestations bind the result; pack state binds its input, attestation, and output before `local_patch_ready`. Invalid state returns one metadata-only repair action; stale queues route reconciliation.

For non-Steam/launcher-neutral delivery, `filesystem-package` is authoritative. `-GameRoot` accepts an absolute path or a path relative to the startup current directory. If omitted, the installer tries that validated startup current directory before prompting. It never discovers or controls a store client.

## Stable entry commands

```text
python -m pip install -e .
game-l10n init-workspace <external-workspace> --sdk-root <repository> --game-root <source-root>
game-l10n status --workspace <external-workspace>
game-l10n policy --workspace <external-workspace> --git-choice <enabled|disabled> --user-confirmed-git-choice
game-l10n enter-control-plane --workspace <external-workspace> --contracts-read --source-unopened
game-l10n sdk-inventory --workspace <external-workspace> --refresh-remote
game-l10n sdk-inventory --workspace <external-workspace> --runtime-root <extracted-sdk-snapshot> --portable-branch sdk-<engine>-<version-line> --portable-commit <full-authenticated-commit-id>
game-l10n sdk-evaluate --workspace <external-workspace> --target-engine <engine> --target-version <version-line> --branch <sdk-branch> --result <compatible|partial|reference|incompatible> --reason <reason-code> --evidence-path <public-contract-path>
game-l10n sdk-select --workspace <external-workspace> --target-engine <engine> --target-version <version-line> --mode <reuse|extend|derive-new|private-new> [--branch <sdk-branch>]
game-l10n sdk-activate --workspace <external-workspace> [--runtime-root <selected-sdk-runtime>] [--reference-checkout <reference-runtime>]
game-l10n prepare-data-plane-task --workspace <external-workspace> --worker-role <source_probe|primary_inventory|independent_inventory|inventory_reconciler|context_analyzer>
game-l10n seal-data-plane-task --workspace <external-workspace> --relay <relay-path>
game-l10n native-pass-step --workspace <external-workspace> --manifest <private-manifest> --results-dir <private-wave-results>
```

These are agent boundaries. A non-success data-plane receipt remains executable work: run status again and dispatch its reported retry or SDK-identity repair. Never verify a failed worker's placeholder artifact merely because the file exists. Portable inventory requires an authenticated connector or immutable commit archive; never invent branch or commit. Configure OpenCodex proxies separately and keep their credentials outside this repository.

## Completion

The final control-plane report contains only categorical coverage states, counts, deterministic issue codes, unresolved queue sizes, artifact paths, and the next required decision. Do not claim runtime coverage without separately authorized or user-executed observation.

For repository changes, run:

```text
python scripts/check_repo_privacy.py
python -m unittest discover -s tests -t . -v
```

Keep a sanitized SDK profile local after promotion. External effects remain dormant until explicitly requested.
