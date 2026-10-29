# Execution and validation budget

This contract prevents a localization job from expanding into speculative infrastructure. The objective is the requested playable patch, not the largest safety or compatibility system an agent can imagine.

## Scope freeze

At the start of each user request, write one active deliverable in the private workspace state. Work required to produce that deliverable is in scope. Optional migration systems, rollback archives, universal installers, compatibility matrices, broad refactors, public SDK promotion, and repository publication are separate deliverables and require an explicit request or a demonstrated blocker. Publication is never a blocker-resolution default: until the user explicitly requests it, do not suggest it, ask permission for it, stage it, or expose its repository, tag, release, or upload steps in user-facing progress.

Build the minimum working artifact first. If a simpler design satisfies the stated acceptance criteria, prefer it over a more general design. Do not preserve an obsolete implementation merely to support an update path the user did not request.

## Effort ladder

Use the lowest sufficient level:

1. **Inspect:** run the SDK reuse gate once, batch the necessary public-contract reads, and select reuse, same-identity extension, derived-new identity from a reference, or a private implementation. Do not spend build time recreating reusable code, and do not turn code reuse into ownership by the wrong compatibility branch.
2. **Build:** produce one new artifact without mutating the source root.
3. **Focused validation:** validate changed data, package structure, manifests, placeholders, and hashes once.
4. **Synthetic lifecycle:** for an installer change, run one isolated install, update, verify, and restore lifecycle against synthetic files. Include one declared original replacement only when that behavior changed.
5. **Real runtime validation:** launch or install into a real game only after the user explicitly requests that exact external action and the matching one-shot approval is recorded. An offline limitation or concrete runtime failure is a reason to ask, not permission to act.
6. **Broad regression:** run the repository suite and privacy scan before a repository commit or SDK publication, not after every private artifact edit.

Stop when the required level passes. Broaden or repeat validation only after input changes, a failed check, or an unresolved concrete risk.

## SDK promotion budget

Follow the preparation and fixed-base sequence in `docs/sdk-branch-contract.md`: consolidate acceptance criteria, check planned path ownership, freeze the intended `main` hash, implement, run focused checks, then run final acceptance on fixed inputs. Generalization and missing synthetic fixtures are implementation work, not deployment overhead. Keep small coupled changes in the current agent.

Run the broad core/adapter suites and privacy/FAQ gates once after the final code and base are settled. Reuse passing evidence for unchanged inputs; changes to relevant code, contracts, fixtures, or dependencies require the affected checks again. Do not weaken a gate to meet the time budget. Create the final delivery archive, export, and report after acceptance, once. Never package midway through a review loop merely to report progress.

Measure implementation, final validation/packaging, user wait, and external publication separately. Use actual command elapsed times for network or signing claims. Parallel review overlaps implementation and must not be added again to wall-clock totals. A readiness check must not expand into a new scheduler, duplicate verifier, credential probe, or speculative deployment system.

For all-branch maintenance, follow `docs/branch-update-contract.md`. Refresh refs once and reuse one pinned inventory. Skip merge preparation and broad validation for already synchronized targets, hold known core overrides early, and reuse unchanged evidence for remaining candidates. Equivalent current core content may skip a history-only merge; a successful future-core simulation does not mean missing core changes are already installed. Do not rerun suites after an applied merge whose tree exactly matches the validated tree.

## Small correction budget

When the user supplies the final replacement wording for a typo or small translation fix, use `docs/minimal-corrections.md`. Resolve its stable IDs, run `game-l10n apply-user-correction`, repack the owning payload once, and verify the package once. The expected model-call and context-analysis budget is zero. Do not delegate the correction, reopen full source discovery, re-read every translation, rehash the game tree, or create a job-specific apply/validation script. If reverse dependencies exist, review only those IDs; if mapping is ambiguous, perform one narrow static lookup before changing anything.

