# Coverage and completion gates

Parr0t reports coverage and assurance honestly; a single “complete” label must not erase what was actually verified:

1. **Static inventory coverage** — in full-assurance mode, two independent extraction methods agree on ordered targets and user-visible scope. A configured lower-assurance mode reports its narrower evidence and never claims independent verification.
2. **Result alignment** — the current result artifact has exactly one matching row for every target in its declared scope; partial artifacts retain explicit pending IDs.
3. **Semantic review coverage** — each claimed reviewed candidate has current-generation verdict evidence bound to its context and candidate digest.
4. **Runtime coverage** — an authorized or user-executed observation reports unmatched, untranslated, unvisited, and unobservable user-visible states.

Only runtime evidence establishes runtime completeness. Static checks report `REAL_RUNTIME_NOT_EXECUTED` when no runtime report exists. `manifest_missing_count=0` never proves that the running game has no omissions. Lower assurance may establish a useful checkpoint but must not be labeled full assurance.

## Independent source inventories

Full-assurance inventory verification uses two independently implemented extraction methods with the same stable target-ID contract. When `coverage.require_independent_inventory` is false, report the selected lower assurance explicitly; do not claim independent coverage or silently weaken a requested full-assurance gate.

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
Fresh Core inventory tasks bind `parr0t.text-eligibility.v1` alongside the
existing `coverage.display_scope` (`parr0t.display-scope.v1`) occurrence
accounting. Each `coverage.text_source` declares exactly:

| Field | Locale path | Display extraction |
| --- | --- | --- |
| `route` | `locale_data` | `display_extraction` |
| `locale_probe` | `applicable` | `unavailable` |
| `evidence_fingerprint` | SDK probe SHA-256 | SDK probe SHA-256 |
| `screen_review` | `not_applicable` | `static` or explicitly `user_skipped` |
| `pending_display_count` | `0` | nonnegative; `0` required for complete inventory |

Locale jobs inventory only verified locale values. Display jobs inventory
only positively established display values; analysis that finds an unresolved
actual display dependency records it as pending, not as a segment. Unclassified
raw strings remain private accounting data, not pending display dependencies
without evidence. Both inventory methods must agree on the route.

Each row binds `metadata.display_binding_id`, its exact UTF-8 `source_sha256`,
and a lowercase SHA-256 `evidence_fingerprint`. Its `metadata.storage_role` and
`metadata.display_scope_evidence.kind` are `locale_value`/`locale_value` for the
locale path (evidence `binding_basis: locale_catalog`) and
`display_value`/`positive_display_binding` for the display path (evidence
`binding_basis: format_field` or `render_argument`). A digest identifies
underlying evidence; it cannot turn a claim into actual SDK proof. The Core
does not approve unknown fields, structural keys, or executable code as text.
Previously attested inventories retain their original evidence contract.

Only after the user's explicit approval for workspace-wide additional text
screen review, record `game-l10n screen-review-policy --workspace <workspace>
--mode skip --user-requested`. This is not a locale choice, visual/image
authorization, runtime launch approval, extraction waiver, or translation
review waiver. The default is no skip; an explicit `--mode required
--user-requested` restores it for future tasks and new attestations. Sealing
uses the task's bound approval. Eligibility evidence remains mandatory.


An engine SDK may reduce display-candidate overcapture through the capability contract in `docs/engine-static-scope.md`. The inventory then records candidate, included, conservatively retained, and proven-unused counts in `coverage.static_scope`; included count must match its segment count. This denominator is display candidates, not every raw literal. Two differently implemented graph analyses must agree before an SDK removes a display candidate. Dynamic or indirect references retain the affected targets; absence of a direct reference is not proof of non-display. Reachability exclusions without scope tracking are invalid. Static reachability is not runtime observation and never satisfies the runtime coverage gate.

When a target is one part of a composed UI string, both inventories must agree on the evidence-backed fields defined by `docs/segment-metadata.md`. A stable source ID without its slot role or composition dependencies is insufficient for safe reuse and review.
The inventories must also agree on source-language authority, terminology concept IDs, and parenthetical policy from `docs/localization-authority.md`. These fields are immutable semantic inputs, not translator output. Malformed authority metadata fails inventory validation; a mismatch emits `INVENTORY_LOCALIZATION_AUTHORITY_MISMATCH` and prevents static completion.

