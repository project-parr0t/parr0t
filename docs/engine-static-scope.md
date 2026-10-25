# Engine static display-scope contract

An engine SDK may build a smaller translation inventory when its public data format exposes stable runtime roots, display bindings, and control-flow references. This is an optional engine capability, not a generic text heuristic. Unsupported engines retain every extracted candidate.

## Claims

Every candidate receives exactly one private classification:

- `statically_reachable`: a fixed entry point or reachable graph node owns the display binding;
- `conservative_retained`: an indirect, dynamic, plugin, variable, or otherwise unresolved reference could reach it;
- `proven_unused`: two independent static analyses agree that no supported runtime root can reach the owning binding.

Only `proven_unused` targets may be absent from the translation inventory. They remain in a private exclusion ledger with stable IDs, categorical reason codes, evidence fingerprints, and counts. Subject matter, provider behavior, filename wording, a test-like name, or raw source duplication is never reachability evidence.

## Inventory binding

Both `parr0t.source-inventory.v1` documents carry the same `coverage.static_scope` object using schema `parr0t.static-scope.v1`. It records:

- the engine capability identity and `static_reachability_only` claim;
- total candidate, included, proven-unused, conservatively retained, and unresolved-dynamic-reference counts;
- two distinct analysis method names and implementation fingerprints;
- whether the methods agree and one fingerprint binding their evidence.

Candidate count equals included plus proven-unused. Conservative targets are included. Static-scope exclusions carry `scope: static_reachability`, a positive `target_count`, `affects_user_visible_text: false`, and their evidence fingerprint. The verifier rejects incomplete accounting, one-method exclusion, or disagreement between inventories.

## Engine implementation rules

1. Start from format-defined runtime entry points and active registries, not recursive filename discovery alone.
2. Follow fixed control-flow and resource references. Keep every conditional branch unless the format itself proves it impossible.
3. Treat computed IDs, variables, reflection, eval, plugin calls, script dataflow, missing resources, and unsupported commands as conservative retention for the affected category.
4. Do not launch the game or run scripts. Static text scope never inspects images or uses OCR. Only a separately user-requested image task may follow `docs/image-localization.md`; it cannot satisfy static text coverage or runtime observation.
5. Preserve source order and stable target IDs for included targets. Store the excluded-ID ledger only in the private workspace.
6. Raw-equal strings remain separate occurrences. An SDK may fan out one translation only when it emits an explicit canonical display binding and proves identical semantic context, UI slot contract, validation constraints, and source payload.

RPG-style map/event engines can commonly implement this contract for fixed start maps, literal transfers, common-event calls, autorun/parallel roots, encounter tables, and active plugin registries. Binary or reflection-heavy engines should report the capability unavailable or conservatively retain uncertain categories rather than inventing a completeness claim.