When final wording was not supplied, a bounded tone, voice, nuance, or naturalness correction still uses that document's focused route. Freeze at most 32 directly affected and reverse-dependent IDs, prepare context once, use one ordered rewrite batch and at most one ordered focused-review batch, then merge and package once. Do not convert a small correction into a full translation or review pass merely because a model must choose the final wording.

## Work-lane and publication fence

Do not reuse a checkout merely because another session already has it open. Before a write, bind the current request to exactly one lane: private game job, read-only supervision, core maintenance, or one SDK branch. Resolve the intended repository and branch from the current request and inspect their status once. A supervision or analysis lane may observe and send authorized corrective messages, but it must not patch, commit, or push.

Before every commit or push, enumerate the outgoing commits and changed paths against the current request. An outgoing commit created by another lane, an unexpected remote delta, or a branch not named by the active deliverable is a stop condition for publication. Record the mismatch as metadata and reconcile it in the correct maintenance session; never solve it with an automatic force push, reset, revert, or all-branch update.

## Retry and loop limits

- Do not repeat a successful command when its input fingerprint is unchanged.
- After the same operation fails twice for the same cause, stop repeating it. Diagnose once, simplify the design or choose a different route, and record the categorical blocker.
- Poll a worker only when new progress is expected. Prefer a bounded wait over frequent status checks.
- Do not create a second verifier for a property already covered by a deterministic validator.
- Do not retranslate, re-review, recompile, or repackage unchanged inputs merely for reassurance.

Configured translation retries are fresh failed-only tasks, not extra calls hidden inside one semantic worker. Their provider and ordinal are manifest-bound, successful rows are immutable, and cumulative usage telemetry carries across the Luna, Sol, and external-fallback stages. The preset's five Luna retries and three Sol retries are explicit route semantics, not whole-job usage ceilings and not permission for the control plane to invent more attempts; each count is in addition to the route's first call. No whole-job time, token, call, or cost gate exists unless the user stated it first and the manifest records it. Never ask the user to create one as a prerequisite.

## Delegation budget

Use subagents when work divides into independent, non-overlapping bulk translation or review shards, or when parallel research materially shortens a blocking discovery phase. Keep simple packaging, a single script, a small bug fix, documentation, state inspection, and deterministic validation in the current agent.

Every delegated worker must have an exclusive output and a clear completion envelope. Do not delegate a duplicate review of a deterministic pass merely to increase confidence.

For native bulk localization, create stable exclusive ID tasks once with `prepare-native-pass`, then let `native-pass-step` select every wave. Dispatch only the returned task/result paths, use one bounded wait, and call the step again; it imports the complete wave and returns one next action. Do not inspect the manifest to choose work, launch one subagent per target, import individual shards, or poll repeatedly.

The v7 task file contains the complete bound segment payload. A worker consumes that one immutable task snapshot and must not open or scan the complete ordered source artifact. If a command-output transport would truncate the snapshot, deterministic ordered ranges of that same digest-bound file are one logical read, not permission to reread or expand scope; the worker verifies complete count and order before its single semantic call. That artifact is framework-owned validation input, not a worker prompt source. This prevents every fresh worker from replaying the whole inventory before processing its small exclusive shard.

## User-opt-in Codex control-turn budget

Native orchestration is core-directed and relay-isolated. The root control plane never dispatches or receives a semantic worker directly. `native-pass-step` writes one exact metadata-only wave artifact, and the root gives only its path to one fresh no-history relay. That relay is a disposable transport boundary, not a controller: it may launch the bound workers once and aggregate categorical completion only. It cannot plan, retry, split, select routes, choose models, open semantic task payloads, or forward raw child responses. After its lifecycle ends, it executes the artifact's `seal-native-wave-relay --relay-ended` command so core—not the relay or root—derives missing bound tasks and records operational failures. Semantic workers must not delegate. The maximum depth is relay plus semantic worker below the root.