Run the gate before context analysis:

```text
game-l10n verify-inventory --workspace <workspace> --primary <primary.json> --independent <independent.json>
```

The attestation is bound to the primary path and content fingerprint. Changing either inventory invalidates downstream context and completion claims.

Startup determines missing inventories from the canonical primary/independent paths or an existing attestation, not from whether `inputs/` contains arbitrary files. A staged original request is not extraction evidence and must not skip the primary inventory dispatch.

Inventory strings, including IDs, metadata values, and object keys, must contain valid Unicode scalar values in UTF-8 JSON. Malformed UTF-8 or unpaired surrogate escapes fail categorically with `E_INVENTORY_INVALID_UNICODE` before a new attestation or identity binding is written. No replacement, dropping, or lossy normalization is permitted. Valid supplementary characters, including JSON surrogate-pair escapes decoded to those characters, retain their exact content and fingerprint behavior.

Every attestation records a metadata-only reconciliation generation, gaps, identity gate, and progress. `incomplete` requires continuation and is not a terminal success. Reconcile from cached scans; mismatch, parser/layout repair, and newly found targets are actionable work, not a user decision or semantic exhaustion. Continue eligible repair and independent work without whole-wave/no-progress barriers. Stop for an actual external blocker, deterministic gate, or user stop; retain the checkpoint and exact remaining codes.

The metadata-only `agreed_target_count` counts active comparison keys whose source, UI/display contract and localization authority agree across both methods. A lower complete blocker score is improvement. At an equal score, an unchanged blocker vector may also report `improved` when this agreement count grows and neither active denominator shrinks; a remaining completeness declaration does not erase that progress. Raw-occurrence growth and fingerprint churn alone do not qualify. Identical input fingerprints report `unchanged`; other uncredited input changes report `changed`. None of these observations grants static completion, waives proof, changes retry routes or creates a job limit.

When an SDK provides `coverage.extraction_evidence`, the attestation reports aggregate scanned/reused file counts, extraction timings, and distinct unresolved dependency counts. The dependency keys bind a method-neutral physical observation, source bytes and reason; only when both methods supply keys can Core report common and one-method-only dependencies. Uncovered active display candidates are those not agreed by the pair, not unreviewed translations or omitted package entries. These diagnostics do not affect inventory identity, completeness or retry policy.

## Evidence-bound scope correction

Canonical occurrence identity and current translation eligibility are separate. A proven correction never deletes, renames, reorders, or changes the source of a checkpoint-bound primary occurrence. Keep historical primary rows in `segments`; the core compares and loads only eligible display rows after validating `coverage.scope_correction`.

An attestation from an older display validator is rechecked against both cached inventories before semantic work. If a control-storage row is still active, status reports `INVENTORY_ACTIVE_CONTROL_STORAGE` and routes `resolve_source_inventory_gaps` to the isolated reconciler. This does not rewrite the historical attestation or authorize a fresh scan. Use each method's cached evidence to determine whether the occurrence is proven non-display or needs a separate display projection, then run pair verification again. A valid unchanged checkpoint acquires the current validation binding without changing its identity or reconciliation generation.

This optional object uses `schema: parr0t.inventory-scope-correction.v1`, `basis`, and `records` only:

