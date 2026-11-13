# Control-plane isolation contract

## Deterministic review router (normative)

Review orchestration is a finite-state router, not a semantic reviewer or an
autonomous recovery agent. The control plane may use only validated status,
native manifests, control envelopes, aggregate usage counters, and explicit
user approval. It must not inspect source or translated text, infer meaning,
choose a model, invent a review plan, or reinterpret a worker result.

The only review-control actions are: read validated status; prepare the
repository-defined manifest; run `game-l10n native-pass-step`; dispatch the
single exact `dispatch_isolated_wave_relay` artifact it returns to one fresh relay; wait once for that active wave; validate a
text-free control envelope; aggregate a completed pass; route an exact
categorical status; record aggregate usage; or checkpoint and stop.

Anything else is denied. In particular, the control plane must not create an
ad-hoc manifest, receipt, queue, or result format; clone or rebind the SDK;
move files to repair tools; change batch shape outside the core split or change models; repeat an unchanged
success; or clear a halted/cost-audit state on its own. User approval is an
input to the repository contract, not permission to improvise missing budget
or execution fields. Resuming additionally requires a regenerated, validated
manifest. If a required state is missing, contradictory, or not
recognized, the sole transition is `checkpoint_and_stop`.

That unknown-state rule is scoped to an active native review manifest and its
budget/control state. It does not turn a sealed discovery, inventory, or
context failure into a terminal job state. Those stages return to
`game-l10n status`, which emits the exact retry or repair relay; the parent
must not broaden `checkpoint_and_stop` beyond the review router.
Perceived turn length, elapsed wall time, context pressure, repeated bounded
waits, or a model's estimate of a nearby limit is not an unknown state or a
budget signal. A root call/token ceiling exists only when the user requested a
concrete value and the validated manifest records that approval; otherwise the
control plane must continue until a terminal state or actionable blocker.
The control plane must not ask the user to invent such a ceiling, including as
a startup, large-inventory, translation, or review prerequisite. Absence is the
complete policy: no whole-job ceiling applies. The only automatic usage guard
is the configured per-request input preflight inside an external DeepSeek
fallback adapter, and it is irrelevant until that fallback route is reached.

Every native review manifest embeds `parr0t.review-control-contract.v1` inside
its immutable detail contract. Changing or removing it invalidates the
manifest, so lower-priority notes cannot silently restore control-plane
discretion.

The fixed review order is deterministic validation, complete low-cost triage,
selective strong review, flagged-only rewrite, then failure-only fallback.
Strong-review membership comes only from private triage status, a manifest
high-risk flag, or the stable deterministic sample. The parent receives IDs
and counts only; it cannot inspect text, waive triage, or promote strong review
to an unbudgeted universal pass.

The top-level Parr0t agent schedules work and advances queues. It does not inspect or judge game text. This boundary applies from preliminary discovery through final validation; it is not limited to translation and review providers. Arbitrary subprocess output remains in private workspace logs; a parent-facing tool failure is categorical only.

Translation retry routing is deterministic too. The core exposes only the bound provider route, attempt ordinal, pending count, locked-success count, and workspace-relative artifact paths. The parent dispatches the reported route and does not choose, skip, extend, or shorten the sequence. In the recommended preset, DeepSeek translation fallback is ineligible until the checkpoint proves five Luna retries and three Sol retries were exhausted for the remaining targets, or a categorical authentication/quota condition made that exact route unavailable. Provider prose is never evidence for that transition.

Deterministic native-wave collection is part of the private data plane. `native-pass-step` is the normative scheduler: it persists text-free progress and aggregate usage, enforces unchanged-observation stops and only manifest-recorded user-approved control-turn limits, imports a complete active wave atomically, and emits exactly one next action. It must not synthesize a root limit from defaults, elapsed time, usage telemetry, or model judgment. For review refusal/error only, it may lock successes and apply the manifest's bounded failed-subset rule. Its parent-facing dispatch contains only one relay-artifact path and counts. The parent must not open that artifact, select another wave, call legacy import as recovery, or open shards.

