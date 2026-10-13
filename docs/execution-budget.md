# Execution and validation budget

This contract prevents a localization job from expanding into speculative infrastructure. The objective is the requested playable patch, not the largest safety or compatibility system an agent can imagine.

## Scope freeze

At the start of each user request, write one active deliverable in the private workspace state. Work required to produce that deliverable is in scope. Optional migration systems, rollback archives, universal installers, compatibility matrices, broad refactors, public SDK promotion, and repository publication are separate deliverables and require an explicit request or a demonstrated blocker.

Build the minimum working artifact first. If a simpler design satisfies the stated acceptance criteria, prefer it over a more general design. Do not preserve an obsolete implementation merely to support an update path the user did not request.

## Effort ladder

Use the lowest sufficient level:

1. **Inspect:** batch the necessary reads once and select the existing adapter or private implementation.
2. **Build:** produce one new artifact without mutating the source root.
3. **Focused validation:** validate changed data, package structure, manifests, placeholders, and hashes once.
4. **Synthetic lifecycle:** for an installer change, run one isolated install, update, verify, and restore lifecycle against synthetic files. Include one declared original replacement only when that behavior changed.
5. **Real runtime validation:** launch or install into a real game only after the user explicitly requests that exact external action and the matching one-shot approval is recorded. An offline limitation or concrete runtime failure is a reason to ask, not permission to act.
6. **Broad regression:** run the repository suite and privacy scan before a repository commit or SDK publication, not after every private artifact edit.

Stop when the required level passes. Broaden or repeat validation only after input changes, a failed check, or an unresolved concrete risk.

## Retry and loop limits

- Do not repeat a successful command when its input fingerprint is unchanged.
- After the same operation fails twice for the same cause, stop repeating it. Diagnose once, simplify the design or choose a different route, and record the categorical blocker.
- Poll a worker only when new progress is expected. Prefer a bounded wait over frequent status checks.
- Do not create a second verifier for a property already covered by a deterministic validator.
- Do not retranslate, re-review, recompile, or repackage unchanged inputs merely for reassurance.

## Delegation budget

Use subagents when work divides into independent, non-overlapping bulk translation or review shards, or when parallel research materially shortens a blocking discovery phase. Keep simple packaging, a single script, a small bug fix, documentation, state inspection, and deterministic validation in the current agent.

Every delegated worker must have an exclusive output and a clear completion envelope. Do not delegate a duplicate review of a deterministic pass merely to increase confidence.

For native bulk localization, create stable exclusive ID shards once with `prepare-native-pass`. A worker may process many IDs sequentially, but each target retains its own candidate and refusal status. Keep only the available worker slots active, use bounded waits, and dispatch the next shard when a slot opens. Do not launch one subagent per target or poll every worker repeatedly.

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
