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

Configured translation retries are failed-only tasks bound to their provider and ordinal; successes remain immutable. Preset retry counts are defaults, not frozen topology: explicit user route/model choices and configured retry counts remain authoritative, and only missing settings receive defaults. Independent time, token, and cost ceilings are optional user hard ceilings; there is no default whole-job quota. Safe task sizes, worker counts, and per-request preflights are operational defaults, not user hard ceilings. Unknown metered usage remains unknown.

Deterministic acceptance is path-independent. Single-target, deferred batch, and fallback results pass the same structure, protected-token, glossary, UI, and locked-target-language gates before a success is cached. An evidence-bound optional UI slot may validly produce an empty string; absence of a candidate is represented separately and must not be inferred from text truthiness. Provider-independent impossible constraints, such as protected tokens longer than a bound UI character limit, fail preflight without spending a model call. That preflight rejection is not a provider attempt and cannot be used as route-exhaustion evidence or authority to preserve the source.

Native retry collection applies the same bound optional-slot and integrity checks as native-pass ingestion. Exact empty output may remain a successful candidate when its inventory evidence permits it; unbound empty output and whitespace substitutions remain invalid.

## Delegation budget

Use subagents when work divides into independent, non-overlapping bulk translation or review shards, or when parallel research materially shortens a blocking discovery phase. Keep simple packaging, a single script, a small bug fix, documentation, state inspection, and deterministic validation in the current agent.

Every delegated worker must have an exclusive output and a clear completion envelope. Do not delegate a duplicate review of a deterministic pass merely to increase confidence.

For native bulk localization, create stable exclusive ID tasks once with `prepare-native-pass`, then let `native-pass-step` select work. Dispatch only its returned relay path; core imports available task results, preserves awaiting targets, and exposes the next eligible task or repair without requiring every wave member to finish. Do not inspect manifests to choose work or launch a worker per target.

The v7 task file contains the complete bound segment payload. A worker consumes that one immutable task snapshot and must not open or scan the complete ordered source artifact. If a command-output transport would truncate the snapshot, deterministic ordered ranges of that same digest-bound file are one logical read, not permission to reread or expand scope; the worker verifies complete count and order before its single semantic call. That artifact is framework-owned validation input, not a worker prompt source. This prevents every fresh worker from replaying the whole inventory before processing its small exclusive shard.

## User-opt-in Codex control-turn budget

Native orchestration is core-directed and relay-isolated. `native-pass-step` selects and imports waves, while the root passes only the exact relay artifact to a fresh isolated relay. The relay dispatches its bound tasks and records categorical completion; it cannot plan, retry, split, select routes/models, inspect payloads, or forward raw responses. Workers are fresh, end after one task, and cannot delegate. Core owns continuation and preserves completed results.

`native-pass-step` persists control state, records usage idempotently, validates and imports available task results, and returns the next core-selected action. Missing targets remain pending; independent cohorts and deterministic repairs may proceed without waiting for a complete wave. Action selection remains core-owned.

The v7 manifest is the executable detail contract for new work; existing v6 checkpoints remain readable under their serialized contract. Configured worker and task-size values are scheduling defaults with structural safety limits, not unexplained policy ceilings. Use host/model capacity and safe sharding/backpressure; honor positive explicit task bounds. Optional user time, token, and cost ceilings are independent and apply only when explicitly configured. A long turn, wait, or model estimate is not an executable limit.

Continue through eligible work until completion or an actionable blocker. Partial task imports are valid: retain imported successes, keep missing or failed targets pending, and dispatch available independent work rather than imposing a whole-wave or no-progress barrier. A repair-needed but unattempted task is not semantic route exhaustion. A real user stop remains authoritative; no progress narration substitutes for work.

Semantic cache identity follows the exact focused prompt material a target can consume plus job-wide glossary, semantic routing, and provider bindings. Unrelated scene, screen, feature, or knowledge entries and execution-only parallelism must not invalidate a completed target. Relevant context, style anchors, glossary, provider, or semantic-policy changes still invalidate it. Legacy cache keys remain a read-only migration fallback.

Read each required contract set once per control-plane generation. `game-l10n status` returns `contract_set.digest`; reread only when that digest or the generation changes. A context state bound to the current independently verified inventory fingerprint is sufficient for status; do not parse the same ordered inventory again merely to reproduce its segment digest. Tool output and worker responses stay in artifacts. The root receives only fixed-size metadata and must not turn each command, wait, shard completion, or unchanged observation into another narrated model turn.