The top-level control plane is never the direct parent of a semantic worker. For each authorized wave, `native-pass-step` writes one metadata-only `parr0t.native-wave-relay.v1` dispatch file. The root gives only that path to one fresh no-history `isolated_wave_relay`. The relay may start only the exact bound workers, once; it may not open their task payloads, plan, retry, split, change providers, alter routes, or interpret failures. Semantic workers remain non-delegating and write their normal private artifacts. Raw child responses terminate at the disposable relay and must never be forwarded to the root. After the bounded wait and after the relay lifecycle has ended, the relay runs `game-l10n seal-native-wave-relay --workspace <workspace> --dispatch <relay-path> --relay-ended` and returns only the resulting workspace-relative `parr0t.native-wave-relay-receipt.v1` path. The deterministic seal derives the missing bound tasks itself, records only those tasks as operational-error rows, and never overwrites a result that won the race. The complete wave can then be imported and its failed targets enter the configured resumable queue. Repeated `--ended-task` remains a legacy/manual compatibility path, not the normal relay contract; omitting both terminal forms is invalid at the CLI and must not create an incomplete receipt that the root polls. For v7 review manifests, no worker split proof is accepted or required: after a wave is imported, deterministic core code locks every structurally valid success and creates the next smaller manifest from failed target IDs only. Legacy v6 manifests retain their original worker-proof validation solely so an existing checkpoint can finish. The seal never semantically interprets result rows. The relay must not invent a supplemental lifecycle envelope. If a child emits prose or private content, quarantine that relay, preserve valid completed result files, and finish through the same categorical terminal seal.

For a launch-bound native wave, an absent terminal receipt means `wait_for_active_wave`, even after repeated unchanged observations. The root continues bounded waits on its direct relay without ending workers or retrying their targets. A categorical relay end must be sealed through core; an explicit user-bound budget still applies. Wait duration is not evidence of a timeout, refusal, or exhausted attempt.

## Native host and launch binding

Before dispatch in each control generation, run `native-runtime --workspace <workspace> --host codex|omp`. This records metadata only. Codex defaults bind `native-luna` to `gpt-6-luna` and `native-sol` to `gpt-6.1-sol`; OMP uses the corresponding `openai-codex/` IDs. An operator-selected exact host mapping is passed through repeated `--model route=exact-id`; a proxy's endpoint choice does not authorize another semantic provider. Availability is checked against the actual host tool/model surface, never guessed from a model name. No credentials or global host settings are changed.

Every newly dispatched relay carries `launch_contract`. Before any worker starts, the relay verifies that it can dispatch children and use the exact model. It records `record-native-launch --workspace <workspace> --relay <relay-path> --event capability-available`. Missing nested dispatch records `capability-unavailable`; a missing/unauthorized model records `model-unavailable`. It then runs its normal terminal seal and returns only the receipt path. Never emulate the missing tool, ask to remove the relay, change model, or synthesize per-target failures.

For each task in `unstarted_tasks`, pass `launch_contract.model` explicitly to the spawn call (`model` in Codex, each `tasks[].model` in OMP) with no inherited history. After a successful spawn, record `--event worker-started --task <exact-binding> --worker-id <returned-id> --model <requested-exact-id>`. Include `--observed-model <id>` when the host supplies model metadata. Ordinary data-plane tasks use `worker-0`; context tasks use the bound `worker-<slot>` and `finalizer` keys. Context start/end lifecycle commands still apply separately. Already recorded workers must not be spawned again.

Launcher records bind the explicit request and any host-reported model, not a worker-authored `provider` label. When the host omits actual-model telemetry, its identity remains unobserved; do not invent it or scrape session logs. A mismatching reported model blocks import even if the output labels itself as the requested provider. Preserve/quarantine rejected output; never relabel it.

Unstarted/model-mismatched waves return `repair_native_runtime`. After actual host repair and relay termination, `native-pass-step --resume-relay` reopens only that same wave; the next relay records capability again and starts only unstarted tasks. It preserves completed files, imported successes, route/attempt, and cumulative usage. It is not a whole-job terminal state or permission for repeated probes of an unchanged unavailable host. A legacy retry result containing launcher-generated `native-control` errors resumes those exact targets at the same ordinal. Successful targets lock immediately, while already attempted failures remain deferred until the operational subset finishes; they must not be reprocessed during transport recovery. Old checkpoints remain readable but are not retroactively certified as model-verified.

Retry-state summaries return `native_pass_output` and `native_pass_results`. Use these exact paths for the next pending subset: a transport recovery keeps the semantic ordinal but receives a distinct core-named output location, so an old completed error manifest cannot satisfy the new execution. `pending_count` describes that immediate subset; `deferred_failure_count` stays visible and `unresolved_count` includes both. Configured custom native routes may supply additional exact `native-<name>=<model-id>` bindings; the mapping does not change the preset's authorized route order or provider roles.

## Deterministic delivery selection

