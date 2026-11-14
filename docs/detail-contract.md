# Task-detail contract governance

This contract governs every job-, stage-, coordinator-, wave-, task-, or worker-specific instruction created during a Parr0t run. A detail contract is supplemental-only: it supplies bindings, never a new policy layer.

## Authority order

Apply instructions in this order: the user's current request and explicit approvals, repository `AGENTS.md` and routed core contracts, selected SDK contract, workspace `AGENTS.md`, generated `parr0t.detail-contract.v1`, then task prose. A lower layer may narrow its assigned targets, paths, outputs, checks, deadline, or budget. It must not weaken, replace, reinterpret, or postpone a higher layer.

The native v7 task manifest is the only executable detail contract for new work; legacy v6 manifests remain valid only for continuing an existing checkpoint. Its `worker_contract`, `detail_contract`, `execution_contract`, and `contract_fingerprint` are one immutable unit. Reject a changed or conflicting unit before dispatch with `E_DETAIL_CONTRACT_CONFLICT`. Free-form Markdown may be a short operator note, but it is not passed to a worker and has no execution authority.

Preparation uses the same immutable-contract validator as stepping. Identical preparation of a valid historical v7 checkpoint preserves its original contract fingerprint, including supported omission of `result_contract`, the older detail contract without the runtime-repair action, and absence of an execution namespace. Reuse must not replace completed shards or reset usage; a changed present contract is rejected rather than overwritten.

New failed-only review children inherit the parent's exact provider/attempt binding. Historical children created without that optional route binding remain discoverable through their original root command; lookup does not relabel their contracts or recorded launches.

The root may pass only the core-generated `parr0t.native-wave-relay.v1` path to a fresh isolated relay. It must not copy the bound task paths into direct semantic-worker prompts. The relay is restricted to exact dispatch and categorical collection; it cannot add instructions, inspect semantic payloads, retry, split, select a provider, or create a replacement format. A semantic worker's free-form response contaminates only that disposable relay unless the relay forwards it; forwarding is a control-plane incident.

An explicitly selected `native-host-api` transport executes that same bound relay through the deterministic code broker in `native-host-api.md`. It requires a real independent native worker API and a route/model-bound live canary. The broker consumes host replies privately and returns the existing core receipt; it never makes the root a semantic worker's direct message recipient. Default collaboration dispatch and existing manifests retain their contracts.

Discovery, inventory, reconciliation, context, and requested UI-image work use the equivalent `parr0t.data-plane-relay.v4` contract. The disposable relay may read only its safe relay metadata, including a core-bound worker route/model family and exact execution plan; it must not open the private task or read or relay a child's final response. Context workers use only the task's exact `claim-context-chunk` and `commit-context-fragment` commands. The finalizer uses only `finalize-context-fragments`; a provider-created context pack or envelope is never proof of completed chunks. A context relay records every required slot start/end and the mandatory finalizer start/end through the core lifecycle command. A plan with zero slots is a valid finalizer-only resume and must not create semantic workers. It may not omit slots, seal before the finalizer, or invent an elapsed-time timeout when the plan binds none. At terminal relay state it invokes `seal-data-plane-task --worker-ended` exactly once and returns only the generated receipt path. The core seals valid output or records the operational failure with actual fragment counts, including execution incompleteness separately from semantic failure, and exposes a fresh attempt. Context attempts rotate the configured native route and reduce their private chunk ceiling by core policy. The relay must not turn a missing or malformed result into a content judgment, refusal explanation, parent-facing summary, or whole-job stop.

Before context chunks are assigned, core code writes and hashes a private `parr0t.context-preflight.v1` sidecar. It uses structural metadata only: UI and buttons do not skip context unless an engine adapter supplied the exact positive static authority defined in `segment-metadata.md`. Dynamic, narrative, knowledge, terminology, quest/tutorial, logic/storage, strong-review, dependency-bearing, and uncertain UI stays in semantic analysis. The final seal recomputes the sidecar from the bound inventory and rejects a changed or invented plan.

Existing summaries are reusable outputs, not self-authenticating cache entries. A fragment may be skipped only when its own source digest, ordered evidence IDs, preflight policy version, mode, prompt contract, and nested schema still match. A reduced chunk ceiling repartitions only unfinished records; it does not invalidate a completed matching fragment. An unbound or stale summary may inform a new semantic pass but cannot mark records complete. Legacy completed context packs remain readable and are not rewritten merely to adopt this ledger.