Context startup and resume are manifest-driven. Core partitions only unfinished semantic records, caps active workers by the number of pending chunks, and issues zero semantic slots when none remain. Each slot loops over core atomic claims rather than receiving an invented chunk list. A single deterministic finalizer aggregates once per wave. Do not repeatedly enumerate the inventory, recreate chunk formats, accept a summary as a result fragment, spawn analyzers for already committed work, or merge after every small completion.

Report meaningful progress when useful or when an actionable blocker requires a decision. A complete-wave boundary is not required for progress reporting or continuation; avoid per-task, per-small-batch, per-wait, and unchanged-status narration.

## Executable detail-contract budget

Follow `docs/detail-contract.md`. The native v7 manifest is the only executable job/task detail contract for new work. Do not promote a generated Markdown plan, incident ledger, repair diary, or coordinator note into worker policy. Such a note cannot raise a limit, alter provider/pipeline/isolation/authorization rules, require repeated proof, or convert a prepared batch into per-record semantic calls.

Every task permits one semantic model/API batch call and one ordered result containing a status for every bound target. Load shared context once. Do not generate one prompt artifact per record, run an inner per-record model/API loop, or count a directory of calls as one task. Review may continue only through new failed-subset manifests returned by `native-pass-step`; successful rows are immutable and inherited usage remains bound to the whole chain. If a provider accepts only one target, repartition before dispatch so the manifest makes that cost visible.

`execution.bulk_budget_gate_records` is retired and removed from older configuration, not an effective pilot gate. Without user-set ceilings every eligible wave remains dispatchable. Elapsed-time, aggregate-token, and provider-cost limits are independently optional; require accounting only for selected dimensions. Unknown usage remains unknown, never zero.

## Bounded residual work

At the end of a pass, core may form a bounded residual task from remaining targets, preserving their candidate digests and context bindings. The configured residual size is a default, not a universal ceiling; explicit positive task bounds are honored. Dispatch remains behind the isolated relay. Do not reopen successful targets or repeat full aggregation.

Do not create a job-local scheduler, ownership database, or duplicate verifier. Missing evidence may be added only when a deterministic validator identifies the exact artifact; otherwise reuse attested context.

Import completed task results when available; partial waves do not block independent cohorts or available deterministic repairs. Capacity, admission, and startup failures remain resumable operational state and do not consume an unstarted semantic attempt. Do not treat absent/quiet progress as terminal failure. Semantic rewrite bounds are configurable policy, not hidden depth or amplification clamps.

Relay lifecycle observation is finite and interruptible per host call. It is
not a job deadline and elapsed time is never an operational-failure signal.
Do not enter an eval wait-until-all that prevents error recording or prompt
cancellation; after each observation return to core, and on an actually
observed provider/guard error cancel the owned child and establish terminal
lifecycle before any replacement task is dispatched.

Inventory reconciliation route succession is opt-in through
`routing.data_plane_routes.inventory_reconciler`, an ordered list of already
bound native routes. If omitted, the existing `native-sol` behavior remains.
Only a sealed `operational_error` advances to the next configured entry;
semantic failures retain the same route and ordinary attempt behavior. An
exhausted configured list remains pending and never proves route exhaustion or
permits source preservation.

After semantic completion, packaging, README assembly, release metadata, and deterministic finalization stay in the current agent. Do not open new translation or review workers for those serial steps. If a framework bug blocks finalization, hold the instance checkpoint, fix the reusable core separately, and rerun only the failed deterministic gate; do not re-review unchanged results.

## Native model context budget

Native workers are data-plane executors, not long-lived chats. Start every translation or review task in a fresh worker with no inherited parent conversation (`fork_turns="none"` or the host equivalent). Give it only the immutable worker contract and private workspace/source/context/task paths. It reads and writes files directly and returns only task ID, counts, categorical states, issue codes, and artifact paths. Never paste source text, candidates, inventories, code listings, or command output into agent messages.

One worker processes one task and then ends. Do not send another shard through a follow-up turn, even when the worker is idle. Long-lived workers accumulate every previous target and tool result; replaying that history on each next target makes input usage grow superlinearly while adding no translation evidence.

Keep one canonical extraction/source snapshot and delta outputs. A review wave, package revision, synthetic test, or documentation update must not make another full game-tree copy. Temporary extracted or build trees must have one declared owner and be removed or retained by a deterministic checkpoint rule, not accumulated per attempt.

