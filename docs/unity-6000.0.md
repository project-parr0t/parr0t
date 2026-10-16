# Unity 6000.0 Mono SDK

This branch adds an offline adapter for the Unity 6000.0 Mono compatibility line.
It edits explicitly selected serialized string fields, single-physical-line TSV
TextAsset cells, and individual unsigned CLR MethodDef `ldstr` instructions. It
can add caller-prepared dynamic TextMesh Pro fallback objects while preserving
existing fonts, renderers, settings fields, and all other original object bytes.

Install the branch with `python -m pip install -e .` and its engine libraries with
`python -m pip install -r profiles/unity-6000.0-requirements.txt`.
The SDK owns its module entry point and requirements file; promotion adds only
new engine-owned paths and leaves all files inherited from main unchanged.
The tested library versions are UnityPy 1.25.0, dnfile 0.18.0, dncil 1.0.2, and
fonttools 4.67.0. Synthetic fixtures are generated from scratch during tests.

## Supported boundary

The player must contain one unambiguous data directory, a Mono managed layout,
and readable Unity 6000.0 build-version evidence. IL2CPP, other Unity lines,
signed assemblies, mixed/native CLR images, PE overlays, and final-section BSS
tails are rejected. No loader, runtime hook, installer, launch command, OCR,
image translation, glyph rasterizer, or general layout solver is supplied.

Discovery scans serialized candidate files under the selected data directory:
asset/bundle/unity3d extensions, level-prefixed filenames, the global manager,
and the two standard extensionless built-in resource names. Other extensionless
files, split-file groups, encrypted/custom containers, remote content, and
runtime-generated labels are outside this scan contract. Managed assemblies
are scanned only when explicitly named with `--managed-file`. Parse/schema
failures block discovery; it never reports a partial scan as complete.

Candidate strings are not automatically human text. Discovery output is private
data-plane material. Every candidate must be explicitly classified before apply.
The structural UI census recognizes standard serialized control, text, image,
and transform classes. It partitions serialized files, follows only local
hierarchy pointers, and reports unresolved external/missing relationships.
Image content remains unverified, including image-only controls; this census
does not establish complete UI coverage or runtime text coverage.

## Workspace commands

First use the neutral selection/activation workflow for the exact branch identity.
All commands require that active checkout, a configured read-only source root,
and an external workspace that does not overlap the source or this repository.
Inputs and new outputs stay in that workspace; collisions fail. CLI output
contains categorical states, counts, and artifact paths only.

```text
python -m game_l10n.unity6000 discover --workspace <work> --output discovery.json --schemas schemas.json --managed-file <relative-assembly> --table-file <relative-asset>
python -m game_l10n.unity6000 unpack --workspace <work> --inventory discovery.json --selection selection.json --schemas schemas.json --output segments.json
python -m game_l10n.unity6000 apply --workspace <work> --inventory discovery.json --selection selection.json --results results.json --schemas schemas.json --output prepared
python -m game_l10n.unity6000 pack --workspace <work> --prepared prepared --results results.json --schemas schemas.json --output patch.zip
python -m game_l10n.unity6000 verify --workspace <work> --inventory discovery.json --results results.json --schemas schemas.json --archive patch.zip
```

`--schemas` is optional when all required typetrees are embedded. `--table-file`
selects TSV interpretation for TextAssets in that container; individual row and
column IDs still require explicit classification. It does not translate every
column. `unpack` produces the neutral framework's segment JSON. Perform two
independent ordered inventories, reconcile scope, then complete the core context,
translation, fallback, review, and static completion gates before packing.

The selection document has schema `parr0t.unity6000.selection.v1`, the exact
`inventory_fingerprint`, and `entries` containing every discovered `id` exactly
once. Each entry has `classification` equal to `confirmed_text`, `nontext`, or
`unresolved`. Unresolved candidates block unpack/apply; they cannot be silently
excluded. Classifying table identifiers, code fields, or structural strings as
nontext is a data-plane decision requiring evidence. Location IDs bind the
container path, serialized-file identity, path ID, field path or method/IL offset.
Protected UnityEvent names, script identities, object names, GUIDs, and pointer
structures cannot be translated even if selected.