Packaging eligibility is computed by `materialize-delivery-candidates`; the control plane must not recreate this join in an ad hoc script. A translated and reviewed target is emitted as `reviewed`. A translated target whose review did not complete may be emitted only after the user's explicit inclusion request, and remains `accepted_unreviewed` with its review history intact. A translation failure is omitted from the result artifact so the selected SDK preserves the exact source. A placeholder, markup, control-token, script-integrity, or other deterministic integrity failure is also omitted and recorded in the metadata-only `parr0t.delivery-selection.v1` report. Review failure alone never converts an existing candidate into a translation failure, while an unsafe structure never becomes acceptable merely because semantic review passed.

The selected SDK may apply stricter engine-specific structural checks, such as line-slot counts or engine escape-code order. Such a check may only preserve the source or fail the build; it may not silently apply the unsafe candidate. The top-level plane receives counts, stable target IDs, categorical issue codes, and artifact paths only. The release checkpoint must retain `reviewed`, `accepted_unreviewed`, and `source_preserved` as distinct states so a later run can resume only the unfinished work.

## Entry gate

The boundary begins before preliminary discovery. The top-level agent may confirm that repository, workspace, and supplied source paths exist, but it must not inspect game-root contents. Existing-locale and engine/version discovery therefore run as a core-bound `source_probe`. When status exposes `data_plane_task`, run `prepare-data-plane-task`, give only its `parr0t.data-plane-relay.v4` path to one fresh no-history relay, and execute its single terminal seal command. Primary inventory, independent inventory, gap reconciliation, and complete typed context analysis use the same route. The existence of a core task means no extra user exception is needed; a model must not replace it with a manual assignment or the direct-provider `analyze-context` command.

Ordinary relays start exactly one non-delegating worker. A context relay is the sole exception: when its bound `max_workers` exceeds one, it starts every listed fresh semantic worker concurrently with its unique numeric slot before treating any worker as ended. A child obtains work only through `claim-context-chunk --task <task> --slot <slot>`, writes exactly one `parr0t.context-chunk-result.v1`, and submits it through `commit-context-fragment --task <task> --claim <claim> --result <result>` before claiming again. A claim may carry private `localization_inputs`: the locked project languages, genre and style guide, the glossary entries whose source occurs in the chunk, and the user character profiles of its speakers. The worker treats the glossary as authoritative, lets the profiles override inferred traits, and expresses register guidance in the target language's own terms. These inputs are part of the fragment binding, so a changed glossary, profile, or style guide re-analyzes the affected chunks. Core-owned claim directories make the lowest unfinished claim atomic; a free-form response, direct fragment, or directly written context pack has no completion authority. The relay records each categorical start/end through `record-data-plane-relay-event`; it does not infer success from that lifecycle. No elapsed-time or implicit worker timeout exists unless the core plan contains a positive timeout. After every required slot ends, one fresh finalizer runs `finalize-context-fragments --task <task>` between its recorded start/end events. That command alone validates every fragment, merges them in source order, and writes the shared context pack and envelope. When every chunk is already committed, the next plan has `max_workers=0`, no semantic slots, and an immediate finalizer-only lifecycle; completed work must not spawn replacement analyzers. The relay never opens the private task, fragments, or child responses and returns only the core-generated `parr0t.data-plane-receipt.v2` path. The v4 relay embeds the immutable contract, exact slot plan, core-bound route/model family, attempt, worker ceiling, lifecycle path, finalizer gate, and nonterminal failure flag; it never selects or replaces a route. Sealing recomputes progress from validated fragments, rejects an incomplete lifecycle or an ad-hoc final artifact, and categorical terminal sealing preserves actual completed/pending counts. The core preserves completed fragments, retries only unfinished chunks, restores the base chunk ceiling whenever a receipt proves semantic progress, and halves it only after a complete no-progress route cycle. `task_incomplete`, `malformed_response`, and `relay_execution_incomplete` schedule another bound attempt rather than ending the job. Legacy v1, v2, and v3 relays and v1 receipts remain sealable/readable for checkpoint compatibility.

