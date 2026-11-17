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

The first saved attestation freezes the primary ID lineage in `.game-l10n/inventory-primary-identity.json`. Later reconciliation may append newly proven targets, but it may not rename, remove, or reorder any checkpoint-bound primary ID. If raw IDs scarcely overlap while at least 90% of the smaller inventory's source multiset overlaps, the core emits `INVENTORY_IDENTITY_NAMESPACE_MISMATCH`; it does not count the two entire inventories as content loss. The reconciler must keep primary IDs canonical and add deterministic comparison-key evidence to the method-specific side. Duplicate source text alone is never alias evidence; use occurrence, location, resource, or equivalent engine evidence. If an inherited attestation changed before ID lineage could be bound, stop with `E_INVENTORY_IDENTITY_LINEAGE_UNAVAILABLE` instead of rebuilding prior checkpoints.
Physical comparison keys take precedence over method-specific raw ID overlap. Substantial evidenced comparison-key overlap routes remaining occurrences as ordinary gaps, not a new namespace loss. While a namespace mismatch remains unresolved, repair aliases in a separate generation before expanding the primary inventory; a union that merely makes both manifests equal cannot bypass that gate. Merely changed inputs do not reset the no-progress observation count.

An exclusion has `reason_code` and `affects_user_visible_text`. If the latter is true, the inventory cannot pass. Inventory workers classify structure and player visibility only: subject matter, an anticipated safeguard response, or a content-policy guess is never an extraction exclusion. Raw literal presence proves neither display scope nor completeness. Cache each method's structural parse, physical provenance, categories, display bindings, and dependency evidence in a single source pass; derive its inventory from that cache. Account separately for raw occurrences, display-bound targets, unresolved display dependencies, and proven non-display occurrences. Preserve equal-valued physical occurrences and source order. Proven internal/control/debug-only occurrences may be excluded with categorical reasons, counts, and evidence fingerprints in existing `coverage.exclusions`; engine or module names alone never justify blanket exclusions. Retain player-visible system messages and uncertain dynamic display dependencies.
New core-bound inventory tasks additionally require `coverage.display_scope` (`parr0t.display-scope.v1`) and per-target `metadata.display_scope_evidence`. The declaration contains non-negative `raw_occurrence_count`, `display_candidate_count`, `control_only_count`, and `unclassified_count`, plus an `evidence_fingerprint`; candidate count must equal segment count. Each target carries `display_scope_evidence.kind` as `positive_display_binding` or `possible_display_dependency`, and its own nonempty `evidence_fingerprint`. Raw-only, unclassified, and control-only records are accounting data, not translation targets; `declared_complete` cannot promote them into scope. Uncertain actual display candidates must be retained. Core verifies schema, accounting shape, counts, evidence presence, and inventory bindings; it cannot verify that SDK fingerprints truthfully describe engine behavior. Older snapshots without this declaration remain readable and are not retroactively held to the new sealing contract.
Each new-contract target also carries a nonempty `metadata.display_binding_id`. Its scope evidence binds the exact UTF-8 source with `source_sha256` and uses a lowercase SHA-256 `evidence_fingerprint`. A control/lookup/index/script storage value requires `display_projection_required=true`; it is not a direct replacement target. Control-only plus unclassified accounting cannot exceed raw occurrences. A fingerprint binds evidence identity, not its truth: the SDK remains responsible for the referenced render or dependency proof.


An engine SDK may reduce display-candidate overcapture through the capability contract in `docs/engine-static-scope.md`. The inventory then records candidate, included, conservatively retained, and proven-unused counts in `coverage.static_scope`; included count must match its segment count. This denominator is display candidates, not every raw literal. Two differently implemented graph analyses must agree before an SDK removes a display candidate. Dynamic or indirect references retain the affected targets; absence of a direct reference is not proof of non-display. Reachability exclusions without scope tracking are invalid. Static reachability is not runtime observation and never satisfies the runtime coverage gate.

