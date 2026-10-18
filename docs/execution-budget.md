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

## Exact correction budget

When the user supplies the final replacement wording for a typo or small translation fix, use `docs/minimal-corrections.md`. Resolve its stable IDs, run `game-l10n apply-user-correction`, repack the owning payload once, and verify the package once. The expected model-call and context-analysis budget is zero. Do not delegate the correction, reopen full source discovery, re-read every translation, rehash the game tree, or create a job-specific apply/validation script. If reverse dependencies exist, review only those IDs; if mapping is ambiguous, perform one narrow static lookup before changing anything.

## Retry and loop limits

- Do not repeat a successful command when its input fingerprint is unchanged.
- After the same operation fails twice for the same cause, stop repeating it. Diagnose once, simplify the design or choose a different route, and record the categorical blocker.
- Poll a worker only when new progress is expected. Prefer a bounded wait over frequent status checks.
- Do not create a second verifier for a property already covered by a deterministic validator.
- Do not retranslate, re-review, recompile, or repackage unchanged inputs merely for reassurance.

## Delegation budget

Use subagents when work divides into independent, non-overlapping bulk translation or review shards, or when parallel research materially shortens a blocking discovery phase. Keep simple packaging, a single script, a small bug fix, documentation, state inspection, and deterministic validation in the current agent.

Every delegated worker must have an exclusive output and a clear completion envelope. Do not delegate a duplicate review of a deterministic pass merely to increase confidence.

For native bulk localization, create stable exclusive ID tasks once with `prepare-native-pass`. Each target retains its own candidate and refusal status. Keep only the available worker slots active, use bounded waits, and dispatch the next task when a slot opens. After every task in a wave finishes, run `import-native-wave` once instead of importing each shard in separate root turns. Do not launch one subagent per target or poll every worker repeatedly.

## Codex control-turn budget

Native orchestration is root-direct. The root control plane prepares a deterministic queue and dispatches each task straight to one fresh worker. A model session must never act as a scheduler, recovery controller, owner/lease manager, or intermediary between the root and a worker. Native workers must not delegate. The maximum agent depth is one below the root.

The v5 task manifest is the executable budget and detail contract. One wave uses at most `routing.max_workers` direct workers, 12 root model calls, and one root progress message. One root turn stops at 64 model calls or 5,000,000 reported input tokens, whichever comes first. A worker stops after its one task, at 16 control/tool turns and one semantic batch call, with no parent-facing progress messages or follow-up task. Reaching a limit checkpoints unchanged queue state and returns control; it must not trigger a summary, compaction, replacement controller, nested worker, or automatic continuation.

Read each required contract set once per control-plane generation. `game-l10n status` returns `contract_set.digest`; reread only when that digest or the generation changes. A context state bound to the current independently verified inventory fingerprint is sufficient for status; do not parse the same ordered inventory again merely to reproduce its segment digest. Tool output and worker responses stay in artifacts. The root receives only fixed-size metadata and must not turn each command, wait, shard completion, or unchanged observation into another narrated model turn.

## Executable detail-contract budget

Follow `docs/detail-contract.md`. The native v5 manifest is the only executable job/task detail contract. Do not promote a generated Markdown plan, incident ledger, repair diary, or coordinator note into worker policy. Such a note cannot raise a limit, alter provider/pipeline/isolation/authorization rules, require repeated proof, or convert a prepared batch into per-record semantic calls.

One multi-target task permits one semantic model/API batch call and one ordered batched result containing a status for every bound target. Load shared context once. Do not generate one prompt artifact per record, run an inner model/API loop, or count a directory of per-record calls as one task. If a provider accepts only one target, repartition before dispatch so the manifest makes that cost visible.

For a pass at or above `execution.bulk_budget_gate_records` (default 4,096), wave 1 is a measurement pilot. A later wave requires user-approved whole-pass ceilings for elapsed minutes and aggregate input tokens plus either metered provider cost or an unmetered declaration. `import-native-wave` rejects later waves without that binding. Use the pilot telemetry to propose the smallest realistic cap; never infer approval from the original translation request or sunk work.

## Residual convergence gate

At the end of a complete translation, judgment, or rewrite pass, freeze the remaining target IDs, candidate digests, and existing context fingerprints. When the remainder is no larger than `execution.residual_direct_records` (default `32`), `prepare-native-pass` must produce one direct residual task for one fresh no-history worker. This is a terminal convergence path, not a reason to design another workflow.

