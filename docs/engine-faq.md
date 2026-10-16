<!-- parr0t-engine-faq:v1 -->
# Engine exception runbook

## Stripped Mono schema requires external generation

- **Trigger:** `U6000_SCHEMA_NODE_REQUIRED`, `U6000_SCHEMA_SCRIPT_UNRESOLVED`, or `U6000_TYPETREE_UNAVAILABLE` for a supported Mono player.
- **Automation boundary:** This SDK consumes embedded or caller-generated typetrees; it has no managed schema generator and cannot infer a missing field layout safely.
- **Stop condition:** Stop before discovery is accepted or any payload is constructed for the unresolved object.
- **Required input or tool:** External schema tooling for the exact observed engine build and immutable managed assemblies, or independently verified equivalent generated node lists and exact assembly bindings.
- **Procedure:** Generate only the required schemas externally in the private workspace. Record the exact engine build and generator identity, flattened node lists, and assembly path/digest bindings using the cache contract in `docs/unity-6000.0.md`. Resolve collection nodes into parser-compatible array/vector types. Keep diagnostics and all assembly/schema material outside the SDK tree.
- **Resume verification:** Pass the cache to discovery; require engine/build and assembly digest checks, successful full typetree reads, independent ordered inventory reconciliation, and later serialized roundtrip verification. A stale or unavailable binding does not permit resume.
- **Do not:** Guess offsets, reuse another compatibility line's cache, silently skip failed objects, modify source assemblies, or treat generated pass-through tools as an adapter.

## Additive fallback requires caller-prepared material

- **Trigger:** A target glyph requirement cannot be satisfied by existing serialized font inputs, or no valid caller overlay has been supplied.
- **Automation boundary:** The SDK validates and serializes an explicit dynamic TMP overlay; it does not select fonts, produce metrics/glyphs, or establish external redistribution rights.
- **Stop condition:** Stop before claiming glyph support or accepting an invalid overlay.
- **Required input or tool:** Caller-prepared same-schema font/TMP/material/atlas trees, noncolliding IDs, valid template and settings identities, font bytes with an explicit digest and required-codepoint list, and any required external licensing decision.
- **Procedure:** Prepare those inputs in the private workspace using suitable external tooling. Preserve template script/shader identities and existing fallback entries. Supply no new external asset dependency; pass the explicit overlay to apply.
- **Resume verification:** Require template/schema, reference-closure, font-digest/cmap, and full serialized roundtrip checks. Require all other original object bytes and original fonts/renderers to remain unchanged.
- **Do not:** Replace original fonts/renderers, infer a font from another job, copy private font assets into this branch, or equate cmap coverage with successful runtime rendering.

## Runtime label or layout evidence is required

- **Trigger:** A serialized census contains image-only controls, unresolved external hierarchy relationships, or a caller asks whether dynamic labels, glyph rendering, or layout are correct at runtime.
- **Automation boundary:** Static object/reference validation cannot inspect runtime state or prove visual coverage. Image content is outside the text scope.
- **Stop condition:** Stop before making a runtime coverage claim or performing any real installation or launch.
- **Required input or tool:** User-provided runtime evidence or explicit authorization for the exact real runtime action under the neutral runtime-approval contract; image work additionally requires an explicit image request.
- **Procedure:** Keep serialized text candidates and unverified image references distinct. Use the neutral runtime authorization workflow for any user-requested real action. Collect only the evidence required for the specific unresolved condition in the external workspace.
- **Resume verification:** Reconcile the new evidence with stable target IDs and the current unchanged artifact. Re-run affected static checks when bindings change. Report remaining image/dynamic-layout limitations explicitly.
- **Do not:** Launch automatically, run OCR, infer text coverage for image-only controls, or widen the compatibility claim to another engine line or backend.