When a target is one part of a composed UI string, both inventories must agree on the evidence-backed fields defined by `docs/segment-metadata.md`. A stable source ID without its slot role or composition dependencies is insufficient for safe reuse and review.
The inventories must also agree on source-language authority, terminology concept IDs, and parenthetical policy from `docs/localization-authority.md`. These fields are immutable semantic inputs, not translator output. Malformed authority metadata fails inventory validation; a mismatch emits `INVENTORY_LOCALIZATION_AUTHORITY_MISMATCH` and prevents static completion.

Run the gate before context analysis:

```text
game-l10n verify-inventory --workspace <workspace> --primary <primary.json> --independent <independent.json>
```

The attestation is bound to the primary path and content fingerprint. Changing either inventory invalidates downstream context and completion claims.

Inventory strings, including IDs, metadata values, and object keys, must contain valid Unicode scalar values in UTF-8 JSON. Malformed UTF-8 or unpaired surrogate escapes fail categorically with `E_INVENTORY_INVALID_UNICODE` before a new attestation or identity binding is written. No replacement, dropping, or lossy normalization is permitted. Valid supplementary characters, including JSON surrogate-pair escapes decoded to those characters, retain their exact content and fingerprint behavior.

Every saved attestation also records a metadata-only reconciliation generation, gap vector, gap score, identity gate, and progress state. `incomplete` always has `continuation_required=true` and is never a terminal job result. `reconcile_inventory_identity_namespace` repairs aliases before ordinary content gaps. If a bounded repair reduces the gap score, run the next `inventory_reconciler` task from cached scans. An ID/order mismatch, local parser/layout error, or newly found target is not a user decision and must not end the job. Stop only when status proves an external missing input/capability, an applicable deterministic gate, or the configured unchanged-progress limit; preserve the checkpoint and exact remaining codes. A root call/token turn ceiling is not applicable unless the user explicitly requested its concrete value and the manifest records that approval.

## Evidence-bound scope correction

Canonical occurrence identity and current translation eligibility are separate. A proven correction never deletes, renames, reorders, or changes the source of a checkpoint-bound primary occurrence. Keep historical primary rows in `segments`; the core compares and loads only eligible display rows after validating `coverage.scope_correction`.

This optional object uses `schema: parr0t.inventory-scope-correction.v1`, `basis`, and `records` only:

- `basis.file` names an immutable pre-correction canonical inventory snapshot in the inventory's own directory. It is a safe ASCII basename ending in `.json`, not an absolute path or traversal. `basis.sha256` binds its complete bytes. Both inventories name the same content-bound basis. On the first correction, its semantic fingerprint must match the prior primary attestation; later corrections either retain that basis or roll forward from the prior attested primary. The basis retains earlier correction history on a rollover.
- Each record binds `canonical_id`, physical `comparison_key`, exact UTF-8 `source_sha256`, `classification`, `projection_ids`, and `evidence`. Duplicate values are not occurrence identity. Every retired occurrence must exist unchanged in the bound canonical basis, and the primary retains all basis IDs, source values, physical keys, and relative order.
- `evidence` contains exactly two independent proofs, each with `method`, `implementation_fingerprint`, and `evidence_sha256`. Method names, implementations, and proof digests must differ. The methods and implementations must match the actual compared inventories. Both inventories must independently agree on the complete correction object; one-sided or conflicting proposals yield `INVENTORY_SCOPE_CORRECTION_MISMATCH`, not static completion.
- `classification: proven_non_display` requires an empty `projection_ids`. Use it only when both independent analyses prove the occurrence cannot reach a player-visible display. Refusal, an absent tool, an uncertain dependency, raw duplication, and a convenient denominator are not evidence of non-display.
- `classification: control_storage_with_projection` requires nonempty `projection_ids` naming separate, uniquely bound eligible display bindings. Each projection row has a matching `display_binding_id`, `display_projection_of` equal to the original physical comparison key, positive source-bound display evidence, and no control-storage role. The pair verifier validates both entire documents before reporting `INVENTORY_SCOPE_PROJECTION_REQUIRED` and retaining an incomplete attestation. A segment consumer still rejects the unresolved scope with `E_INVENTORY_SCOPE_PROJECTION_REQUIRED`; the control value itself never becomes translated output.