The root also does not select a wave. `native-pass-step` persists the pass control state, counts repeated observations, records idempotent aggregate usage events, validates complete result sets, imports them atomically, and emits one of `dispatch_isolated_wave_relay`, `wait_for_active_wave`, `checkpoint_and_stop`, or `aggregate_completed_pass`. Any other recovery sequence is outside the contract.

The v7 task manifest is the executable budget and detail contract; existing v6 checkpoints remain readable under their legacy split contract. One wave uses one relay and at most `routing.max_workers` semantic workers. Root call/token ceilings are disabled by default. The framework, an agent, or elapsed wall time must not create, infer, announce proximity to, or enforce a root wave/turn limit unless the user explicitly requested a concrete ceiling and the manifest records `control_turn_budget_user_approved=true` with that positive limit. A long turn, repeated bounded waits, context pressure, or a model's estimate is not an executable limit signal.

Without a user-approved control-turn budget, continue through inventory, reconciliation, context, semantic passes, validation, and packaging until `local_patch_ready` or a genuine actionable blocker. Do not use “return control,” a progress summary, or a voluntary final answer as a substitute for completion. If an approved limit is reached, only deterministic core accounting may emit the checkpoint action; the model must not predict or self-declare it. Every semantic worker still ends after its one bound task, and review splitting still follows only the core-owned v7 contract. No relay or worker sends free-form parent-facing progress or accepts a follow-up task.

Read each required contract set once per control-plane generation. `game-l10n status` returns `contract_set.digest`; reread only when that digest or the generation changes. A context state bound to the current independently verified inventory fingerprint is sufficient for status; do not parse the same ordered inventory again merely to reproduce its segment digest. Tool output and worker responses stay in artifacts. The root receives only fixed-size metadata and must not turn each command, wait, shard completion, or unchanged observation into another narrated model turn.

Emit one progress update after a complete wave or on an actionable blocker. Per-task, per-small-batch, per-wait, and unchanged status narration is not progress and is outside the control-turn budget.

## Executable detail-contract budget

Follow `docs/detail-contract.md`. The native v7 manifest is the only executable job/task detail contract for new work. Do not promote a generated Markdown plan, incident ledger, repair diary, or coordinator note into worker policy. Such a note cannot raise a limit, alter provider/pipeline/isolation/authorization rules, require repeated proof, or convert a prepared batch into per-record semantic calls.

Every task permits one semantic model/API batch call and one ordered result containing a status for every bound target. Load shared context once. Do not generate one prompt artifact per record, run an inner per-record model/API loop, or count a directory of calls as one task. Review may continue only through new failed-subset manifests returned by `native-pass-step`; successful rows are immutable and inherited usage remains bound to the whole chain. If a provider accepts only one target, repartition before dispatch so the manifest makes that cost visible.

`execution.bulk_budget_gate_records` (default 4,096) remains a compatibility/reporting field and never authorizes the framework to invent a budget or stop after a pilot. With no explicit user limit, every wave remains eligible and the pass continues from its checkpoint. If the user explicitly sets a finite whole-pass budget, it applies at any inventory size and requires positive elapsed-minute and aggregate-input-token ceilings plus either metered provider cost or an unmetered declaration. The elapsed ceiling starts when that approved budget is first bound; earlier waiting time is not execution time. Existing input-token and provider-cost usage remains cumulative, and migrating an already-approved pass preserves its original elapsed start. A legacy unapproved pilot lock is non-authoritative and must release without rebuilding the manifest or discarding results.

## Residual convergence gate

At the end of a complete translation, judgment, or rewrite pass, freeze the remaining target IDs, candidate digests, and existing context fingerprints. When the remainder is no larger than `execution.residual_direct_records` (default `32`), `prepare-native-pass` must produce one direct residual task for one fresh no-history worker. This is a terminal convergence path, not a reason to design another workflow.

Do not create a job-local scheduler, controller, lease/owner database, liveness probe, memoization layer, per-target proof files, or supplemental context pack for this residual task. Do not repartition it, reopen passed targets, or repeat full aggregation. Missing evidence may be added only when an existing deterministic validator names the exact absent artifact; otherwise use the already attested context.

