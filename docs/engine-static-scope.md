# Engine static display-scope contract

Separating raw occurrences from eligible display targets is mandatory for every SDK, as specified in `docs/sdk-branch-contract.md`. An SDK may additionally build a smaller translation inventory when its public format exposes stable runtime roots, display bindings and control-flow references. This additional reachability pruning is optional, not a generic text heuristic. Raw literal discovery is not proof of display use. Retain actual uncertain display dependencies, but do not promote every unclassified raw string into that category.

After SDK activation and before either inventory, use a bounded locale probe.
Verify the selected engine's active loader, source format, and reversible
target-locale apply path. An applicable locale route enumerates only locale
values; there is no screen review or fallback source extraction. Only a
verified unavailable route permits display extraction. An inconclusive probe
stays pending, even when additional screen review has been waived.

## Claims

Keep raw occurrence accounting separate from display-target accounting. Cache physical provenance, format roles, display bindings, and dependency evidence during each method's single source pass. Classify raw occurrences privately as display-bound, uncertain display dependencies, or proven non-display; account for their categories and counts without deduplicating equal values. Proven internal identifiers, control values, engine/library internals, and debug-only strings need no translation when structural evidence proves they cannot reach a player-visible display binding. Names, modules, or library membership alone are not that evidence. Player-visible system messages remain targets.

Every display candidate receives exactly one private classification under the existing static-scope contract:

- `statically_reachable`: a fixed entry point or reachable graph node owns the display binding;
- `conservative_retained`: an indirect, dynamic, plugin, variable, or otherwise unresolved reference could reach it;
- `proven_unused`: two independent static analyses agree that no supported runtime root can reach the owning binding.

Only `proven_unused` display candidates may be absent under this reachability claim. They remain in a private exclusion ledger with stable IDs, categorical reason codes, evidence fingerprints, and counts. Proven non-display raw occurrences are accounted for separately through the existing `coverage.exclusions` records (`reason_code`, `affects_user_visible_text: false`, `target_count`, and `evidence_fingerprint`), not added to the static display-candidate denominator. Subject matter, provider behavior, filename wording, a test-like name, or raw source duplication is never reachability evidence.

Each fresh inventory binds its route through `coverage.text_source` and its
accounting through `coverage.display_scope`; see `docs/coverage-gates.md`.
New display-route targets require a positive format field or render argument
binding, including the exact slot and nested path. An unresolved actual
dependency stays pending outside the active rows; a merely unknown occurrence
stays in the private raw accounting. Core validates the contract, not the
underlying engine observation. Previously validated snapshots retain their
original contract.

Eligibility is decided before inventory materialization, context chunking, translation and review. Supported display fields and consumer traces establish candidates; editor/control/diagnostic storage does not become eligible from its language, container membership, a caller's arbitrary label or a matching hash. Retain unclassified occurrences in the private census and resolve their roles locally from cached format evidence. An actual unresolved display consumer remains an explicit pending dependency, not a silently excluded target.

Only eligible rows are semantic inputs. Preserve full parse/command evidence privately and attach only target-bound speaker, branch, state, composition and other relevant facts. Never pass the entire census to context analysis to discover which rows should have been filtered.

Each independent method discovers target locations and eligibility from source independently. Shared format primitives are allowed; emitting the same discovered segment list twice with different method labels is not independent extraction. Engine fixtures must show both exclusion and visible-text preservation, byte-safe application and detection of a one-method omission.


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
3. Keep positively bound display values whose reachability is uncertain. For computed IDs, variables, reflection, eval, unknown plugin calls, or unsupported commands that prevent a positive binding, report the affected actual dependency as pending; never promote an entire container.
4. Do not launch the game or execute game scripts. A bounded local SDK, parser, compiler, or build tool may run when needed for the authorized static task, using validated local inputs and private outputs; this does not authorize third-party network access, installation, game mutation, or launch. Static text scope does not inspect images or use OCR. Separately requested image inspection or translation follows `docs/image-localization.md` and cannot satisfy static text coverage or runtime observation.
5. Preserve source order and stable target IDs for included targets. Store the excluded-ID ledger only in the private workspace.
6. Raw-equal strings remain separate occurrences. An SDK may fan out one translation only when it emits an explicit canonical display binding and proves identical semantic context, UI slot contract, validation constraints, and source payload.
7. Derive display scope and category accounting from cached parse/reference evidence, not repeated source reads. The independent method must construct its own evidence without reading or deriving from the counterpart inventory. Reconciliation reuses both completed scans and resolves only reported gaps; it cannot rewrite checkpoint-bound primary identity or order.
8. Keep occurrence identity separate from evidence storage. Cache shared method, file and type proofs once and reference their content fingerprints from occurrence records; never deduplicate physical occurrences or discard their source/location/display bindings. Large deterministic parse caches should be serialized directly by native parsing code from file inputs, not embedded in generated prompts, Python literals or tool-RPC text payloads. This is a lossless storage/transport rule, not a target-count, artifact-size or whole-job usage limit.

RPG-style map/event engines can commonly implement this contract for fixed start maps, literal transfers, common-event calls, autorun/parallel roots, encounter tables, and active plugin registries. Binary or reflection-heavy engines should report the capability unavailable or conservatively retain uncertain categories rather than inventing a completeness claim.