Corrected inventories require the ordinary `coverage.display_scope` declaration and source-bound evidence for every eligible row. Its display candidate count and optional static-scope included count describe eligible rows; raw occurrence accounting still includes retained history. Proof hashes bind evidence identity, not its semantic truth: SDKs must retain the actual independent structural, display, dependency, and projection evidence privately and never fabricate a matching ledger.

`verify-inventory` retains the full canonical primary identity binding and fingerprints the full documents and correction evidence. Once correction is introduced, the identity ledger freezes each occurrence's source digest and physical comparison key, including subsequently attested additions; retaining an older basis cannot change an added ID's meaning. Bootstrapping an older ID-only ledger requires the unchanged attested primary or a rollover basis matching that exact attestation. Attestations distinguish `primary_occurrences` / `independent_occurrences` from active `primary_segments` / `independent_segments`. Changed eligibility or valid evidence invalidates affected downstream bindings; historical source/key mutations and silent removal of correction history are rejected. A stale basis, including in a one-sided proposal, cannot certify readiness or a blocked-state hold.

All core context, translation, review, retry, correction, and native-pass ingestion uses `formats.read_segments`, which validates this bound correction and returns only eligible rows in canonical relative order. Adjacency is formed from those display rows, not retired control storage. Runtime coverage uses the same active denominator. SDK application and pack implementations must consume this same eligible scope and preserve retired source/control bytes; a correction is neither a translation failure nor `source_preserved`, and it never supplies runtime evidence or bypasses review, application, or pack verification.

Pair verification reuses each document's validated active scope and a shared canonical basis only within that one command. A shared basis is read, parsed, indexed, and fingerprinted once per resolved path, while each inventory still checks its own expected digest and occurrence/projection bindings. No process-global or cross-command readiness cache is used: a later verification rereads the basis, and downstream admission still checks bound input freshness.

## Context execution gate

The context state records a successful sealed context task. Provider refusal, capacity, timeout, malformed output, or another operational error produces a non-success receipt and status exposes a fresh bound `context_analyzer` attempt; an existing partial file never converts that failure into success. Each new task binds one configured native route. Its relay may run the configured ceiling of up to 32 core-bound workers on disjoint atomically claimed chunks, store slot-owned fragments, and invoke one fresh source-order finalizer; workers never share-write the context pack. Completed fragments remain cached and only unfinished claims retry. A receipt with measurable progress restores the configured base chunk ceiling; only a complete no-progress route cycle halves it toward the floor. Each attempt owns a generation-and-attempt-specific envelope, so stale output cannot be resealed. The top-level agent sees only receipt counts and must not substitute `analyze-context`, inspect private fragments, or read the source inventory. Only the core seal may write a successful context state after deterministic domain and evidence coverage passes. Context receipts and native-pass unchanged-observation limits are nonterminal here; only status may prove an external blocker.

## Existing translation review

Use a dedicated full pass when an earlier translation is being improved:

```text
game-l10n review-existing --workspace <workspace> --input <primary.json> --existing <old-results.json> --output <new-results.json>
```

Every existing target is re-triaged; selected flags/risk/sample targets also receive strong review. Integrity checks can trigger a focused repair first, but a matching ID, source hash, or candidate digest cannot substitute for the current verdict. Review completion records both the reused-candidate count and the freshly reviewed reused-candidate count.
Correction and re-review may change target text only. They never rename a target, edit its source, reorder records, or rewrite authority metadata. Glossary, source-language authority, or speaker-profile changes invalidate the affected semantic binding and require fresh current-generation triage; selected targets still require strong review.

## Failure queue and held patches