The task fixes source roots, SDK binding, output artifact, generation-and-attempt-specific output envelope, counterpart-read prohibition, attempt number, and role/stage. A context task additionally hashes a `parr0t.context-work-manifest.v1` containing the exact source/preflight/mode/contract bindings and ordered complete request rows. Claims, results, and reusable `parr0t.context-fragment.v1` files must match that manifest. A prior attempt's envelope is immutable evidence and can never satisfy a later attempt. Sealing rejects a changed task, wrong role/stage, missing bound artifact, invalid inventory schema, discovery success without recorded SDK selection, reconciliation that did not run a newer core verification, reconciliation that violates append-only primary ID lineage, or a context pack that is not exactly reproduced from all bound fragments and then fails deterministic domain/evidence coverage. A global method-specific ID namespace mismatch routes to identity reconciliation; it is not authority to rewrite the primary IDs or discard inherited checkpoints. On context success the core—not the worker or parent—writes the bound context-state attestation.

## Control-plane observation boundary

The top-level agent may observe only core CLI metadata, a validated control-envelope path, a sealed relay-receipt path, and the categorical lifecycle of its direct relay. It may perform one bounded wait on that relay and then return to the core state machine. It must not inspect `.codex/sessions`, chat transcripts, agent trees, nested-worker status or output, semantic-worker logs, terminal previews of semantic work, or another session to reconstruct progress. It must not query a nested worker even when the worker is visible to the host.

Usage accounting comes only from the core-owned usage ledger or a sealed relay receipt populated at the event boundary. Missing telemetry is unknown, not permission to scrape host/session logs. The relay may observe only bound task lifecycle categories needed to seal an ended task and must never forward child output. Any forbidden detail that reaches root is a control-plane leak: do not interpret it; record the fixed incident and rotate.

A non-success receipt cannot be hidden by an empty or partial artifact at the expected path. Status routes that role back to a fresh bound attempt. Inventory workers use the parent-safe `sdk_identity_mismatch` code when selected and observed engine identities conflict; status then routes to the private source probe/SDK selection step instead of inviting the parent to diagnose engine files.

Every new workspace starts with `parr0t.control-plane-entry.v1` in `pending`. Existing workspaces without this marker are also treated as pending. `game-l10n status` then returns only `establish_text_free_control_plane` and the contracts that must be read. If the parent has not inspected or received game content, it establishes the gate with:

```text
game-l10n enter-control-plane \
  --workspace <workspace> \
  --contracts-read \
  --source-unopened
```

All ordinary CLI operations reject a pending gate with `E_CONTROL_PLANE_ENTRY_REQUIRED`. The assertion is invalid after any game-content exposure; record a leak and stop instead. Leak recording and fresh-session rotation reset the entry gate to pending so the replacement control plane must establish its own clean boundary.

## Data-plane stages

The following roles are data-plane workers:

- source and existing-locale discovery;
- primary and independent inventory;
- inventory gap reconciliation;
- context analysis;
- translation;
- compact review judgment and selective rewriting;
- patch application and integrity validation when they inspect text-bearing artifacts.

These workers may read the private files assigned to them. They write detailed results inside the private workspace and must not paste source text, candidates, translations, context summaries, reviewer prose, command output, or refusal explanations into their final response.

Inventory and context workers classify only structure, player visibility, bindings, order, and evidence coverage. They do not decide whether user-visible text is eligible for translation based on subject matter. Anticipating that a provider might refuse is not a valid exclusion or routing decision.

## Parent-facing envelope

Every data-plane worker writes one `parr0t.control-envelope.v2` JSON file and returns only its workspace-relative path. The parent validates it before changing orchestration state:

```text
game-l10n validate-control-envelope \
  --workspace <workspace> \
  --input <relative-envelope.json> \
  --expected-role <role> \
  --expected-stage <stage>
```

A valid envelope contains exactly these fields:

```json
{
  "schema": "parr0t.control-envelope.v2",
  "worker_role": "translator",
  "stage": "translation",
  "status": "fallback_required",
  "target_ids": ["synthetic-target-001"],
  "counts": {"total": 1, "success": 0, "fallback_required": 1},
  "issue_codes": ["fallback_required"],
  "artifact_paths": [".game-l10n/data-plane/synthetic-result.json"],
  "queue_transitions": [
    {"from": "primary_pending", "to": "translation_fallback_pending", "count": 1}
  ],
  "route_evidence_path": ""
}
```

The CLI allowlists roles, stages, statuses, count names, queue names, and issue codes. Unknown fields and content-derived classifications are rejected without echoing their values. Before returning an envelope to the parent, every data-plane target ID is deterministically replaced with a `ctl_<sha256>` alias; raw engine keys and source-derived IDs never cross the boundary. Detailed review findings such as meaning, omission, voice, tone, and terminology remain only in the private review artifact; the parent sees only opaque control IDs and the queue transition that requires rewriting. Artifact paths must be relative, remain inside the private workspace, and already exist; every path segment is limited to `[A-Za-z0-9._-]` so a filename cannot carry spaces, line breaks, or non-ASCII source text to the parent. Operator-chosen locations that later appear in envelopes, such as a translation-exchange `--output` directory or `files.context_pack`, are rejected under the same rule when they are created.

