# Unity 6000.4 Mono: explicit binding profile

`profiles/sdk-unity-6000.4` supplies a bounded data-plane implementation for the
Unity 6000.4 compatibility line. It uses explicit private bindings rather than
guessing which strings are translation targets. Select and activate the exact
SDK identity through the core selection contract before using it.

The supported mechanisms are:

- UnityFS member reconstruction with preserved entry names/flags and byte-exact
  opaque resource streams and unowned members;
- v22 little-endian serialized files whose recorded version starts `6000.4.`;
- string fields selected by object ID and typed field path, after a byte-exact
  original type-tree round trip;
- nonoverlapping, four-byte-aligned length-prefixed UTF-8 string spans selected
  by object ID and exact original start/end offsets;
- Mono managed CIL `ldstr` replacement selected by full method token, full method
  signature, instruction offset and exact source string;
- explicit-template embedded Font/TMP/material/dynamic atlas groups and appended
  TMP fallback references, verified by serialized readback.

Every path requires exact source bindings. Missing, ambiguous, stale or
unsupported bindings fail. No player file is written, installed or executed.

## Dependencies

The branch declares `UnityPy==1.25.4` in
`profiles/unity-6000.4-requirements.txt`; validation installs it only into the
external audit state when dependency provisioning is requested. Keep generated
schemas outside this repository. Embedded type trees may be used
directly; stripped behaviours require caller-supplied matching nodes or an
explicit private `typetree_dll_directory` containing the matching copied managed
assemblies. The profile uses UnityPy's type-tree generator for that directory.
It does not search the player for assemblies or acquire them automatically.
If the host cannot complete the transitive pure-Python `tpk_ar` PEP 517 build,
build its wheel in external tooling storage and expose that directory through
`PIP_FIND_LINKS` before provisioning. Do not vendor the wheel or package source.

Managed application additionally needs Mono.Cecil 0.11.6 and a compatible C#
compiler/runtime with `System.Web.Extensions`. Compile the provided source into
the private tooling directory, keeping `Mono.Cecil.dll` beside the executable:

```text
csc /nologo /target:exe /reference:Mono.Cecil.dll /reference:System.Web.Extensions.dll /out:ManagedLiteralWriter.exe SDK_TOOLS/ManagedLiteralWriter.cs
```

Set `GAME_L10N_MANAGED_WRITER` to that executable's absolute path. This executable
reads/writes managed assembly copies as data. It never loads the target assembly
for execution. Strong-named assemblies and assemblies with multiple modules are
rejected; IL2CPP/native binary modification is unsupported. No dependency binary
or font is bundled in the SDK.

For the synthetic managed regression tests, set `GAME_L10N_CSC` to the compiler
and `GAME_L10N_CECIL` to the exact Mono.Cecil DLL. Their required file bindings
are declared in `profiles/sdk-validation.json`; a missing binding is a
categorical validation hold, not a merge conflict.

## Private binding plan

Set `GAME_L10N_WORKDIR` to the external workspace and `GAME_L10N_BINDINGS` to an
absolute JSON plan path inside it. Source and workspace must be separate trees.
All output directories must be new and remain inside the workspace. All file
paths below are forward-slash relative paths confined to their respective roots;
source hashes are mandatory. The build string must be the independently observed
6000.4 build. This illustrative plan is entirely synthetic:

```json
{
  "schema_version": 1,
  "engine": "unity",
  "engine_build": "6000.4.0f1",
  "backend": "Mono",
  "game_root": "ABSOLUTE_READ_ONLY_SOURCE_ROOT",
  "files": [
    {
      "path": "Synthetic_Data/synthetic-container.bin",
      "kind": "unityfs",
      "sha256": "CALLER_COMPUTED_SOURCE_SHA256",
      "bindings": [
        {
          "id": "synthetic-label",
          "member": "synthetic-member",
          "object_id": 11,
          "field_path": ["m_Label"],
          "source": "SYNTHETIC_SOURCE_{0}"
        }
      ]
    }
  ]
}
```

A raw string binding replaces `field_path` with integer `start` and `end` offsets.
`end` includes the original four-byte padding. Typed and raw binding modes cannot
be mixed within one object. Typed paths contain exact string keys or nonnegative
list indices. A binding cannot refer to a TextAsset object in this profile.

For explicitly supplied object schemas, a UnityFS file record may contain
`member_options: {MEMBER: {nodes: {OBJECT_ID: {records: NODE_RECORDS,
align_enabled: false}}}}`. IDs in JSON object keys are decimal strings. Set
`align_enabled: true` only for a verified native behaviour schema whose first
three level-one fields are `m_GameObject`, `m_Enabled` (`UInt8`), `m_Script`.
The SDK sets only the enabled byte's alignment flag, reconstructs the node to
discard parser caches, and still requires a lossless original object round trip.
It does not infer or repair arbitrary schema differences.

