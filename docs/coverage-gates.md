# Coverage and completion gates

Parr0t separates four claims that must not be collapsed into one “complete” result:

1. **Static inventory coverage** — two independent extraction methods found the same ordered targets and neither excluded user-visible scope.
2. **Result alignment** — the current result artifact has exactly one matching row for every target in its declared scope.
3. **Semantic review coverage** — every non-preserved candidate, including reused and previously unflagged candidates, received a fresh independent verdict for this context and candidate digest.
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

The two documents must name different methods and carry different implementation fingerprints; copying one manifest and changing its label is not an independent check. An exclusion has `reason_code` and `affects_user_visible_text`. If the latter is true, the inventory cannot pass. Overcapture is resolved inside the engine SDK; it is not a reason to silently omit an entire source class.

When a target is one part of a composed UI string, both inventories must agree on the evidence-backed fields defined by `docs/segment-metadata.md`. A stable source ID without its slot role or composition dependencies is insufficient for safe reuse and review.

Run the gate before context analysis:

```text
game-l10n verify-inventory --workspace <workspace> --primary <primary.json> --independent <independent.json>
```

The attestation is bound to the primary path and content fingerprint. Changing either inventory invalidates downstream context and completion claims.

## Context execution gate

The context state records a successful route for every chunk. Provider refusal, capacity, timeout, malformed output, or another operational error advances to the next configured context route. If all routes fail for any chunk, no successful state is written and translation stops. A context-pack file without its matching successful execution attestation is never accepted.

## Existing translation review

Use a dedicated full pass when an earlier translation is being improved:

```text
game-l10n review-existing --workspace <workspace> --input <primary.json> --existing <old-results.json> --output <new-results.json>
```

Every existing target is re-judged. Integrity checks can trigger a focused repair first, but a matching ID, source hash, or candidate digest cannot substitute for semantic judgment. Review completion records both the reused-candidate count and the freshly reviewed reused-candidate count.

## Failure queue and held patches

Operational failures are saved as target IDs, categorical failure codes, and blocking state only. They contain no source or translation text. The queue is not retried automatically in a later turn. After the user explicitly asks to continue, run:

```text
game-l10n resume-failures --workspace <workspace> --input <primary.json> --output <results.json> --user-requested
```

An SDK marks a target `metadata.blocking_on_failure=true` when leaving that target untranslated would make the patch unusable or unsafe. If any blocking target is unresolved, Parr0t writes a private held checkpoint, returns status `held`, and does not emit the requested patch artifact. Optional targets may preserve exact source after route exhaustion, but the completion attestation remains explicit about them.

## Runtime observations

Parr0t never launches a real game merely to satisfy this gate. A selected SDK or user-run diagnostic may write a metadata-only `parr0t.runtime-coverage.v1` report containing the bound inventory fingerprint, observed target IDs, and counts of unmatched, untranslated, unvisited, or known-unobservable categories. Record it with:

```text
game-l10n record-runtime-coverage --workspace <workspace> --report <runtime-report.json>
```

This records evidence only. It does not install, update, launch, or close a game.
