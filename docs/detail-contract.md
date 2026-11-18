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

New context tasks bind a `worker_execution` contract separately from the relay's lifecycle commands. Slots use its complete claim/commit argv with their assigned slot and owned artifact paths; only the fresh finalizer uses its finalize argv. Normal-host argv binds the executing Python and packaged core entry directly, so neither a `game-l10n` PATH lookup nor access to the relay's private launch ledger is needed. Use the declared execution tool and replace only declared whole arguments. Operational blockers stay in the bound artifact/receipt flow, never in agent messages; generic host cooperation instructions do not override this boundary.

Before context chunks are assigned, core code writes and hashes a private `parr0t.context-preflight.v1` sidecar. It uses structural metadata only: UI and buttons do not skip context unless an engine adapter supplied the exact positive static authority defined in `segment-metadata.md`. Dynamic, narrative, knowledge, terminology, quest/tutorial, logic/storage, strong-review, dependency-bearing, and uncertain UI stays in semantic analysis. The final seal recomputes the sidecar from the bound inventory and rejects a changed or invented plan.

Existing summaries are reusable outputs, not self-authenticating cache entries. A fragment may be skipped only when its own source digest, ordered evidence IDs, preflight policy version, mode, prompt contract, and nested schema still match. A reduced chunk ceiling repartitions only unfinished records; it does not invalidate a completed matching fragment. An unbound or stale summary may inform a new semantic pass but cannot mark records complete. Legacy completed context packs remain readable and are not rewritten merely to adopt this ledger.

Context chunk ceilings measure the complete serialized request rows, including IDs, source, and metadata—not source text alone. Current context state uses `parr0t.context-state.v2`; removing its new attestation fields cannot downgrade it into legacy reuse. A genuinely legacy schema-less state remains reusable only when its bound source, output, mode, domains, and execution proof still validate.

Each v7 task row binds the complete segment payload and the current source/target-language policy under `parr0t.native-task-payload.v1`. The default target is Korean; a different target requires a core-recorded explicit user policy. A data-plane worker consumes one immutable task snapshot and must not open the full ordered `source_artifact`. "Once" binds the worker to that single snapshot; it does not require one shell-output frame. When host transport would truncate the task, the worker may consume deterministic ordered byte or line ranges from the same unchanged file, verifying its digest, complete record count, order, and language binding before the one semantic call. The full source exists only for framework-side validation.

Every new semantic task row also carries its complete `parr0t.content-source-policy.v1`. Default-deny rows use only the selected model and bound local payload/context. A named `parr0t.content-source-grant.v1` may allow only its exact scope, task kind, route, and sources; retries and core-owned review children retain the parent's authorization and cannot widen it. Results persist `parr0t.content-provenance.v1`; undeclared external sources or a mismatched model/policy digest fail import. Local inventory payloads carry a non-semantic local-inspection policy so source discovery remains usable without granting candidate generation or network content.

## Permitted detail

A task may define only:

- exclusive target IDs and existing private input/context paths;
- one exclusive output path and its fixed response schema;
- stage-specific acceptance checks and categorical failure codes;
- task bounds from manifest defaults or positive explicit settings, subject to host/model capacity and resource-safe sharding;
- the checkpoint to reuse and the exact stop condition.

Do not copy core contracts into a job file. Resolve the manifest fingerprint and routed contract digest instead. Historical incidents, previous model arguments, provider refusal prose, game text, speculative recovery designs, and title-specific analysis stay in private audit data; generally reusable failure guidance belongs in the neutral FAQ.

## Batched semantic execution

Translation and rewrite tasks are one semantic batch. Load shared context once, evaluate all bound targets in one request/worker response, and write one status per target in the ordered result. Do not create a prompt file or model/API call per record.

Review task packing has safe configurable defaults, not universal hard ceilings. Honor positive explicit ID/token/worker bounds and host/model capacity; estimates size scheduling material, not billed usage. Each fresh worker makes one semantic call and ends. Core imports valid results independently, locks successes, and retains only failed or missing targets for continuation. Splitting follows the manifest's serialized depth and configured optimization policy; do not impose hidden effective-depth or amplification clamps. Repair-needed or unattempted targets are not exhaustion.

If a provider protocol can accept only one target, `prepare-native-pass` must create one-target tasks before dispatch. A worker must not silently turn a multi-target task back into a serial loop. Outside manifests generated by the core-owned review split, repartitioning requires a newly generated manifest, not a job-local exception.

## Bulk budget gate

`execution.bulk_budget_gate_records` is retired and removed when loading older configuration. No record-count pilot gate or implicit quota remains. Eligible work continues from checkpoints within independently optional explicit user ceilings; unknown usage is not zero.

User time, token, and cost ceilings are independent optional hard limits. Apply only dimensions the user selected; a limit preserves completed results and pending work without requiring unrelated ceilings. Unknown metered usage remains unknown.

`game-l10n native-pass-step` is the authority for route/task selection and progress, but partial imports are valid: completed target results survive while missing targets remain pending, and independent cohorts or deterministic repairs may proceed. A no-progress observation is not a universal stop barrier.

## Conflict examples

Reject a detail contract that introduces per-record semantic calls within a batch, raises call/token/time/cost limits, reopens unchanged successes, adds duplicate verification, changes provider roles or pipeline order, exposes text to the control plane, grants runtime/publication effects, or converts a refusal into exclusion. Keep the valid checkpoint and regenerate only the conflicting detail contract.
