# Unity 2022.3 Mono text SDK

The profile under `profiles/sdk-unity-2022.3` implements source-bound extraction and copied-payload rewriting for Unity `2022.3.x` Mono players. It rejects IL2CPP and other Unity series. Format support is not a claim that every runtime-generated string or every title-specific reference is statically understood.

## String extraction capabilities

- PE32/PE32+ CLR metadata, CIL literals, typed fields, call/field display dependencies and embedded `.resources` v1/v2 strings. Resource names and lookup keys remain identifiers; a physical literal also proven to reach display stays a display candidate, but changing its shared identifier use is rejected rather than rewriting game logic.
- Standalone little-endian serialized files v22 and UnityFS v6–8 containers, with none/LZMA/LZ4 block codecs. Native UI fields, physically bound MonoScript/custom fields, nested arrays and PPtrs retain exact object/field identities. JSON/INI TextAsset values retain syntax-bound entry locations; raw presence alone does not prove display use.
- Windows menu/dialog caption roles and Unity `app.info` product/window title. String/message-table membership is raw census, not display evidence: only supported resource-to-UI consumer bindings admit their referenced IDs. PE64 native import-IAT/unwind/operand-bound UI references retain possible dependencies. Only exactly bound UTF-16 native arguments support selected-use relocation; unproven projections remain pending.
- Existing opaque font replacement and pure glyph/layout screening remain available below. The text scanners never decode image pixels or audio and never perform OCR.

Managed method-driven and heap/use-driven enumeration use fresh readers. Serialized primary extraction uses exactly UnityPy 1.25.4; the independent path uses first-party byte/typetree decoding. Public format constants and low-level CLR/native semantic machinery are shared: do not describe these as independently implemented decoders. Neither method derives targets or parse evidence from the counterpart inventory.

## Prepare before either inventory

Use Core containing the extractor-preparation protocol. Propagate Core through the official branch updater before activating a combined SDK; importing a different Core over an already activated runtime is not deployment and is correctly rejected. No merge, publication or game action is implicit.

For a deficient/extended SDK, activate the exact selected SDK first, then prepare its isolated implementation task:

```text
game-l10n prepare-data-plane-task --workspace <workspace> --worker-role source_probe --purpose extractor_preparation
```

Inside that worker, run the activated profile's `prepare.py --workspace <workspace>`, repeating `--dependency-path <authorized-exact-cache>` when needed. It stages implemented tools, real synthetic format binaries, fixed expectations and `tools/extractor-preparation.json`. Source-probe requirements, when present, must match supported format/capability codes; unsupported requirements are not silently dropped. Use only separately authorized exact dependencies from `profiles/unity-2022.3-requirements.txt`; no tool downloads or installs them.

Reactivate the same selection if its activation-bound implementation changed, then run `game-l10n check-extractor-preparation --workspace <workspace> --relay <bound-relay>`. Core executes checks and binds their input/output bytes, implemented files, requirements and current activation. Missing, stale or failing readiness blocks both new inventory roles. Preparation checks cover extraction, duplicate physical bindings, independent agreement and selected rewrite/restore; they do not establish full-game coverage or semantic quality.

## Private workspace interface

Bind `GAME_L10N_WORKDIR` and use `unpack.py INPUT_GAME_ROOT OUTPUT_DIRECTORY`. `GAME_L10N_INVENTORY_METHOD` selects `primary` or `independent`; a call runs only that method. Outputs remain outside the read-only player tree: immutable source snapshot/manifest, `evidence.sqlite3`, and `inventory.json`. Each scan is bound to method, source and implementation bytes; unchanged scans are reused. Shared proofs are stored once while equal-valued physical occurrences remain distinct. Inventories are streamed directly to files, not serialized through model/tool arguments.

The inventory records file scan and cache reuse counts and elapsed times for PE/managed scanning, serialized resolution, and inventory emission. Both methods hash unresolved physical observations without using display text or traversal order. These metrics diagnose work remaining; they are not coverage or runtime proof.

CLR method/field resolution uses image-local declaration indexes and token caches. Cross-assembly proof is method-local and uses only digest-bound assemblies from that method's snapshot or pack binding; changing its peer set invalidates dependent scan entries.

Private raw caches preserve non-display UTF-16 code units losslessly using escaped JSON. Actual display candidates still require valid Unicode scalar values; invalid source is held, never replaced or normalized away. Known image-content dependencies remain recorded as unverified non-text work, so they cannot deadlock text inventory before the separately requested image phase.

Canonical IDs use physical method/IL, resource-entry or object/field/table-entry bindings, never source value, method label or incidental heap/payload relocation. Native operand-use identity survives relocation. Unknown raw strings are accounting data, not automatic translation targets or completeness gaps. Actual unresolved display dependencies retain candidates and hold coverage. Two independent inventories and Core reconciliation still precede context/translation/review; extraction readiness never replaces those barriers.