- `basis.file` names an immutable pre-correction canonical inventory snapshot in the inventory's own directory. It is a safe ASCII basename ending in `.json`, not an absolute path or traversal. `basis.sha256` binds its complete bytes. Both inventories name the same content-bound basis. On the first correction, its semantic fingerprint must match the prior primary attestation; later corrections either retain that basis or roll forward from the prior attested primary. The basis retains earlier correction history on a rollover.
- Each record binds `canonical_id`, physical `comparison_key`, exact UTF-8 `source_sha256`, `classification`, `projection_ids`, and `evidence`. Duplicate values are not occurrence identity. Every retired occurrence must exist unchanged in the bound canonical basis, and the primary retains all basis IDs, source values, physical keys, and relative order.
- `evidence` contains exactly two independent proofs, each with `method`, `implementation_fingerprint`, and `evidence_sha256`. Method names, implementations, and proof digests must differ. The methods and implementations must match the actual compared inventories. Both inventories must independently agree on the complete correction object; one-sided or conflicting proposals yield `INVENTORY_SCOPE_CORRECTION_MISMATCH`, not static completion.
- `classification: proven_non_display` requires an empty `projection_ids`. Use it only when both independent analyses prove the occurrence cannot reach a player-visible display. Refusal, an absent tool, an uncertain dependency, raw duplication, and a convenient denominator are not evidence of non-display.
- `classification: control_storage_with_projection` requires nonempty `projection_ids` naming separate, uniquely bound eligible display bindings. Each projection row has a matching `display_binding_id`, `display_projection_of` equal to the original physical comparison key, positive source-bound display evidence, and no control-storage role. The pair verifier validates both entire documents before reporting `INVENTORY_SCOPE_PROJECTION_REQUIRED` and retaining an incomplete attestation. A segment consumer still rejects the unresolved scope with `E_INVENTORY_SCOPE_PROJECTION_REQUIRED`; the control value itself never becomes translated output.

Corrected inventories require the ordinary `coverage.display_scope` declaration and source-bound evidence for every eligible row. Its display candidate count and optional static-scope included count describe eligible rows; raw occurrence accounting still includes retained history. Proof hashes bind evidence identity, not its semantic truth: SDKs must retain the actual independent structural, display, dependency, and projection evidence privately and never fabricate a matching ledger.

`verify-inventory` retains the full canonical primary identity binding and fingerprints the full documents and correction evidence. Once correction is introduced, the identity ledger freezes each occurrence's source digest and physical comparison key, including subsequently attested additions; retaining an older basis cannot change an added ID's meaning. Bootstrapping an older ID-only ledger requires the unchanged attested primary or a rollover basis matching that exact attestation. Attestations distinguish `primary_occurrences` / `independent_occurrences` from active `primary_segments` / `independent_segments`. Changed eligibility or valid evidence invalidates affected downstream bindings; historical source/key mutations and silent removal of correction history are rejected. A stale basis, including in a one-sided proposal, cannot certify readiness or a blocked-state hold.

All core context, translation, review, retry, correction, and native-pass ingestion uses `formats.read_segments`, which validates this bound correction and returns only eligible rows in canonical relative order. Adjacency is formed from those display rows, not retired control storage. Runtime coverage uses the same active denominator. SDK application and pack implementations must consume this same eligible scope and preserve retired source/control bytes; a correction is neither a translation failure nor `source_preserved`, and it never supplies runtime evidence or bypasses review, application, or pack verification.

Pair verification reuses each document's validated active scope within the command. It reads, parses, indexes, and fingerprints a shared canonical basis once per resolved path, while each inventory still checks its own expected digest and occurrence/projection bindings. The attestation records the SHA-256 of the exact verified inputs. Reconciler preparation can reuse an unchanged projection baseline after hashing each distinct input once, without reparsing. A new pair verification still reads and validates the complete inputs, and downstream admission still checks freshness.

### Deterministic SDK projection repair

An activated SDK can supply `tools/reconcile_inventory.py`. Reconciler
preparation runs it once over cached evidence before starting a model. The
hook reads UTF-8 JSON on stdin with the keys `primary` and `independent`.
Each contains a map from unresolved physical comparison key to a map of
positive display binding IDs to `[physical comparison key, source SHA-256]`.
Only validated active links are offered. The hook returns a JSON map from
original comparison key to sorted binding IDs. Do not open game files or the
network in the hook.

Core accepts only complete, identical positive link sets from both methods.
Neither a one-sided binding nor matching wording establishes a projection.
The hook cannot retire more rows, change sources or rewrite existing
projection IDs. Core writes a new inventory pair, verifies it once, and uses
the saved attestation as the authoritative pair locator. Previous files and
canonical identities are preserved. SDK consumers use the attested paths,
not a filename heuristic.

On `inventory_verified`, refresh status; no relay was prepared. If there are
remaining gaps, the prepared relay is bound to the new pair and receives only
the remaining repair work. A no-work result is cached for the unchanged pair,
basis and hook. Missing hooks and unsupported or ambiguous gaps retain the
normal isolated repair workflow.

