# Parr0t agent operation guide

This is the short operating entry point for coding agents. `AGENTS.md` defines invariants; dedicated documents define stage details. Do not load unrelated contracts in advance.

## Supported control paths

Parr0t supports Codex App/CLI directly and Codex routed through an OpenCodex-compatible proxy. A proxy changes provider routing only. It does not relax repository instructions, workspace isolation, control-plane separation, provider locks, or authorization boundaries.

## Start or resume

Resolve these locations before acting:

- Parr0t repository: reusable framework and selected SDK branch;
- source game root: read-only input;
- external workspace: all private extraction, context, translation, cache, report, build, and patch data.

Text translation is the default. Do not inspect images for text or consider OCR/image translation unless the user requested it.

For a new job:

1. Confirm repository/workspace access and only that the source path exists; do not inspect its contents.
2. Initialize with that path and run status. For `establish_text_free_control_plane`, read its contracts and run `game-l10n enter-control-plane --workspace <workspace> --contracts-read --source-unopened`. If the parent already saw game content, record a leak and stop.
3. Ask once whether Git may be used for this game task and record the choice. Disabled means no `git init`, commit, branch, remote, or push for that job.
4. Follow the single later `next_actions`. Resolve only its `required_contracts`; load another contract only for a new user route. Isolated workers perform locale/engine discovery, inventory, and classification, update private state, and return validated envelope paths. Only `local_patch_ready` is terminal.

For an existing job, start with status and reuse valid checkpoints. Do not repeat extraction, context work, translation, or validation whose input fingerprint is unchanged.

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

Do not skip or reorder barriers. One refusal, timeout, malformed result, capacity error, or worker failure affects only its target and route. It cannot cancel a batch, exclude content, or justify a reduced manifest. Blocking unresolved targets hold the patch; other exhausted targets preserve exact source and remain visibly unresolved.

## Operating rules

- Select and activate the SDK before data-plane work. Exact engine/version matches run from the bound branch checkout. A different identity may be a bound read-only code reference for `derive-new`, never a compatibility or promotion destination. Generated pass-through workspace tools are placeholders, not an active adapter.
- Inventory completeness requires two independently implemented ordered manifests. Result alignment, semantic review coverage, and runtime observation are separate claims.
- Context is target-scoped, not absent. Use narrative/voice, functional/UI, knowledge/prerequisite, adjacent, glossary, and immutable evidence relevant to that target.
- The top-level agent is a text-free control plane. It handles IDs, counts, categorical status, queue movement, artifact paths, and deterministic codes. Workers access private text and return only validated metadata envelopes.
- The top-level agent never inspects game-root files, including preliminary discovery; it passes paths to isolated workers. A missing SDK does not relax this boundary.
- Provider roles come from the selected preset. A key does not select a mode, change a role, or authorize another service. Parent-facing refusal is only `fallback_required`; provider rationale remains private.
- Translation is faithful transformation, not content generation. Independent review checks meaning, omission, voice, tone, function, conditions, units, terminology, allusions, placeholders, markup, control codes, ordering, and material layout risk.
- Build new outputs in the workspace. Automated checks use synthetic targets and never install or launch the real game.
- Apply proportional verification and stop after the changed layer passes. Keep small or tightly coupled work in the current agent; parallelize only independent bulk shards or genuinely independent research.
- Native passes are root-direct: no model controllers or worker delegation. Obey manifest budgets, checkpoint on any ceiling, and reread contracts only when their digest or control generation changes.

## Status-driven routes

Use the routing table in repository `AGENTS.md`. Common short paths are:

- full job: `sdk-selection.md` → `coverage-gates.md` → `review-pipeline.md`;
- native worker/control-plane work: `control-plane-contract.md` and `execution-budget.md`;
- manual/API fallback exchange: `translation-exchange.md`;
- exact user-approved wording correction: `minimal-corrections.md` without model or context calls;
- prior release, game update, or selective continuation: `release-checkpoint.md`;
- composed UI slots: `segment-metadata.md`;
- real runtime action: `runtime-approval.md` only after the matching explicit request;
- Windows delivery: `installer-contract.md` plus the selected Steam or filesystem packaging contract;
- publication: `instance-release.md` only after the user commands publication;
- reusable SDK promotion: SDK branch and engine FAQ contracts only after confirmed patch success.

After verified context, status routes to the complete primary translation/review pipeline. A missing, malformed, or stale completion attestation is never `ready`; incomplete results route to targeted gap resolution, and static completion routes to validation and packing. The completion attestation binds the exact result artifact, and the pack cache binds that input plus the current attestation and output digest before status can return `local_patch_ready`. Malformed state returns one metadata-only repair action instead of a traceback, while a failure queue bound to another inventory returns a stale-queue reconciliation action rather than blocking the current inventory.

For non-Steam/launcher-neutral delivery, `filesystem-package` is authoritative. `-GameRoot` accepts an absolute path or a path relative to the startup current directory. If omitted, the installer tries that validated startup current directory before prompting. It never discovers or controls a store client.

## Stable entry commands

```text
python -m pip install -e .
game-l10n init-workspace <external-workspace> --sdk-root <repository> --game-root <source-root>
game-l10n status --workspace <external-workspace>
game-l10n enter-control-plane --workspace <external-workspace> --contracts-read --source-unopened
game-l10n sdk-inventory --workspace <external-workspace> --refresh-remote
game-l10n sdk-evaluate --workspace <external-workspace> --target-engine <engine> --target-version <version-line> --branch <sdk-branch> --result <compatible|partial|reference|incompatible> --reason <reason-code> --evidence-path <public-contract-path>
game-l10n sdk-select --workspace <external-workspace> --target-engine <engine> --target-version <version-line> --mode <reuse|extend|derive-new|private-new> [--branch <sdk-branch>]
game-l10n sdk-activate --workspace <external-workspace> [--reference-checkout <reference-worktree>]
```

These are agent execution boundaries, not a human command cookbook. With an OpenCodex-compatible proxy, configure the proxy separately and run Codex against the Parr0t checkout; keep proxy credentials and configuration outside this repository.

## Completion

The final control-plane report contains only categorical coverage states, counts, deterministic issue codes, unresolved queue sizes, artifact paths, and the next required decision. Do not claim runtime coverage without separately authorized or user-executed observation.

For repository changes, run:

```text
python scripts/check_repo_privacy.py
python -m unittest discover -s tests -t . -v
```

Keep a sanitized SDK profile local after successful promotion. Commit, push, release, install, launch, and every other external effect remain dormant until the user requests that exact action.