Aggregate results once when the worker finishes. A capacity failure, missing native slot, or worker startup failure ends that attempt, records the unchanged task in resumable operational state, and returns control. It must not auto-restart the root turn or generate another controller. Keep ordinary wave transitions silent; report only a completed pass/checkpoint or actionable blocker. One unchanged observation after a bounded wait, or two control actions that do not change a target state, is enough to stop polling and report the categorical blocker. Semantic rewrites remain bounded by `routing.review_max_rounds`.

After semantic completion, packaging, README assembly, release metadata, and deterministic finalization stay in the current agent. Do not open new translation or review workers for those serial steps. If a framework bug blocks finalization, hold the instance checkpoint, fix the reusable core separately, and rerun only the failed deterministic gate; do not re-review unchanged results.

## Native model context budget

Native workers are data-plane executors, not long-lived chats. Start every translation or review task in a fresh worker with no inherited parent conversation (`fork_turns="none"` or the host equivalent). Give it only the immutable worker contract and private workspace/source/context/task paths. It reads and writes files directly and returns only task ID, counts, categorical states, issue codes, and artifact paths. Never paste source text, candidates, inventories, code listings, or command output into agent messages.

One worker processes one task and then ends. Do not send another shard through a follow-up turn, even when the worker is idle. Long-lived workers accumulate every previous target and tool result; replaying that history on each next target makes input usage grow superlinearly while adding no translation evidence.

Keep one canonical extraction/source snapshot and delta outputs. A review wave, package revision, synthetic test, or documentation update must not make another full game-tree copy. Temporary extracted or build trees must have one declared owner and be removed or retained by a deterministic checkpoint rule, not accumulated per attempt.

`prepare-native-pass` v7 keeps translation/rewrite defaults at 64 IDs and 8,000 estimated private material tokens per task, with a preferred eight-record minimum. The smaller transport-safe token bound prevents a valid immutable task from being clipped by ordinary Codex command-output limits. `--pass-kind review` exposes hard ceilings of 2,048 IDs and 262,144 estimated material tokens, but initial v7 tasks are packed to at most 256 IDs and 65,536 estimated material tokens. Failed-only child manifests halve the prior task bounds and retain cumulative chain usage. The estimate sizes scheduling material only and is not a provider billing claim. Lower either hard maximum when focused context, records, or expected output is large. A single oversized target remains isolated. Reuse the cached task manifest only when the source fingerprint, pass kind, sizing/worker/budget bounds, contract fingerprint, and manifest version match.

Workers write result files using the exact task/result paths returned by `native-pass-step`. On the next call, it validates every result against the exclusive task order before writing any shard and returns aggregate counts only. Merge with the same manifest so stale or unrelated shards are ignored. `import-native-wave`, the one-shard import, and unbound merge remain compatibility paths, not model-selected recovery routes.

An older workspace that still has either the former implicit 256-record setting or the v3 32/12,000 defaults and no v4 policy marker automatically receives the new v4 bounds. Explicit `--max-records`, `--min-records`, `--max-material-tokens`, and `--max-workers` values remain authoritative.

## Large-inventory startup budget

At 10,000 or more records, startup is a bounded linear phase. The primary and independent workers each enumerate the source once; each changed inventory generation gets one ordered core comparison. If that comparison is incomplete, status emits an `inventory_reconciler` task bound to the cached pair and current gap vector. A lower gap score requires another bounded gap-only generation; it does not authorize a rescan or a terminal failure report. Do not add another full extraction, whole-tree rescan, per-record agent, proof file, or speculative tool before context.

An unchanged verified inventory uses its saved path/size/mtime binding instead of reparsing the complete inventory on every status, translation, review, or resume command. JSONL input is streamed, and one command reuses the same in-memory segments when translation input and context inventory are the same file. A changed binding falls back to full semantic fingerprint verification.

