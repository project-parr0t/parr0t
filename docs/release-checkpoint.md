# Release checkpoint contract

Every Parr0t distribution contains `parr0t-release-checkpoint.json`. The file makes the released translation a machine-readable continuation point: when an agent later receives that release together with a readable game folder, it can recover verified unchanged targets without translating or reviewing them again.

The checkpoint is distribution data, not SDK data. It may contain only the target strings already shipped in the release; source text is represented by a digest and must not be duplicated in a separate field. A `source_preserved` target may necessarily equal its source, but remains explicitly marked unresolved. The file must not contain prompts, model output, provider explanations, credentials, logs, private workspace paths, or unpublished translation candidates.

## Bound identity

The checkpoint binds one release to:

- patch and release IDs;
- the selected SDK branch, commit, and adapter fingerprint;
- every supported game build and its binding fingerprint;
- the complete source-inventory method, fingerprint, and target count;
- a run-level context fingerprint for provenance and a global review-policy fingerprint;
- each stable target ID and its source, target, location, dependency, and review fingerprints;
- the declared locale-fallback result.

The package manifest records the checkpoint path, schema, byte count, and digest. Package validation rejects a missing, noncanonical, modified, or differently bound checkpoint.

## Resume algorithm

When a prior release and game folder are supplied, the agent must do this before any model call:

1. Validate the package and checkpoint and confirm that their patch and release IDs agree.
2. Select the compatible SDK under `docs/sdk-selection.md` and produce the two current source inventories required by `docs/coverage-gates.md`.
3. Recompute the game-build, SDK-adapter, inventory, context, and review-policy bindings. Treat a different game build as a new release target, not as proof that every string changed.
4. Match current entries by stable ID. Reuse a `reviewed` target without a model call only when its source hash, location fingerprint, dependency fingerprint, target digest, and target-scoped review fingerprint still match, the global review policy is unchanged, and the selected SDK declares the adapter compatible. The target-scoped review fingerprint must cover only the context, glossary, voice/function evidence, and constraints that can affect that target.
5. Never promote `source_preserved` to translated or reviewed. It remains unresolved input for the configured routes.
6. Analyze, translate, and review only new, changed, unresolved, or invalidated entries. Run deterministic integrity checks on reused entries without semantic model review.
7. Emit a new checkpoint for the new release; never edit the old release in place.

A new game build always requires a newly validated patch and compatibility record. It may reuse exact unchanged translations while building that patch, but the old release still fails its build binding before installation. Locale fallback is not permission to install a patch on an unsupported build.

## Selective correction

When the user requests a wording or terminology change, resolve it to stable target IDs and invalidate those entries plus the transitive reverse dependency closure declared by the SDK. Examples of dependencies include shared glossary terms, paired UI states, speaker voice anchors, and references whose interpretation depends on another line.

Do not invalidate the full inventory merely because one entry changed. Expand the invalidation set only when evidence shows a wider dependency, a global glossary/voice/context policy changes, or stable mapping is ambiguous. If the user explicitly requests a complete re-review, `docs/coverage-gates.md` still requires a fresh verdict for every current target.

The control plane sees only IDs, fingerprints, counts, invalidation reasons, and queue states. Text comparison and checkpoint materialization remain in the private data plane.

## Missing-locale fallback

The selected engine SDK should, when its format permits, prevent a newly introduced or absent localized key from producing a missing-locale crash. In descending preference, use a verified fallback to:

1. the game's declared base or English locale;
2. the exact source text supplied by the current game resource;
3. a clearly nonempty placeholder that preserves the required data shape.

The fallback must preserve placeholders, types, keys, array/object shape, and engine control data. It must be exercised by the SDK's synthetic compatibility check and recorded as `implemented` with its verified mode. Do not fabricate empty locale objects or guess a fallback path.

Some formats replace whole archives, compile locale tables, or provide no safe runtime lookup hook. In that case, record `status: unsupported`, `mode: none`, and a neutral `reason_code`, then continue without claiming crash protection. This is an optional compatibility improvement, not a release blocker unless the user or selected SDK explicitly makes it one.

## Minimal schema example

This synthetic example shows the required shape. Digests are abbreviated here for readability; real files require lowercase 64-character SHA-256 values and a valid Git commit ID.

```json
{
  "schema": "parr0t.release-checkpoint/v1",
  "patch_id": "fixture-localization",
  "release_id": "r1",
  "sdk": {
    "branch": "sdk-fixture-1.0",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "adapter_fingerprint": "<sha256>"
  },
  "game_builds": [
    {"id": "fixture-build", "binding_fingerprint": "<sha256>"}
  ],
  "source_inventory": {
    "method": "fixture-primary",
    "fingerprint": "<sha256>",
    "target_count": 1
  },
  "context": {
    "fingerprint": "<sha256>",
    "review_policy_fingerprint": "<sha256>"
  },
  "entries": [
    {
      "id": "fixture:0001",
      "source_sha256": "<sha256>",
      "target": "TARGET_FIXTURE",
      "target_sha256": "<sha256>",
      "location_fingerprint": "<sha256>",
      "dependency_fingerprint": "<sha256>",
      "review_fingerprint": "<sha256>",
      "status": "reviewed",
      "dependencies": []
    }
  ],
  "locale_fallback": {
    "status": "unsupported",
    "mode": "none",
    "fallback_locale": "",
    "placeholder": "",
    "verified": false,
    "reason_code": "format_has_no_safe_runtime_fallback"
  }
}
```

Generate and validate the real file through the shared library contract. Do not hand-author placeholder digests for a release.
