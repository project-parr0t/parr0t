# Control-plane isolation contract

The top-level Parr0t agent schedules work and advances queues. It does not inspect or judge game text. This boundary applies from the first inventory pass through final validation; it is not limited to translation and review providers.

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

Every data-plane worker writes one `parr0t.control-envelope.v1` JSON file and returns only its workspace-relative path. The parent validates it before changing orchestration state:

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
  "schema": "parr0t.control-envelope.v1",
  "worker_role": "translator",
  "stage": "translation",
  "status": "partial",
  "target_ids": ["synthetic-target-001"],
  "counts": {"total": 1, "success": 0, "refused": 1},
  "issue_codes": ["provider_refusal"],
  "artifact_paths": [".game-l10n/data-plane/synthetic-result.json"],
  "queue_transitions": [
    {"from": "primary_pending", "to": "translation_fallback_pending", "count": 1}
  ]
}
```

The CLI allowlists roles, stages, statuses, count names, queue names, and issue codes. Unknown fields and content-derived classifications are rejected without echoing their values. Detailed review findings such as meaning, omission, voice, tone, and terminology remain only in the private review artifact; the parent sees only the IDs and queue transition that require rewriting. Artifact paths must be relative, remain inside the private workspace, and already exist.

Inventory workers cannot produce `provider_refusal`. Only a configured model/provider invocation may produce it for the context chunk, translation target, or review target it actually processed. The top-level agent advances that unit to the configured next route. It never opens private content to confirm the refusal and never converts it into an omission recommendation.

## Free-form response and leak handling

If a worker returns prose instead of the envelope path, the parent must not quote, summarize, classify, or use it. If source/candidate text, a prose refusal explanation, or a content classification has already entered the parent conversation—or the parent performed semantic review—record the incident immediately:

```text
game-l10n record-control-plane-leak \
  --workspace <workspace> \
  --incident-code <fixed-code>
```

This writes a text-free `rotation_required` marker. All non-recovery CLI work, including native task preparation, import, merge, and envelope validation, then stops with `E_CONTROL_PLANE_ROTATION_REQUIRED`. Metadata-only status, diagnostics, incident recording, and rotation acknowledgment remain available. Preserve completed data-plane artifacts and queue metadata; do not delete or redo them.

The contaminated top-level agent must stop. A fresh control plane, with no inherited game-content history, reads only the metadata checkpoint and acknowledges the matching incident:

```text
game-l10n resume-control-plane \
  --workspace <workspace> \
  --incident-id <incident-id>
```

Acknowledgment advances the control-plane generation; it does not approve omitted targets or discard pending work. The fresh agent resumes the configured primary/fallback/review order from existing checkpoints.

Use `game-l10n control-plane-status --workspace <workspace>` to inspect the text-free generation and rotation state.
