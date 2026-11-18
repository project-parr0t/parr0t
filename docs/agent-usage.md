# Parr0t agent operation guide

`AGENTS.md` defines invariants; load stage documents on demand.

## Supported control paths

Supports Codex App/CLI, [OMP](control-plane-contract.md#omp-entry), and OpenCodex-compatible proxies. OMP: prompt separation; optional host-enforced guards.

## Start or resume

Resolve these locations before acting:

- repository: framework and selected SDK;
- game root: read-only input;
- workspace: all private and generated data.

Text-only default: images require `image-localization.md` policy; target language stays Korean unless the user explicitly changes it through `target-language-policy`.

Semantic localization is content-network deny by default. Apply `skills/translation-source-isolation/SKILL.md`; it defines exact grants, immutable policy, direct-model provenance, and taint rejection. Local inventory inspection needs no grant.


For a new job:

1. Confirm repository/workspace access and only that the source path exists; do not inspect its contents.
2. Initialize; bind `user-request`; follow status. `enter-control-plane --workspace <workspace> --contracts-read --source-unopened`. Exposure/recovery: `control-plane-contract.md`. Never auto-launch replacements.
3. Ask once whether Git may be used for this game task; record only the answer. Disabled forbids Git mutation/publication in that workspace, not authenticated SDK access, download, inventory, or activation. Never infer the answer from transport or missing `.git`.
4. Bind `native-runtime --host codex|omp` before dispatch. Model overrides use `--role-model role=exact-id --user-requested-models`. Preserve host/isolation and started bindings; see `codex-app-deepseek.md`.

For an existing job, start with status and reuse valid checkpoints. Do not repeat work with unchanged fingerprints.

Continuation/diagnosis: `control-plane-contract.md`. Analysis/shutdown requires the main host's `parr0t_framework_mode` and interactive user approval. Flags and children cannot authorize changes; exposure/recovery gates remain.

## State machine

```text
workspace and authorization
  -> SDK identity and selection
  -> exact SDK implementation activation
  -> two independent ordered inventories
  -> inventory reconciliation
  -> typed context analysis
  -> primary/secondary translation retries for failures only
  -> external fallback for the remainder
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
- Independent ordered inventories prove static—not runtime—coverage. Raw presence or `declared_complete` never establishes display scope.
- At 10,000+ records: scan once/method, cache evidence, reconcile identities before expansion, then repair gaps. Denominator growth is not progress.
- Keep relevant narrative/voice, functional/UI, knowledge, adjacency, glossary, and immutable evidence target-scoped.
- The text-free parent handles only metadata and dispatches core relays; it never inspects game-root or semantic contents. A missing SDK changes nothing.
- Explicit initial models override preset defaults; preserve provider permissions, transport, and unspecified roles. `codex-app-deepseek.md`.
- Translation retries default to Luna 5, Sol 3, then DeepSeek; first calls excluded. Unbound routes remain pending.
- Sol strong review: ≤2 calls/target across retries/confirmation/resume. Bind initial fallback model/count with `review-policy`; missing/incompatible bindings stay pending, never substitute DeepSeek. `review-pipeline.md`.
- Translation is faithful transformation, not content generation. Independent review checks meaning, omission, voice, tone, function, conditions, units, terminology, allusions, placeholders, markup, control codes, ordering, and material layout risk.
- Build only in the workspace; synthetic checks never install or launch games.
- Apply proportional verification and stop after the changed layer passes. Keep small or tightly coupled work in the current agent; parallelize only independent bulk shards or genuinely independent research.
- Native uses `native-pass-step` and `--relay-ended`; review packing/splitting limits are in `control-plane-contract.md`. They are not whole-job limits: never solicit one unless the user first states it. Only reached DeepSeek fallback requests receive automatic input preflight.
- Observe finitely and interruptibly. Operational errors require owner cancellation/sealing before configured fallback. Preserve successes/evidence; missing bindings stay pending.

## Status-driven routes

Use the routing table in repository `AGENTS.md`. Common short paths are:

- full job: `sdk-selection.md` → `coverage-gates.md` → `review-pipeline.md`;
- native worker/control-plane work: `control-plane-contract.md`, `execution-budget.md`, and `detail-contract.md`;
- manual/API fallback exchange: `translation-exchange.md`;
- exact user-approved wording correction: `minimal-corrections.md` without model or context calls;
- dialogue/reference flag without a stable ID: use `minimal-corrections.md` and its local correction UI once; accept its frozen export and never repeat a global inventory, text, or vector search;
- prior release, game update, or selective continuation: `release-checkpoint.md`;
- composed UI slots: `segment-metadata.md`;
- explicitly requested UI-image translation: `image-localization.md`;
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

A data-plane relay never reads or forwards child responses. At termination it runs its bound `seal-data-plane-task --worker-ended` once and returns only the receipt path. Core seals output or records failure; status schedules retries. Never trust placeholder artifacts. Portable inventory requires an authenticated connector or immutable commit archive; never invent branch or commit. Keep proxy credentials external.

## Completion

Localization reports contain categorical coverage, issue codes, queues, counts, and paths only. Diagnostic reports may explain framework defects, not reproduce private logs. Never claim unobserved runtime coverage.

For repository changes, run:

```text
python scripts/check_repo_privacy.py
python -m unittest discover -s tests -t . -v
```

Keep a sanitized SDK profile local after promotion. External effects remain dormant until explicitly requested.