Retryable failures are saved as target IDs, categorical failure codes, and blocking state only. They contain no source or translation text. The queue is not retried automatically in a later turn. After the user explicitly asks to continue, run:

```text
game-l10n resume-failures --workspace <workspace> --input <primary.json> --output <results.json> --user-requested
```

`failed` entries remain in the retryable queue. `source_preserved` and user-approved `accepted_unreviewed` entries are terminal unresolved outcomes and are recorded separately; they are not silently retried by `resume-failures`. A new user request may reopen them only through a new explicit correction or translation decision, never by pretending the exhausted route is still pending.

An SDK marks a target `metadata.blocking_on_failure=true` when leaving that target untranslated would make the patch unusable or unsafe. If any retryable or terminal blocking target is unresolved, Parr0t writes a private held checkpoint, returns status `held`, and does not emit the requested patch artifact. Optional targets may preserve exact source after route exhaustion; they can be packaged, but both the failure queue and completion attestation keep them visibly terminal-unresolved rather than translated.

## Result-to-patch binding

Finalization writes the result artifact first, records its absolute workspace path, SHA-256 digest, byte length, and current target-language policy binding in the completion attestation, and never rewrites it after attestation. Wrong-language accepted output blocks completion. Packing accepts only that exact bound artifact under the unchanged language policy. The pack cache also binds the completion-attestation digest, input digest, tool fingerprint, and output digest. A changed result, language policy, completion record, tool, or patch invalidates `local_patch_ready` and requires only the affected stage to run again.

Tool-backed pack readiness also verifies the current configured command and resolved executable/tool contents. Content, rather than timestamps alone, determines cache reuse. A failed forced rerun invalidates the previous stage success, and post-execution input checks detect timestamp-preserving mutations. Separately verified SDK packages keep their verifier binding. Neither path may use the input itself, a hardlink alias, an overlapping tree, an external output, or an output covering cache state as the package. Private tool logs retain failure evidence; parent errors remain categorical. Stronger directory/tool fingerprints may invalidate older cache entries once.

The selected SDK must also account for each translated target at the application boundary. A target is either a direct display-safe replacement or an evidenced display projection that preserves any shared lookup, index, condition, animation, or script value. Unmapped display bindings and translated control values are blocking application defects even when inventory alignment and semantic review are complete. Compare original/patched functional groups and the actual package replacement set; file count or a successful pack command is not application evidence.

For composed or dynamically formatted UI, application evidence must exercise the complete emitted value, not merely prove that each fragment exists in a translation table. Use the evidenced render pattern with representative dynamic values and verify that every static source fragment is replaced, dynamic values and formatting are preserved, and no replacement leaks into an unrelated slot. A whole-string lookup does not cover a value whose runtime shape can vary; each variable shape needs a fragment-safe binding or an equivalent evidenced display projection.

Static inspectors must close source-file handles before handoff. Tools with uncertain lifetimes must inspect content-bound workspace snapshots because read-only handles can block an authorized replacement.

When the user explicitly requests image translation, the separate gates in `docs/image-localization.md` run before packing. Text completion does not authorize OCR, and an image result cannot satisfy or weaken text inventory/review coverage.

## Runtime observations

Parr0t never launches a real game merely to satisfy this gate. A selected SDK or user-run diagnostic may write a metadata-only `parr0t.runtime-coverage.v1` report containing the bound inventory fingerprint, observed target IDs, and counts of unmatched, untranslated, unvisited, or known-unobservable categories. Record it with:

```text
game-l10n record-runtime-coverage --workspace <workspace> --report <runtime-report.json>
```

This records evidence only. It does not install, update, launch, or close a game.

A synthetic fixture proves only the exercised static or synthetic property. It must not be reported as “working in game”. Player-visible placeholder/default strings, language-registration assumptions, and runtime interception behavior remain blocking or explicitly unverified until deterministic engine evidence or an authorized/user-executed runtime observation establishes them.