Context chunk ceilings measure the complete serialized request rows, including IDs, source, and metadata—not source text alone. Current context state uses `parr0t.context-state.v2`; removing its new attestation fields cannot downgrade it into legacy reuse. A genuinely legacy schema-less state remains reusable only when its bound source, output, mode, domains, and execution proof still validate.

Each v7 task row binds the complete segment payload and the current source/target-language policy under `parr0t.native-task-payload.v1`. The default target is Korean; a different target requires a core-recorded explicit user policy. A data-plane worker consumes one immutable task snapshot and must not open the full ordered `source_artifact`. "Once" binds the worker to that single snapshot; it does not require one shell-output frame. When host transport would truncate the task, the worker may consume deterministic ordered byte or line ranges from the same unchanged file, verifying its digest, complete record count, order, and language binding before the one semantic call. The full source exists only for framework-side validation.

## Permitted detail

A task may define only:

- exclusive target IDs and existing private input/context paths;
- one exclusive output path and its fixed response schema;
- stage-specific acceptance checks and categorical failure codes;
- limits equal to or stricter than the parent manifest;
- the checkpoint to reuse and the exact stop condition.

Do not copy core contracts into a job file. Resolve the manifest fingerprint and routed contract digest instead. Historical incidents, previous model arguments, provider refusal prose, game text, speculative recovery designs, and title-specific analysis stay in private audit data; generally reusable failure guidance belongs in the neutral FAQ.

## Batched semantic execution

Translation and rewrite tasks are one semantic batch. Load shared context once, evaluate all bound targets in one request/worker response, and write one status per target in the ordered result. Do not create a prompt file or model/API call per record.

A task prepared with `--pass-kind review` has hard ceilings of 2,048 candidates and 262,144 estimated material tokens, but a new v7 chain starts at no more than 256 candidates and 65,536 estimated material tokens per task. Each fresh worker makes one semantic call and ends. After import, `native-pass-step` locks every successful row and prepares new tasks containing only the refused/errored subset at half the prior task bound. Existing v7 manifests retain their serialized depth-11 compatibility field, but the current core stops at effective depth 3 or before cumulative attempts exceed 2.0 times the original set. A one-row failed subset or one-row task bound also ends splitting and routes remaining failures to the configured review fallback. This core-owned chain is the only permitted repartitioning and never becomes an in-worker per-record loop.

If a provider protocol can accept only one target, `prepare-native-pass` must create one-target tasks before dispatch. A worker must not silently turn a multi-target task back into a serial loop. Outside manifests generated by the core-owned review split, repartitioning requires a newly generated manifest, not a job-local exception.

## Bulk budget gate

`execution.bulk_budget_gate_records` (default 4,096 records) is retained for manifest compatibility and reporting; record count alone never activates a pilot stop or creates a limit. Without explicit user limits, all waves remain dispatchable and continue from checkpoints until completion or a genuine actionable blocker. A legacy manifest that contains a framework-inferred unapproved pilot lock must resume its pending waves without regeneration or loss of completed work.

When the user does set limits, they apply at any inventory size. The manifest requires positive ceilings for elapsed minutes and aggregate input tokens, plus either a provider-cost ceiling or an explicit declaration that the selected route is unmetered. Those limits cover the whole pass, not each worker. Reaching one stops new dispatch, preserves completed shards and the unchanged pending queue, and returns the smallest required decision. It never authorizes dropping targets or accepting unreviewed candidates.

`game-l10n native-pass-step` is the only authority that selects or advances a wave. A job note, model plan, or direct manifest reading cannot substitute for its returned action. It imports a complete active wave before returning the next dispatch, preserves checkpoints when an explicit budget is later bound, and checkpoints repeated unchanged observations instead of inventing recovery.

## Conflict examples

Reject a detail contract that introduces per-record semantic calls within a batch, raises call/token/time/cost limits, reopens unchanged successes, adds duplicate verification, changes provider roles or pipeline order, exposes text to the control plane, grants runtime/publication effects, or converts a refusal into exclusion. Keep the valid checkpoint and regenerate only the conflicting detail contract.
