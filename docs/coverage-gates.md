# Coverage and completion gates

Parr0t separates four claims that must not be collapsed into one “complete” result:

1. **Static inventory coverage** — two independent extraction methods found the same ordered targets and neither excluded user-visible scope.
2. **Result alignment** — the current result artifact has exactly one matching row for every target in its declared scope.
3. **Semantic review coverage** — every non-preserved candidate, including reused and previously unflagged candidates, received a fresh independent low-cost triage or selected strong verdict for this context and candidate digest.
4. **Runtime coverage** — an authorized or user-executed observation found no unmatched, untranslated, unvisited, or unobservable user-visible state.

Only the fourth claim can establish runtime completeness. Static checks must report `REAL_RUNTIME_NOT_EXECUTED` when no runtime report exists. `manifest_missing_count=0` is never evidence that the running game has zero omissions.

## Independent source inventories

The selected engine SDK produces two JSON documents in the private workspace. They use different extraction methods but the same stable target-ID contract:

```json
{
  "schema": "parr0t.source-inventory.v1",
  "method": "synthetic-primary-method",
  "implementation_fingerprint": "synthetic-primary-implementation-v1",
  "coverage": {
    "declared_complete": true,
    "exclusions": []
  },
  "segments": [
    {
      "id": "synthetic-target-001",
      "source": "SOURCE_TEXT",
      "metadata": {
        "text_type": "label",
        "blocking_on_failure": true,
        "ui_slot_role": "value",
        "ui_composed_with_ids": ["synthetic-label-001"],
        "ui_render_pattern": "{label}: {value}"
      }
    }
  ]
}
```

The two documents must name different methods and carry different implementation fingerprints; copying one manifest and changing its label is not an independent check. When an engine format gives the same physical occurrence different method-specific IDs, each row may carry the same nonempty `metadata.inventory_comparison_key`. The core compares that canonical key, source, UI contract, and order while preserving the primary ID used downstream. Keys must be unique in each inventory; this is deterministic alias normalization, not permission to merge duplicate text by value.

An exclusion has `reason_code` and `affects_user_visible_text`. If the latter is true, the inventory cannot pass. Inventory workers classify structure and player visibility only: subject matter, an anticipated safeguard response, or a content-policy guess is never an extraction exclusion. Overcapture is resolved inside the engine SDK; it is not a reason to silently omit an entire source class.

An engine SDK may reduce overcapture through the capability contract in `docs/engine-static-scope.md`. The inventory then records candidate, included, conservatively retained, and proven-unused counts in `coverage.static_scope`. Two differently implemented graph analyses must agree before an SDK removes a target. Dynamic or indirect references retain the affected targets; absence of a direct reference is not proof of non-display. Static reachability is not runtime observation and never satisfies the runtime coverage gate.

When a target is one part of a composed UI string, both inventories must agree on the evidence-backed fields defined by `docs/segment-metadata.md`. A stable source ID without its slot role or composition dependencies is insufficient for safe reuse and review.

Run the gate before context analysis:

```text
game-l10n verify-inventory --workspace <workspace> --primary <primary.json> --independent <independent.json>
```

The attestation is bound to the primary path and content fingerprint. Changing either inventory invalidates downstream context and completion claims.

Every saved attestation also records a metadata-only reconciliation generation, gap vector, gap score, and progress state. `incomplete` always has `continuation_required=true` and is never a terminal job result. If a bounded repair reduces the gap score, run the next `inventory_reconciler` task from cached scans. An ID/order mismatch, local parser/layout error, or newly found target is not a user decision and must not end the job. Stop only when status proves an external missing input/capability, an applicable deterministic gate, or the configured unchanged-progress limit; preserve the checkpoint and exact remaining codes. A root call/token turn ceiling is not applicable unless the user explicitly requested its concrete value and the manifest records that approval.

## Context execution gate

The context state records a successful sealed context task. Provider refusal, capacity, timeout, malformed output, or another operational error produces a non-success receipt and status exposes a fresh bound `context_analyzer` attempt; an existing partial file never converts that failure into success. The top-level agent must not substitute `analyze-context` in Codex App mode or inspect the partial pack. Only the core seal may write a successful context state after deterministic domain and evidence coverage passes. A context-pack file without that matching state is never accepted.

## Existing translation review

Use a dedicated full pass when an earlier translation is being improved:

```text
game-l10n review-existing --workspace <workspace> --input <primary.json> --existing <old-results.json> --output <new-results.json>
```

Every existing target is re-triaged; selected flags/risk/sample targets also receive strong review. Integrity checks can trigger a focused repair first, but a matching ID, source hash, or candidate digest cannot substitute for the current verdict. Review completion records both the reused-candidate count and the freshly reviewed reused-candidate count.

## Failure queue and held patches

Retryable failures are saved as target IDs, categorical failure codes, and blocking state only. They contain no source or translation text. The queue is not retried automatically in a later turn. After the user explicitly asks to continue, run:

```text
game-l10n resume-failures --workspace <workspace> --input <primary.json> --output <results.json> --user-requested
```

`failed` entries remain in the retryable queue. `source_preserved` and user-approved `accepted_unreviewed` entries are terminal unresolved outcomes and are recorded separately; they are not silently retried by `resume-failures`. A new user request may reopen them only through a new explicit correction or translation decision, never by pretending the exhausted route is still pending.

An SDK marks a target `metadata.blocking_on_failure=true` when leaving that target untranslated would make the patch unusable or unsafe. If any retryable or terminal blocking target is unresolved, Parr0t writes a private held checkpoint, returns status `held`, and does not emit the requested patch artifact. Optional targets may preserve exact source after route exhaustion; they can be packaged, but both the failure queue and completion attestation keep them visibly terminal-unresolved rather than translated.

## Result-to-patch binding

Finalization writes the result artifact first, records its absolute workspace path, SHA-256 digest, byte length, and current target-language policy binding in the completion attestation, and never rewrites it after attestation. Wrong-language accepted output blocks completion. Packing accepts only that exact bound artifact under the unchanged language policy. The pack cache also binds the completion-attestation digest, input digest, tool fingerprint, and output digest. A changed result, language policy, completion record, tool, or patch invalidates `local_patch_ready` and requires only the affected stage to run again.

The selected SDK must also account for each translated target at the application boundary. A target is either a direct display-safe replacement or an evidenced display projection that preserves any shared lookup, index, condition, animation, or script value. Unmapped display bindings and translated control values are blocking application defects even when inventory alignment and semantic review are complete. Compare original/patched functional groups and the actual package replacement set; file count or a successful pack command is not application evidence.

When the user explicitly requests image translation, the separate gates in `docs/image-localization.md` run before packing. Text completion does not authorize OCR, and an image result cannot satisfy or weaken text inventory/review coverage.

## Runtime observations

Parr0t never launches a real game merely to satisfy this gate. A selected SDK or user-run diagnostic may write a metadata-only `parr0t.runtime-coverage.v1` report containing the bound inventory fingerprint, observed target IDs, and counts of unmatched, untranslated, unvisited, or known-unobservable categories. Record it with:

```text
game-l10n record-runtime-coverage --workspace <workspace> --report <runtime-report.json>
```

This records evidence only. It does not install, update, launch, or close a game.

A synthetic fixture proves only the exercised static or synthetic property. It must not be reported as “working in game”. Player-visible placeholder/default strings, language-registration assumptions, and runtime interception behavior remain blocking or explicitly unverified until deterministic engine evidence or an authorized/user-executed runtime observation establishes them.