Do not create a job-local scheduler, controller, lease/owner database, liveness probe, memoization layer, per-target proof files, or supplemental context pack for this residual task. Do not repartition it, reopen passed targets, or repeat full aggregation. Missing evidence may be added only when an existing deterministic validator names the exact absent artifact; otherwise use the already attested context.

Aggregate results once when the worker finishes. A capacity failure, missing native slot, or worker startup failure ends that attempt, records the unchanged task in resumable operational state, and returns control. It must not auto-restart the root turn or generate another controller. Report progress only at a completed wave boundary or actionable blocker, not after individual shards. One unchanged observation after a bounded wait, or two control actions that do not change a target state, is enough to stop polling and report the categorical blocker. Semantic rewrites remain bounded by `routing.review_max_rounds`.

## Native model context budget

Native workers are data-plane executors, not long-lived chats. Start every translation or review task in a fresh worker with no inherited parent conversation (`fork_turns="none"` or the host equivalent). Give it only the immutable worker contract and private workspace/source/context/task paths. It reads and writes files directly and returns only task ID, counts, categorical states, issue codes, and artifact paths. Never paste source text, candidates, inventories, code listings, or command output into agent messages.

One worker processes one task and then ends. Do not send another shard through a follow-up turn, even when the worker is idle. Long-lived workers accumulate every previous target and tool result; replaying that history on each next target makes input usage grow superlinearly while adding no translation evidence.

`prepare-native-pass` v5 defaults to at most 64 IDs and 24,000 estimated private material tokens per task, with a preferred eight-record minimum when two adjacent bounded tasks can be balanced. Its manifest also carries the immutable detail, worker, and root-direct execution contracts: one semantic batch per task, no per-record inner model loop, no model controller, no worker delegation, one aggregation per wave, bounded root/worker calls, and no automatic root-turn restart after capacity failure. The estimate sizes scheduling material only and is not a provider billing claim. Lower either hard maximum when focused context, source records, or expected target output is large. A single oversized target remains isolated in its own task. Reuse the cached task manifest only when the source fingerprint, all sizing/worker/budget bounds, contract fingerprint, and manifest version match.

Workers write result files using their task filenames in a private wave result directory. `import-native-wave` validates every result against the exclusive task order before writing any shard and prints aggregate counts and private artifact paths only. Merge with the same manifest so stale or unrelated shards are ignored. The legacy one-shard import and unbound merge remain compatibility paths, not the preferred bulk route.

An older workspace that still has either the former implicit 256-record setting or the v3 32/12,000 defaults and no v4 policy marker automatically receives the new v4 bounds. Explicit `--max-records`, `--min-records`, `--max-material-tokens`, and `--max-workers` values remain authoritative.

When native usage telemetry is available, record only aggregate input, cached-input, output, reasoning, event, and worker counts in the private workspace. Count root and every descendant separately. A high cached-input ratio does not justify continued reuse: cached input is still repeated input and is evidence that old conversation is being replayed. End the bounded turn rather than adding summaries or compactions to an already long chat.

## API fallback input budget

One translation, judgment, or rewrite request contains one target plus a focused projection of the already prepared context. Never attach a complete context pack, exchange bundle, inventory, prior result set, or application plan to `Segment.metadata`; typed context is supplied separately and only target-relevant glossary, relationship, feature, screen, knowledge, and reference evidence is projected. Stable job-wide fields precede target-specific fields so a compatible provider may reuse a common prefix.

The DeepSeek presets cap the combined system-and-user character count at 24,000 for each translation, compact judgment, and rewrite request. This is a deterministic preflight limit, not a token estimate. It applies at runtime to older preset workspaces even when their stored configuration predates the field. Oversized input returns non-retryable `budget_exhausted` before any network call, keeps that exact target pending, and must be fixed by removing duplicated context or explicitly configuring a justified larger bound; it must not trigger scope reduction or source loss. Normal retry policy remains unchanged for actual retryable provider failures.

Record provider-reported total input, cached input, cache-miss input, output, and reasoning usage when available. A cache hit is useful telemetry, not permission to send redundant context. Do not create a job-local API loop that bypasses the configured adapter and these limits.

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

## Completion report

Report the artifact, the checks that materially establish acceptance, remaining user validation, and any concrete limitation. Do not present repeated checks, polling events, or speculative safeguards as progress.
