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

For native bulk localization, create stable exclusive ID tasks once with `prepare-native-pass`. Each target retains its own candidate and refusal status. Keep only the available worker slots active, use bounded waits, and dispatch the next task when a slot opens. Do not launch one subagent per target or poll every worker repeatedly.

## Native model context budget

Native workers are data-plane executors, not long-lived chats. Start every translation or review task in a fresh worker with no inherited parent conversation (`fork_turns="none"` or the host equivalent). Give it only the immutable worker contract and private workspace/source/context/task paths. It reads and writes files directly and returns only task ID, counts, categorical states, issue codes, and artifact paths. Never paste source text, candidates, inventories, code listings, or command output into agent messages.

One worker processes one task and then ends. Do not send another shard through a follow-up turn, even when the worker is idle. Long-lived workers accumulate every previous target and tool result; replaying that history on each next target makes input usage grow superlinearly while adding no translation evidence.

`prepare-native-pass` v2 defaults to at most 32 IDs and 12,000 estimated private material tokens per task. The estimate sizes scheduling material only and is not a provider billing claim. Lower either bound when focused context, source records, or expected target output is large. A single oversized target remains isolated in its own task. Reuse the cached task manifest only when the source fingerprint and both bounds match.

An older workspace that still has the former implicit 256-record value and no material-token setting is treated as a legacy default and automatically receives the new 32/12,000 bounds. An explicit `--max-records` or `--max-material-tokens` remains authoritative.

When native usage telemetry is available, record only aggregate input, cached-input, output, reasoning, event, and worker counts in the private workspace. A high cached-input ratio does not justify continued worker reuse: it is evidence that old conversation is being replayed. Rotate workers rather than adding summaries or compactions to an already long data-plane chat.

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