An isolated inventory reconciler receives `worker_execution` in its bound
task. Use its exact `core_runtime.cli_argv` to identify the Core launcher
instead of inferring a checkout or import path, and its `verify-inventory`
argv with only the two complete path placeholders replaced. Use the bound
transport and do not pass SDK-specific options to Core. A verification that
reports incomplete scope can still support a partial result. If execution
aborts before verifying the pair, leave the envelope unwritten and let the
relay seal the ended attempt. Core reports `invalid_envelope_issue_codes`
alongside `malformed_response` when the submitted issue codes fail validation;
it never forwards rejected values or worker error text.

For an activated workspace SDK with `tools/reconcile_inventory_worker.py`,
`prepare-data-plane-task --deterministic-inventory-worker` explicitly selects
that runner in prompt OMP. The helper provides its exact argv and admits one
invocation after verifying the runner digest. The worker cannot substitute
inline Python or write a result with another tool. Ordinary reconciliation
is unchanged when the option is absent; the option cannot rebind a live task.

## Context execution gate

The context state records sealed successful work; refusal, capacity, timeout, malformed output, or operational error remains non-success. Each task binds a configured native route. Worker counts and chunk sizes use safe defaults but honor explicit bounds and available host/model capacity. Completed fragments remain reusable; continue unfinished claims and available independent work without requiring a whole no-progress route cycle. Stale output cannot be resealed, and only core sealing writes successful context state after evidence validation.

Context admission consumes eligible display rows, not raw discovery. The context preflight's `semantic_required` default prevents unsupported context skipping; it is not an eligibility filter. SDKs must resolve format roles before materializing targets and provide only relevant target-bound context, retaining full parse evidence privately. See the mandatory extraction and independent-discovery acceptance rules in `docs/sdk-branch-contract.md`.


## Existing translation review

Use a dedicated full pass when an earlier translation is being improved:

```text
game-l10n review-existing --workspace <workspace> --input <primary.json> --existing <old-results.json> --output <new-results.json>
```

Every existing target is re-triaged; selected flags/risk/sample targets also receive strong review. Integrity checks can trigger a focused repair first, but a matching ID, source hash, or candidate digest cannot substitute for the current verdict. Review completion records both the reused-candidate count and the freshly reviewed reused-candidate count.
Correction and re-review may change target text only. They never rename a target, edit its source, reorder records, or rewrite authority metadata. Glossary, source-language authority, or speaker-profile changes invalidate the affected semantic binding and require fresh current-generation triage; selected targets still require strong review.

## Failure queue and held patches

Retryable failures remain private categorical queue entries. An authorized job may continue pending work under core-selected configured routes; operational/admission errors can advance only to an already-approved native successor using a fresh manifest. A real user pause or stop is respected, but a fresh human request is not a prerequisite for resuming authorized work.

```text
game-l10n resume-failures --workspace <workspace> --input <primary.json> --output <results.json> --user-requested
```
`--user-requested` asserts the existing user's authorization to continue; it is not a requirement for a new interactive confirmation.

`failed` entries remain retryable under configured policy. `source_preserved` and `accepted_unreviewed` remain visibly unresolved; reopening either requires an applicable explicit correction or translation decision, not pretending exhausted work is pending. Repair-needed or unattempted targets are not route exhaustion.

Targets marked `metadata.blocking_on_failure=true` remain blocking when unresolved. By default, do not claim a complete patch while a blocking target is unresolved. When explicitly configured, `failure_queue.allow_incomplete_artifact` may produce a separately identified incomplete artifact; it must not be presented as full coverage or a complete patch. Optional targets may preserve exact source only after actual configured-route exhaustion and remain visibly unresolved.
The legacy `hold_patch_when_blocking_target_fails` setting is not permission to publish incomplete output; use the separate `failure_queue.allow_incomplete_artifact` mode only when an honestly incomplete artifact is explicitly requested.
When `required_for_distribution=false`, a locally integrity-verified checkpoint may complete without satisfying publication requirements. `assurance_completion` must match the actual assurance mode; a lower-assurance checkpoint cannot claim full independent coverage. Full publication remains subject to the requested full-assurance gates.

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
