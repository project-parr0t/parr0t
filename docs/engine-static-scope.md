# Engine static display-scope contract

An engine SDK may build a smaller translation inventory when its public data format exposes stable runtime roots, display bindings, and control-flow references. This is an optional engine capability, not a generic text heuristic. Raw literal discovery is a candidate collection step, not proof that every literal is displayed. Without display-scope evidence, retain uncertain display candidates rather than declaring them internal or demanding translation of every raw string.

## Claims

Keep raw occurrence accounting separate from display-target accounting. Cache physical provenance, format roles, display bindings, and dependency evidence during each method's single source pass. Classify raw occurrences privately as display-bound, uncertain display dependencies, or proven non-display; account for their categories and counts without deduplicating equal values. Proven internal identifiers, control values, engine/library internals, and debug-only strings need no translation when structural evidence proves they cannot reach a player-visible display binding. Names, modules, or library membership alone are not that evidence. Player-visible system messages remain targets.

Every display candidate receives exactly one private classification under the existing static-scope contract:

- `statically_reachable`: a fixed entry point or reachable graph node owns the display binding;
- `conservative_retained`: an indirect, dynamic, plugin, variable, or otherwise unresolved reference could reach it;
- `proven_unused`: two independent static analyses agree that no supported runtime root can reach the owning binding.

Only `proven_unused` display candidates may be absent under this reachability claim. They remain in a private exclusion ledger with stable IDs, categorical reason codes, evidence fingerprints, and counts. Proven non-display raw occurrences are accounted for separately through the existing `coverage.exclusions` records (`reason_code`, `affects_user_visible_text: false`, `target_count`, and `evidence_fingerprint`), not added to the static display-candidate denominator. Subject matter, provider behavior, filename wording, a test-like name, or raw source duplication is never reachability evidence.

Core-bound new inventory tasks also use `coverage.display_scope` (`parr0t.display-scope.v1`) to separate raw occurrence accounting from translation candidates. It records raw occurrences, display candidates, control-only and unclassified occurrences, plus a nonempty evidence fingerprint. Every included segment supplies `metadata.display_scope_evidence` with kind `positive_display_binding` or `possible_display_dependency` and its evidence fingerprint. The core checks schema, non-negative counts, candidate/segment equality, and evidence presence; these checks do not establish SDK correctness, engine reachability, physical provenance, or runtime completeness. SDKs own those claims and must retain uncertain actual display candidates. Legacy snapshots may omit this declaration.
New-contract scope evidence additionally binds each target's UTF-8 source digest and a stable `display_binding_id`; control-valued storage needs an explicit display projection. Unclassified raw occurrences remain outside translation scope until the SDK establishes a possible display dependency. This is not permission to exclude uncertain actual display candidates or to infer visibility from module names or raw-string presence.

## Inventory binding

Both `parr0t.source-inventory.v1` documents carry the same `coverage.static_scope` object using schema `parr0t.static-scope.v1`. It records:

- the engine capability identity and `static_reachability_only` claim;
- total candidate, included, proven-unused, conservatively retained, and unresolved-dynamic-reference counts;
- two distinct analysis method names and implementation fingerprints;
- whether the methods agree and one fingerprint binding their evidence.

Display-candidate count equals included plus proven-unused. Included count must equal the inventory's segment count, and conservative targets are included. Raw literals already proven non-display do not inflate these counts. Static-scope exclusions carry `scope: static_reachability`, a positive `target_count`, `affects_user_visible_text: false`, and their evidence fingerprint. The verifier rejects exclusions without scope tracking, incomplete accounting, one-method exclusion, or disagreement between inventories. Unresolved-reference count measures references, not targets: one reference can conservatively retain many targets.

## Engine implementation rules

1. Start from format-defined runtime entry points and active registries, not recursive filename discovery alone.
2. Follow fixed control-flow and resource references. Keep every conditional branch unless the format itself proves it impossible.
3. Treat computed IDs, variables, reflection, eval, plugin calls, script dataflow, missing resources, and unsupported commands as conservative retention for the affected category.
4. Do not launch the game or run scripts. Static text scope never inspects images or uses OCR. Only a separately user-requested image task may follow `docs/image-localization.md`; it cannot satisfy static text coverage or runtime observation.
5. Preserve source order and stable target IDs for included targets. Store the excluded-ID ledger only in the private workspace.
6. Raw-equal strings remain separate occurrences. An SDK may fan out one translation only when it emits an explicit canonical display binding and proves identical semantic context, UI slot contract, validation constraints, and source payload.
7. Derive display scope and category accounting from cached parse/reference evidence, not repeated source reads. The independent method must construct its own evidence without reading or deriving from the counterpart inventory. Reconciliation reuses both completed scans and resolves only reported gaps; it cannot rewrite checkpoint-bound primary identity or order.

RPG-style map/event engines can commonly implement this contract for fixed start maps, literal transfers, common-event calls, autorun/parallel roots, encounter tables, and active plugin registries. Binary or reflection-heavy engines should report the capability unavailable or conservatively retain uncertain categories rather than inventing a completeness claim.