`prepare-native-pass` provides safe packing defaults for ordinary translation, rewrite, and review tasks. These defaults may be overridden by positive explicit bounds; actual packing must also account for focused context, expected output, transport, and host/model capacity. Estimates size scheduling material and are not provider billing claims. Review splitting operates on failed targets and preserves successful rows and cumulative usage; no hidden effective depth or amplification limit may contradict the manifest's serialized depth. A single oversized target remains isolated.

Workers write result files using the exact task/result paths returned by `native-pass-step`. On the next call, it validates every result against the exclusive task order before writing any shard and returns aggregate counts only. Merge with the same manifest so stale or unrelated shards are ignored. `import-native-wave`, the one-shard import, and unbound merge remain compatibility paths, not model-selected recovery routes.

Legacy implicit task-sizing settings receive current safe packing defaults during migration. Explicit positive `--max-records`, `--min-records`, `--max-material-tokens`, and `--max-workers` values remain authoritative, subject to host/model capacity and resource-safe execution.

## Large-inventory startup budget

At 10,000 or more records, startup is a bounded linear phase. The primary and independent workers each enumerate the source once; each changed inventory generation gets one ordered core comparison. If that comparison is incomplete, status emits an `inventory_reconciler` task bound to the cached pair and current gap vector. A lower gap score requires another bounded gap-only generation; it does not authorize a rescan or a terminal failure report. Do not add another full extraction, whole-tree rescan, per-record agent, proof file, or speculative tool before context.

An unchanged verified inventory uses its saved path/size/mtime binding instead of reparsing the complete inventory on every status, translation, review, or resume command. JSONL input is streamed, and one command reuses the same in-memory segments when translation input and context inventory are the same file. A changed binding falls back to full semantic fingerprint verification.

Segment loading classifies known input fields once per file and only reconstructs records when neighbor enrichment changes their context. Streaming inventory hashes reuse one canonical JSON encoder per sequence; their digest remains identical to hashing the equivalent complete JSON array.

`native-pass-step` validates only tasks in the wave it is about to dispatch or import. It stores imported-wave counters in its control checkpoint and does not reopen every pending task or old shard at each step. Final merge still validates the complete binding once. Repeated discovery, unchanged inventory verification, and all-task validation are not startup progress and must not postpone the first semantic wave.

When native usage telemetry is available, the bound relay/provider adapter records aggregate input, cached-input, output, reasoning, event, and worker counts in the core-owned private ledger or sealed receipt. Root reads only that metadata. `input_tokens_known=false` and `telemetry_status=unmeasured` mean that a displayed numeric zero is only the legacy accumulator default, never evidence of zero use; `estimated_material_tokens_*` is workload size, not billed usage. Missing telemetry is unknown; never reconstruct it from `.codex/sessions`, chat transcripts, agent trees, nested-worker status/output, semantic-worker logs, or terminal previews. Per-worker accounting must be emitted at the event boundary. A high cached-input ratio diagnoses replayed history but does not authorize ending an incomplete job or inventing a turn limit. Keep later semantic work in fresh no-history workers and continue through the deterministic checkpoint.

## API fallback input budget

One translation, judgment, or rewrite request contains one target plus a focused projection of the already prepared context. Never attach a complete context pack, exchange bundle, inventory, prior result set, or application plan to `Segment.metadata`; typed context is supplied separately and only target-relevant glossary, relationship, feature, screen, knowledge, and reference evidence is projected. Stable job-wide fields precede target-specific fields so a compatible provider may reuse a common prefix.

Provider-specific per-request preflight limits are configured safety defaults, distinct from user-set whole-job ceilings. An oversized request remains pending before transmission; do not shrink task scope or preserve source to evade the bound. Do not describe an optional host capability or a preset default as user permission, or as a universal fixed limit.

Record provider-reported total input, cached input, cache-miss input, output, and reasoning usage when available. A cache hit is useful telemetry, not permission to send redundant context. Do not create a job-local API loop that bypasses the configured adapter and these limits.

Translation fallback uses the configured adapter's per-request safeguards. Optional user-set whole-job ceilings and usage accounting apply only to the dimensions the user actually chose; no baseline is required for an unbounded dimension. Preserve completed rows, pending work, and uncertain usage evidence across interruption. Partial imports do not block independent eligible bundles, while a repair-needed or unattempted target is not evidence that semantic routes were exhausted.

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