Exact direct calls to a uniquely bound static display helper can establish a caller's literal without guessing a name or a virtual target. The supported helper passes the argument unchanged to a recognized UI setter, and the caller must not use that same literal occurrence for control. Ambiguous assemblies, other helper bodies, and unbound sink parameters remain pending. Decoded RIP-relative loads of known native display IAT entries retain possible scope even when invocation is register-indirect; unrelated native functions are not promoted. Scanner-declared static incompleteness remains a coverage hold when its file has relevant display evidence, including after cache reuse.

A consumer-side audit checks nonempty typed UI text slots and direct `ldstr` operands of known managed setters against each inventoried result. A missing positive occurrence keeps coverage open even when both methods omit it; the audit does not invent a target or infer runtime reachability for unsupported sinks.

Windows `STRINGTABLE` and `MESSAGETABLE` values without consumer evidence remain unclassified in the private cache and never enter translation/context/review segments. Equal text in another resource slot does not inherit a binding. No module, DLL, engine or debug-name exclusion is used. Menu item captions, dialog/control captions and `app.info` product titles have their own format-defined display roles, independently of generic table membership.

The deterministic resource consumer reader supports PE32+ AMD64 functions with validated unwind boundaries and the shared first-party bounded decoder. Within a decoded straight-line prefix it tracks integer register arguments, exact RIP-relative writable buffers, 64-bit register copies, call clobbers and the exact outgoing `RSP+0x20` buffer slot. It joins `LoadStringW` resource IDs or `FormatMessageW` message IDs to that same output buffer passed to a recognized Unicode UI text argument, such as `SetWindowTextW` or `MessageBoxW`. `LoadStringW` requires a positive buffer capacity. `FormatMessageW` requires `FORMAT_MESSAGE_FROM_HMODULE`, without `FROM_STRING` or `ALLOCATE_BUFFER`; inserts/control tokens remain protected during packing. A local module handle is established by `GetModuleHandleW/A(NULL)` for an executable or an exact matching module filename in read-only data. `FormatMessageW` also supports its documented null-source current-executable case. A null handle in a DLL never binds that DLL's resource table. Resource ID bindings admit existing language variants as may-display alternatives, not a claimed runtime language choice.

Branches, unknown memory effects, unsupported instructions, unresolved IDs/modules, stack-local/aliased output buffers, allocation mode, ANSI resource loaders, indirect/dynamic calls, interprocedural propagation and external/system message sources are not inferred. Actual unresolved display dependencies retain explicit pending coverage rather than promoting every table value. Pre-enumerated loader callsites remain pending even when a branch prevents processing them; direct-call graph adjacency nominates unresolved interprocedural loader scope but never proves value flow or promotes table entries. A decoded loader and UI consumer with unresolved flow, a bound ID missing from resources, and `FROM_SYSTEM` fallback remain pending; raw resource presence alone does not hold coverage. The snapshot producer reconciles overlapping native-scanner gaps only at resource-proven loader/module callsites, `FormatMessageW` output callsites and exact UI argument registers. Unrelated callsites/registers and the broader native scanner's static-completeness limitation remain pending. The broader native dependency reader also retains unsupported architecture, missing boundaries, dynamic API and indirect-call evidence. These are bounded may-use proofs, never whole-program reachability or runtime visibility verification. Managed flow eligibility and `display` versus `uncertain + display_dependency` admission are unchanged.

Resource eligibility/source-integrity regressions use only generated synthetic PE bytes, including the real content-bound snapshot producer, both scanners, reconciliation, cache reuse and Core `read_segments` consumer. Run `python -m unittest tests.test_unity_resource_eligibility tests.test_unity_inventory tests.test_unity_managed -v` from the SDK checkout.

A display-bound TextAsset with JSON/INI entries keeps both payload and entry occurrences in raw accounting. The asset reference alone does not select whole-payload rendering versus parsed-value rendering: only one possible payload candidate is exported, marked `display_projection_required`, and coverage/packing remain held. Parsed entries are not independently promoted without a proven consumer, so overlapping payload/entry rewrites are never invented.

For stripped managed UI objects, a complete typed field before an unsupported field can be extracted when its display role is independently proven. Both readers stop at the first unsupported field and leave it pending; malformed wire data does not qualify. Packing permits only proven display spans in a partial object and preserves its unknown suffix byte-for-byte.

For `pack.py REQUEST_JSON OUTPUT_DIRECTORY`, bind `GAME_L10N_STAGE=pack`. The request supplies `game_root`, workspace-contained `segments_path`, `reviewed_path`, `source_manifest_path`, and optional explicit `managed_types`/physical script bindings for stripped custom objects. Assemble large typed maps natively from the method's cache, never paste them through a model RPC. The packer uses Core's eligible segment scope, exact reviewed/source-preserved alignment, protected tokens and bound source hashes. It emits only copied replacement files and `pack-report.json`; it does not install, launch, alter source files, strip signatures or create behavior-changing hooks.

Startup identity values in `app.info` and `PlayerSettings.productName` require a separate display projection. Direct managed literals in identifiable CLR/Unity initialization methods and their locally bound callees are not rewritten. Native operand relocation rejects identified entry-path functions and unresolved native startup boundaries; this is a bounded guard, not a runtime startup proof.