Provider-specific refusal status and prose stay inside the private data plane. Before a result crosses into the parent, the worker normalizes it to the parent-safe `fallback_required` status. The parent never receives `provider_refusal`, a refusal explanation, or a content-derived eligibility label. It advances the affected IDs to the configured next route without opening private content or deciding whether another provider should receive them.

A configured fallback is an ordinary authorized route. A fresh no-history worker sends the exact, unmodified target and its required context to that route. No generated worker contract may add a content-based pre-screen, transfer classification, or other eligibility gate. Only the configured provider can return its own categorical result after the invocation is attempted.

## Fixed manual and fallback exchange

When a direct provider pipeline cannot own the next translation step, the data plane uses only `parr0t.translation-exchange/v1`; see [translation exchange](translation-exchange.md). The CLI creates the immutable request rows and seals the fixed success/failure rows. The parent must not open any text-bearing exchange file, tell a worker to summarize it, or invent another handoff schema. It receives only the sealed metadata receipt and validated v2 envelope. A success moves to `review_pending`; an explicit failure or missing row stays `translation_fallback_pending` as `fallback_required`.

## Terminal route evidence

`source_preserved` is legal only after every configured route for each affected target has actually been attempted and ended in a terminal categorical failure. A v2 envelope that transitions any target to `source_preserved` must include `route_evidence_path`, and that path must also appear in `artifact_paths`. The referenced text-free artifact has this exact shape:

```json
{
  "schema": "parr0t.route-evidence.v1",
  "stage": "translation",
  "target_ids": ["synthetic-target-001"],
  "attempts": [
    {
      "target_id": "synthetic-target-001",
      "route": "translator-primary",
      "status": "provider_refusal",
      "attempts": 1
    },
    {
      "target_id": "synthetic-target-001",
      "route": "translator-fallback",
      "status": "quota_exhausted",
      "attempts": 1
    }
  ]
}
```

The validator reads the selected workspace routing chain, requires one terminal outcome with a positive attempt count for every configured route and target, checks target/count alignment, and rejects missing or extra routes. Legacy v1 envelopes remain readable only for nonterminal continuation; they cannot transition to `source_preserved`. A pre-screened or uncalled fallback therefore remains `fallback_required` rather than being mislabeled as route exhaustion.

## Free-form response and leak handling

If a worker returns prose instead of the envelope path, the parent must not quote, summarize, classify, or use it. If source/candidate text, a prose refusal explanation, or a content classification has already entered the parent conversation—or the parent performed semantic review—record the incident immediately:

```text
game-l10n record-control-plane-leak \
  --workspace <workspace> \
  --incident-code <fixed-code>
```

This writes a text-free `rotation_required` marker. All non-recovery CLI work, including native task preparation, import, merge, and envelope validation, then stops with `E_CONTROL_PLANE_ROTATION_REQUIRED`. Metadata-only status, diagnostics, incident recording, and rotation acknowledgment remain available. Preserve completed data-plane artifacts and queue metadata; do not delete or redo them.

The contaminated top-level agent must stop and must not run or confirm the resume command. `record-control-plane-leak` writes the only valid, digest-bound, metadata-only handoff receipt at `.game-l10n/control-plane-handoff.json`; status output intentionally omits its internal incident identifier. Do not write or read a prose `continuation.txt`, summary, or alternate handoff file. A fresh control plane, with no inherited game-content history, acknowledges the exact receipt and establishes its entry gate atomically:

```text
game-l10n resume-control-plane \
  --workspace <workspace> \
  --handoff-receipt .game-l10n/control-plane-handoff.json \
  --fresh-session-confirmed \
  --contracts-read \
  --source-unopened
```

The confirmation flag is necessary but insufficient: the command rejects any receipt path, schema, digest, incident, or generation not bound to the pending rotation. Acknowledgment advances the generation, resets only generation-local polling observations, and preserves queues, completed results, usage, and pass checkpoints. It does not approve omitted targets or discard pending work. A successful command returns an already-ready entry gate; do not run a second entry or resume sequence.

Use `game-l10n control-plane-status --workspace <workspace>` to inspect the text-free generation and rotation state.
