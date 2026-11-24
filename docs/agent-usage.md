# Parr0t agent operation guide

`AGENTS.md` defines invariants; load stage documents on demand.

## Supported control paths

Supports Codex App/CLI, [OMP](control-plane-contract.md#omp-entry), and OpenCodex-compatible proxies. OMP: prompt separation; optional host-enforced guards.

## Start or resume

Resolve these locations before acting:

- repository: framework and selected SDK;
- game root: read-only input;
- workspace: all private and generated data.

Text-only default: bounded font/visual inspection is not image-translation approval. Requested player-visible image scope follows `image-localization.md` and need not wait for all text work. Target language defaults to Korean; explicit initialization or selection records the language policy itself.

Semantic localization is content-network deny by default. Apply `skills/translation-source-isolation/SKILL.md`; it defines exact grants, immutable policy, direct-model provenance, and taint rejection. Local inventory inspection needs no grant.


For a new job:

1. Confirm repository/workspace access and only that the source path exists; do not inspect its contents.
2. Initialize; bind `user-request`; follow status. `enter-control-plane --workspace <workspace> --contracts-read --source-unopened`. Exposure/recovery: `control-plane-contract.md`. Requested OMP automation may establish its first official controller without an extra confirmation dialog. A separate live or pending session does not block the requested launch.
3. Git is disabled by default; enable only on explicit user choice. Disabled forbids game-workspace Git mutation/publication, not already-authorized SDK access, download, inventory, or activation. Never infer consent from transport or missing `.git`.
4. Bind `native-runtime --host codex|omp` before dispatch. Model overrides use `--role-model role=exact-id --user-requested-models`. Preserve host/isolation and started bindings; see `codex-app-deepseek.md`.

For an existing job, start with status and reuse valid checkpoints. Never repeat locked successes; only core-owned unfinished-target retry or changed-binding repair may reissue work.

Continuation/diagnosis: `control-plane-contract.md`. The ordinary OMP bootstrap supplies root `parr0t_session` and `parr0t_framework_mode` as well as native process execution. Entry/lifecycle requires the user's requested action (`user_requested: true`), not a compulsory interactive dialog. An explicit absolute `workspace` selects the launch target for `parr0t_session`, including from a bound root; omitting it uses the caller's binding. `parr0t_framework_mode` remains restricted to the bound workspace in dedicated sessions. The launcher preserves the current root model/thinking unless an explicit exact model is selected. Clean pause/analysis/shutdown can resume with `mode: "localization"`; actual semantic/history exposure requires canonical fresh recovery. Children cannot authorize entry or mode changes. Do not confuse a missing caller tool with the registered child controller's tool state.

## State machine

```text
workspace and authorization
  -> SDK identity and selection
  -> exact SDK implementation activation
  -> bounded locale-path probe in the active SDK
  -> ordered locale values, or ordered positive display targets if locale is unavailable
  -> required reconciliation
  -> typed context analysis
  -> configured translation routes for unfinished targets
  -> independent compact review at the bound assurance/policy
  -> selective rewrite and confirmation
  -> configured review fallback for failures only
  -> deterministic validation
  -> recoverable packed patch or held checkpoint
  -> local_patch_ready
```

These are per-cohort dependencies, not mandatory whole-job barriers. Core advances independent completed cohorts and groups heterogeneous pending routes. Continue until `local_patch_ready`, an explicit user pause/stop, or a concrete error or authorization gate with no remaining authorized repair or independent work. Build missing in-scope capability rather than stopping at an inconclusive check. Do not repeat unchanged failed work or bypass coverage or consent. Blocking unresolved targets hold normal delivery unless the user requests a separate honestly incomplete artifact; invalid-integrity candidates remain forbidden.

## Operating rules

- Select and activate before data-plane work. Bind exact matches to a Git checkout/worktree or authenticated portable snapshot. Portable activation records the full commit ID and verifies normalized content. Other identities are read-only `derive-new` references, never promotion targets. Pass-through tools are placeholders.
- Before text enumeration, verify the active source loader, source catalog, and reversible value application. These checks select locale-only inventory; do not also scan code/strings or review screens. Unresolved source checks hold the affected scope. Target activation is diagnostic and does not gate inventory. See `docs/engine-static-scope.md`.
- Independent ordered inventories prove static—not runtime—coverage. `require_independent_inventory: false` selects primary-only assurance. Admit only positive display bindings or verified locale values; keep unresolved display dependencies outside semantic work and report them as pending.
- Cache scans and repair bound gaps proportionally; no arbitrary record-count threshold changes permission or coverage.
- Keep relevant narrative/voice, functional/UI, knowledge, adjacency, glossary, and immutable evidence target-scoped.
- The text-free parent handles only metadata and dispatches core relays; it never inspects game-root or semantic contents. A missing SDK changes nothing.
- Explicit exact role/route selections override initial preset defaults; preserve host authorization, provider transport, and unspecified roles. OMP bare IDs require one authoritative exact metadata match; never prefix-guess. Availability is not authorization.
- Translation order/counts come from the bound policy. New presets use one initial call and zero same-route translation retries; preserve existing explicit counts and never repeat locked successes.
- Review limits are user-selected, not Sol-name caps. Missing fallback count defaults to 3 total calls; a missing exact authorized route remains pending. `review-pipeline.md`.
- Translation is faithful transformation, not content generation. Independent review checks meaning, omission, voice, tone, function, conditions, units, terminology, allusions, placeholders, markup, control codes, ordering, and material layout risk.
- Build only in the workspace; synthetic checks never install or launch games.
- Apply proportional verification: stop repeating a check after the changed layer passes, then advance the next eligible action. Keep small or tightly coupled work in the current agent; parallelize only independent bulk shards or genuinely independent research.
- Native uses `native-pass-step` and terminal core seals. Only explicit user time/token/cost limits constrain the whole job; per-request capacity is separate and routes to safe sharding/compaction or actionable repair.
- Observe finitely and interruptibly. Operational/admission errors are not semantic refusal/exhaustion: the owner ends started children and seals, then core creates a fresh manifest for only unfinished targets on the next approved configured route. Preserve successes/evidence; no successor means actionable repair, not source preservation or unchanged retry loops.
- Prompt OMP needs its loaded helper and `parr0t_process`; guarded mode additionally needs guard evidence and `parr0t_exec`. Actual readiness checks both the mode-specific native tool and root `parr0t_framework_mode`. Top-level catalog absence alone is not missing registration: check the `xd://` device when deferred. Registered lifecycle devices enforce the same actual root authority as direct calls; they are not writable lifecycle metadata. Unknown harmless local tools are not blanket forbidden; messaging, private history, content/network scope and exact bound assets remain protected.
- Local packaging may use a validated minimal integrity checkpoint. Full publication retains full release/coverage gates; a lower assurance or explicitly incomplete artifact must remain honestly labeled.


Status validates current bindings; invalid/stale metadata routes repair, never readiness. Pack state binds validated results, attestation, and output.

For non-Steam/launcher-neutral delivery, `filesystem-package` is authoritative. `-GameRoot` accepts an absolute path or a path relative to the startup current directory. If omitted, the installer tries that validated startup current directory before prompting. It never discovers or controls a store client.

## Stable entry commands

```text
python -m pip install -e .
game-l10n init-workspace <external-workspace> --sdk-root <repository> --game-root <source-root>
game-l10n status --workspace <external-workspace>
game-l10n policy --workspace <external-workspace> --git-choice <enabled|disabled> --user-confirmed-git-choice
game-l10n enter-control-plane --workspace <external-workspace> --contracts-read --source-unopened
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
