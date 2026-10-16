# Control-plane isolation contract

The top-level Parr0t agent schedules work and advances queues. It does not inspect or judge game text. This boundary applies from the first inventory pass through final validation; it is not limited to translation and review providers. Arbitrary subprocess output remains in private workspace logs; a parent-facing tool failure is categorical only.

## Data-plane stages

The following roles are data-plane workers:

- primary and independent inventory;
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

The CLI allowlists roles, stages, statuses, count names, queue names, and issue codes. Unknown fields and content-derived classifications are rejected without echoing their values. Before returning an envelope to the parent, every data-plane target ID is deterministically replaced with a `ctl_<sha256>` alias; raw engine keys and source-derived IDs never cross the boundary. Detailed review findings such as meaning, omission, voice, tone, and terminology remain only in the private review artifact; the parent sees only opaque control IDs and the queue transition that requires rewriting. Artifact paths must be relative, remain inside the private workspace, and already exist.

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

The contaminated top-level agent must stop and must not run or confirm the resume command. Status output intentionally omits the internal incident identifier. A fresh control plane, with no inherited game-content history, reads only the categorical state and acknowledges the current incident:

```text
game-l10n resume-control-plane \
  --workspace <workspace> \
  --fresh-session-confirmed
```

The confirmation flag is an assertion by the newly started agent, not a recovery option for the contaminated one. Acknowledgment advances the control-plane generation; it does not approve omitted targets or discard pending work. The fresh agent resumes the configured primary/fallback/review order from existing checkpoints.

Use `game-l10n control-plane-status --workspace <workspace>` to inspect the text-free generation and rotation state.