`native-pass-step` validates only tasks in the wave it is about to dispatch or import. It stores imported-wave counters in its control checkpoint and does not reopen every pending task or old shard at each step. Final merge still validates the complete binding once. Repeated discovery, unchanged inventory verification, and all-task validation are not startup progress and must not postpone the first semantic wave.

When native usage telemetry is available, the bound relay/provider adapter records aggregate input, cached-input, output, reasoning, event, and worker counts in the core-owned private ledger or sealed receipt. Root reads only that metadata. `input_tokens_known=false` and `telemetry_status=unmeasured` mean that a displayed numeric zero is only the legacy accumulator default, never evidence of zero use; `estimated_material_tokens_*` is workload size, not billed usage. Missing telemetry is unknown; never reconstruct it from `.codex/sessions`, chat transcripts, agent trees, nested-worker status/output, semantic-worker logs, or terminal previews. Per-worker accounting must be emitted at the event boundary. A high cached-input ratio diagnoses replayed history but does not authorize ending an incomplete job or inventing a turn limit. Keep later semantic work in fresh no-history workers and continue through the deterministic checkpoint.

## API fallback input budget

One translation, judgment, or rewrite request contains one target plus a focused projection of the already prepared context. Never attach a complete context pack, exchange bundle, inventory, prior result set, or application plan to `Segment.metadata`; typed context is supplied separately and only target-relevant glossary, relationship, feature, screen, knowledge, and reference evidence is projected. Stable job-wide fields precede target-specific fields so a compatible provider may reuse a common prefix.

The DeepSeek presets cap the combined system-and-user character count at 24,000 for each translation, compact judgment, and rewrite request. This is a deterministic preflight limit, not a token estimate. It applies at runtime to older preset workspaces even when their stored configuration predates the field. Oversized input returns non-retryable `budget_exhausted` before any network call, keeps that exact target pending, and must be fixed by removing duplicated context or explicitly configuring a justified larger bound; it must not trigger scope reduction or source loss. Normal retry policy remains unchanged for actual retryable provider failures.

Record provider-reported total input, cached input, cache-miss input, output, and reasoning usage when available. A cache hit is useful telemetry, not permission to send redundant context. Do not create a job-local API loop that bypasses the configured adapter and these limits.

The fixed translation-fallback processor always uses the DeepSeek preset's per-request input preflight. This is the only automatic usage guard. A whole-job ceiling and its core-owned usage ledger are optional and exist only when the user stated the ceiling first. In that opt-in case, the first call requires a confirmed pre-fallback input-token baseline and, when elapsed time is bounded, the original job start. Missing telemetry is not zero. The processor checkpoints fixed exchange rows after each wave, records provider-reported usage once, and stops before another wave only when that user-set ceiling is exhausted. An interrupted metered event with uncertain usage blocks later bundles until that exact event is reconciled; the control plane must not guess a value, reset the ledger, or create a replacement counter.

## Proportional safety baseline

Every patch still requires these boundaries:

- keep the configured source root immutable;
- create a new artifact in the private workspace;
- reject path traversal and unowned collisions;
- manage only paths listed by the patch manifest or receipt;
- verify the produced package once;
- preserve a clear Restore path for patch-owned files and any explicitly replaced originals.
- deny real install, update, remove, and launch by default; follow `docs/runtime-approval.md` when the user explicitly requests one.

These boundaries do not imply a full game backup, hashing every source file, embedding the previous package, supporting unknown legacy states, or designing automatic rollback across arbitrary versions. Add those capabilities only when the user requests them or a concrete target requires them.

Runtime patch code must cache immutable translation tables and indexes outside hot lookup paths. Per-lookup full-table reads or scans, per-frame scene-wide scans, and per-event global object scans are invalid even when a small synthetic fixture passes.

## Completion report

Report the artifact, the checks that materially establish acceptance, remaining user validation, and any concrete limitation. Do not present repeated checks, polling events, or speculative safeguards as progress.