`apply` accepts core JSON/JSONL results with exact selected IDs and sources. It
preserves core protected tokens, numeric format placeholders, escaped controls,
markup order, and raw control characters. Named/numbered placeholders may be
reordered without changing their spelling or format. Empty replacements of
visible text fail. TSV row/column order and existing physical line endings are
preserved; quoted multiline cells are rejected.

Apply builds a new prepared directory with replacement files and a private
receipt. Pack independently reconstructs those files from the bound source,
selection, and exact result bytes, compares them with the prepared payload, then
builds a deterministic sparse delta ZIP. Verification rejects traversal, path
aliases, duplicate/unowned members, stale sources, bad lengths, and changed
hashes, and reconstructs both forward and restore bytes in memory. Original
files are never opened for writing. The archive contains reversible byte deltas
and manifests; it is not a real installation or restore command.

Only after verification does pack call the core `record_verified_pack_artifact`
API with the actual result file. A current static completion attestation must
bind that file's path, size, and hash; otherwise no successful core pack state
is recorded. Discovery/apply by themselves never confer completion. Verification
does not run the player or establish runtime layout, glyph, or dynamic-label coverage.

## Generated typetree input

The SDK consumes generated schemas; it does not generate them. A stripped Mono
schema requires external engine tooling or caller material under the matching
[engine runbook](engine-faq.md). Cache schema is
`parr0t.unity6000.typetrees.v1`, with `engine: unity`, `series: 6000.0`, exact
`unity_version`, and nonempty `generator` identity. `assemblies` maps the exact
MonoScript assembly name to `{path, sha256}` for source-root-relative managed
inputs. `nodes` maps `assembly|namespace.Class` to flattened UnityPy TypeTreeNode
lists. Omit nested `m_Children` members from those flattened lists. Collection
nodes must use parser-compatible array/vector types rather than a primitive type
name for an array. Never import a cache from another version or another assembly
build. Cache schemas and every assembly digest are checked on each command.
External MonoScript references resolve only to unambiguous scanned source asset
files; there is no working-directory or arbitrary-filesystem fallback.

## Caller-prepared TMP overlays

Apply accepts `--overlays <workspace-json-list>`. Each overlay uses schema
`parr0t.unity6000.tmp-overlay.v1`, `file`, `serialized_file`, `settings_id`,
four-role `templates` and new positive `ids` maps (`native`, `tmp`, `material`,
`atlas`), complete caller-prepared `trees`, `font_sha256`, and nonempty
`required_codepoints`. Template class identities and schemas must match the
existing Font, MonoBehaviour, Material, and Texture2D objects. New IDs must not
collide. TMP script and material shader pointers retain their template identity.
The new TMP references the new native font, material, and atlas; material's main
texture points to the new atlas. Local references must resolve, and new external
references or streamed asset dependencies are forbidden.

The template must support dynamic atlas population with empty glyph/character
tables. The supplied native font bytes must match their digest and contain every
requested codepoint in the font cmap. This checks static font availability only;
the caller is responsible for preparing valid metrics, creation settings, atlas
and material properties, and obtaining redistribution rights externally. The
SDK does not choose a font, calculate face metrics, generate glyphs, inspect
atlas pixels, or claim runtime rendering. Existing fallback entries are retained
and the new TMP pointer is appended. Only the settings object and newly cloned
objects change; all original font and renderer bytes remain unchanged.

## Validation and problem classification

Run `python -m unittest tests.test_unity6000 -v` with the SDK requirements installed.
The suite exercises real generated SerializedFile/CLR/font bytes, targeted
roundtrips, shared literal preservation, version/cache isolation, malformed and
stale bindings, protected fields/tokens, additive object references, UI census,
source/output boundaries, archive tampering, and core completion binding. The
synthetic font and minimal CLR image establish bounded serialization behavior,
not universal runtime compatibility. Also run the core suite, privacy scanner,
and engine-runbook checker under the branch acceptance contract.

Deterministic malformed inputs, collisions, stale bindings, protected-field
changes, unsupported signed/native images, and archive failures are
`automated_fix` rejections with regression tests. External schema preparation,
valid caller TMP material, and runtime visual evidence are `engine_runbook`
boundaries. Title-specific evidence is `private_only`; unknown causes remain
`unresolved` outside this repository. No private history is part of this contract.