For a managed file, set `kind: "managed"`; each binding contains `id`,
`method_token` (the complete decimal MethodDef metadata token), `method_full_name`,
`offset`, and `source`. For example, a synthetic method identity is
`System.String SyntheticFixture.Widget::Display()` with token `100663297` and
offset `0`. The file record can supply `protected_methods`, an array of complete
tokens which must exist and cannot be changed. No method selection, call
redirection, comparison normalization, cloning or string eligibility heuristic
is provided. The writer rereads every method body and verifies branch/switch
targets by instruction index, exception regions, existing type/field/method
identities and signatures, and embedded resource bytes.

## Unpack and pack

```text
python SDK_TOOLS/unpack.py READ_ONLY_SOURCE_ROOT NEW_WORKSPACE_INVENTORY_DIRECTORY
python SDK_TOOLS/pack.py COMPLETED_RESULTS_JSON NEW_WORKSPACE_PAYLOAD_DIRECTORY
```

Unpack validates every supplied binding against the real input and writes
`segments.json` and `coverage.json` in the workspace. It inventories only supplied
bindings. Its `static_complete` and `runtime_verified` are always false. An
independent complete inventory, target eligibility decisions, context, translation
and complete review remain requirements of the core workflow.

Pack consumes the core result artifact directly (JSON with a `results` array,
an array, or JSON Lines). Each record must have `segment_id`, exact `source`,
`text` and status `reviewed` or `source_preserved`. IDs must match all plan bindings
exactly, without duplicates. Source-preserved text must remain exact. Protected
tokens, numeric format placeholders and actual tab/newline counts are checked
through existing core token primitives. This does not substitute for semantic
review or authorize source retention.

Set `GAME_L10N_STAGE=pack`; core `run_cached_tool` supplies it automatically.
After reconstruction and readback, `record_verified_pack_artifact` binds the
output to the current core completion attestation and its exact result digest.
The existing core completion guard also runs before any payload directory or
file is created, including direct invocation. Missing/stale completion cannot
produce a payload or valid pack state. Other construction failures can leave
an unbound diagnostic directory privately; use a new output directory when
resuming. Core completion policy is unchanged.

## Explicit font integration

The standalone `unity_fonts.integrate(members, groups, fallbacks)` library accepts
a mapping of member names to serialized bytes. Each group supplies `member`, four
`template_ids` (Font, TMP MonoBehaviour, Material, Texture2D), four unused `new_ids`,
matching `font_node` and `tmp_node` records, `font_bytes`, `names` (family/style),
`label`, `repertoire` (Unicode scalar values), `space_advance` and
`license_confirmed: true`. The caller must establish font licensing and glyph
coverage for the final reviewed repertoire; neither a boolean nor serialization
proves those facts.

The bounded dynamic template has an empty streamed payload and a 1x1 Alpha8 atlas.
The SDK retains its shader, constructs consistent source/material/atlas pointers,
sets dynamic population, clears template glyph/feature tables, embeds supplied
font bytes and derives face/legacy metrics from bounded SFNT tables. SFNT
collections and missing/older metric table layouts are rejected. Numeric readback
allows only bounded binary32 rounding when creating the group, followed by exact
serialized readback of the canonical values. Existing font objects and glyph
tables remain untouched except for explicitly selected fallback arrays.

Each fallback supplies `member`, `object_id`, and a zero-based `group` index. A
cross-member fallback appends or uniquely reuses the exact sibling member's
external reference; existing external records retain their order and values.
Existing fallback entries are retained. Original objects/types and serialized
type metadata are verified, as are all new tree values and references.

To use this through pack, put `font_groups` and `fallbacks` on the UnityFS file
record. Each group uses a workspace-relative `font_path` and `font_sha256` instead
of `font_bytes`. Font integration runs after text application and before bundle
reconstruction. All templates, schemas, IDs, repertoire and font choices stay in
the private plan. Standalone helpers return bytes; they do not create core pack
state by themselves.

## Limits and verification

This profile does not provide complete automatic discovery, TextAsset-specific
application, IL2CPP patching, arbitrary type-tree repair, arbitrary font/atlas
conversion, image decoding/OCR, runtime hooks, or install/launch operations.
Static object/reference consistency cannot prove runtime font ingestion,
rendering, shaping, layout or all-screen coverage. Follow the matching trigger in
`docs/engine-faq.md` when runtime evidence is required.

All adapter fixtures are generated from synthetic schemas, strings, metric-only
bytes and managed code. The metric fixture is not a usable font. No real player,
font program or extracted schema is committed. Run the focused suite with the
pinned dependencies available:

```text
python -m unittest tests.test_unity_6000_4_serialized tests.test_unity_6000_4_fonts tests.test_unity_6000_4_managed tests.test_unity_6000_4_profile -v
```

Managed tests require `GAME_L10N_CSC` and `GAME_L10N_CECIL` pointing to the private
compiler and Mono.Cecil DLL. Binary tests clearly skip when these optional tooling
dependencies are absent; publication validation must run them with no skips.
Run the complete core suite, repository privacy checker and engine FAQ checker
as required by the branch contract.