Signed/unsafe write layouts, ambiguous bindings, unknown serialization, unresolved reflection/dispatch/resource culture and unproven native projections remain explicit pending/rejection cases. UnityFS decoding bounds and supported layout limits are categorical, not permissions to omit affected display scope. Real runtime behavior, font ingestion, glyph rendering and UI clipping require separate authorized observation and remain unverified here.


## Bounded standalone library

Import `unity_serialized` from the profile's `tools` directory. It requires the same private `UnityPy==1.25.4` dependency. `environment(raw_bytes)` accepts only standalone serialized files with version 22 and little-endian metadata; it rejects UnityFS containers and other layouts. This is a Unity 2022.3 capability bound, not support for every engine version that happens to use v22.

- `pack_string(text)` encodes a UTF-8 length-prefixed, four-byte-padded string. `string_at(payload, offset)` parses one explicitly bound string and returns `(text, padded_end)`; malformed boundaries and invalid UTF-8 are rejected.
- `unique_boundary(payload, encoded_bytes)` returns one exact `(start, end)` match and rejects missing, empty or ambiguous bindings. The caller must establish which field the bytes represent.
- `rebuild_serialized(raw_bytes, {object_id: replacement_bytes})` relocates opaque payloads while retaining the original metadata, gaps and unowned object bytes. Only table offsets, sizes and the file-size header change. It verifies object structure, replacement bytes, unowned payloads and protected metadata on round trip. Invalid regions and unknown IDs are rejected. It neither discovers translation targets nor serializes unrelated objects.
- `replace_font_payload(raw_bytes, object_id, font_bytes, family_names)` changes only an explicitly selected embedded `Font` object's `m_FontData` and `m_FontNames`, verifies its typetree round trip and uses the same opaque rebuild. The caller supplies legally usable font bytes and names; no font or license is bundled. A successful round trip does not prove runtime ingestion or glyph rendering.

For TextMeshPro, `unity_tmp.validate_tmp_renderer` checks a bound renderer's serialized font and material pointers, ordered fallbacks, character-to-glyph lookup, atlas rectangles and pixel ink. Use it on the delivered asset bytes. `unity_tmp.apply_static_tmp_font` accepts caller-approved Alpha8 SDF pages and face metrics, creates renderer-local static pages, retains the original font as a fallback, and validates the resulting bytes before returning them. Unbound external assets and unsupported layouts fail explicitly; no font is fetched, bundled, or automatically applied to an ordinary text pack.

Keep target maps, selectors, paths, source/target text and all build orchestration in the private workspace. Callers write returned bytes only to new workspace artifacts and integrate the framework's completion and verified-pack gates themselves. The helpers do not install or launch anything.

## Pure UI screening library

`unity_layout` has no UnityPy, font-file or other external dependency. Supply public-shaped values from a private inventory; the helper does not inspect assets. `Rect(parent, anchor_min, anchor_max, size_delta)` represents a node; a node with `parent=None` represents the canvas reference rectangle. `resolve_rect_size(rectangles, node_id, canvas_size)` resolves child anchor spans plus size deltas through the full parent chain. Missing parents, cycles, nonfinite values and non-positive dimensions are rejected. Rotation, scaling, pivots, driven layout groups and runtime transforms are outside this calculation.

`FontMetrics(units_per_em, line_height, advances, fallback_advance)` receives a codepoint-to-advance mapping and conservative line-height units chosen by the caller. `TextLayout(font_size, wrap=True, truncate_vertical=True, best_fit=False, best_fit_min=1, best_fit_max=1)` defines the screening bounds. `screen_text(text, rect_size, metrics, layout, strip_rich_text=False)` returns the tested size, line measurements, conservative overflow state, missing codepoints and `runtime_verified=False`. Best fit selects the largest fitting integer size; if no size fits, it reports the minimum size and overflow. Optional tag stripping is approximate. A minimal synthetic use is:

```python
from unity_layout import Rect, FontMetrics, TextLayout, resolve_rect_size, screen_text

rectangles = {"canvas": Rect(), "label": Rect("canvas", size_delta=(-20, -20))}
size = resolve_rect_size(rectangles, "label", (200, 100))
metrics = FontMetrics(1000, 1200, {ord(ch): 500 for ch in "SOURCE"}, 1000)
result = screen_text("SOURCE", size, metrics, TextLayout(16))
```

The estimate uses codepoint advances and conservative character wrapping. It does not model Unity word wrapping, shaping, kerning, full rich-text semantics, runtime font fallbacks, dynamic text or nonreference screen sizes. Missing glyphs and overflow are screening findings, not authorization to change authored typography. Actual rendering and runtime coverage require separately authorized user-run observations under `docs/engine-faq.md`.

Run the public profile's `tools/selftest.py` for dependency-free interface checks. The focused synthetic suite is `python -m unittest tests.test_unity_serialized tests.test_unity_layout tests.test_unity_2022_3_profile -v` from the SDK checkout.
