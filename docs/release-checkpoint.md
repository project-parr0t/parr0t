# Release checkpoint contract

Every published distribution contains `parr0t-release-checkpoint.json`. It makes the released translation a machine-readable continuation point for verified unchanged targets. A local package or integrity handoff that is not intended for distribution may instead use the smaller `parr0t.local-release-integrity/v1` checkpoint: it binds ownership/license, language, source inventory, and per-target source/target digests and integrity evidence, without claiming future-update readiness. It must preserve whether results were reviewed; a result produced with review explicitly disabled is labeled unreviewed, not independently reviewed or user-accepted merely because it is included. `required_for_distribution: false` permits this local integrity checkpoint; it does not waive package ownership, payload hashes, source lineage, or integrity checks. When a title-specific translation source repository is also supplied, it remains a separate build/apply input; use it instead of reconstructing equivalent title-specific work or copying it into the checkpoint repository.
Local entries use `reviewed`, `unreviewed`, `accepted_unreviewed`, or `source_preserved`. `unreviewed` is valid for a translated result only when review was explicitly disabled. `accepted_unreviewed` remains a separate user-authorized acceptance state; neither inclusion nor integrity validation implies acceptance or independent review.

## Local integrity checkpoint generation

For a local-only filesystem or Steam package, set workspace configuration `release_checkpoint.required_for_distribution` to `false`. Omit `checkpoint_file`; provide this request in the package spec instead:

```json
"local_checkpoint": {
  "inventory": "inventory.json",
  "results": "translation-results.json",
  "ownership": {
    "owner": "Authorized owner",
    "license": "License identifier"
  }
}
```

The inventory and results must be validated existing paths within the workspace. `owner` and `license` are explicit required values. The package command validates the selected inventory and ordered result/source/status bindings, generates local integrity evidence, embeds that evidence in the checkpoint, and persists the checkpoint with a derived immutable spec; per-target evidence hashes bind entries to the embedded evidence. Callers do not hand-author hashes or evidence. `local_checkpoint` and `checkpoint_file` cannot be combined, and local generation is rejected when distribution requires the full checkpoint. A translated result is labeled `unreviewed` only when `routing.require_review` is false. `accepted_unreviewed` requires actual user acceptance authority; generation does not create it.

## Full release checkpoint

Full release-checkpoint entries record one of three exact states: `reviewed`, user-authorized `accepted_unreviewed`, or `source_preserved`. A translated candidate that was shipped after review failure or review exhaustion must remain `accepted_unreviewed`; it must not be promoted to `reviewed`. A missing or deterministically unsafe candidate remains `source_preserved`. These states allow a later correction or resumed review to touch only the affected targets.

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

For published distributions, the package manifest records the checkpoint path, schema, byte count, and digest. Package validation rejects a missing, noncanonical, modified, or differently bound checkpoint. A local-only integrity checkpoint is validated against its local schema and matching patch/release identity; it is not evidence of update compatibility or publication readiness.

## Resume algorithm

When a prior release and game folder are supplied, the agent must do this before any model call. If a title-specific translation source repository is also supplied, bind its current commit and use its applicable implementation/state as a third input rather than ignoring it:

1. Validate the package and checkpoint and confirm that their patch and release IDs agree.
2. Select and activate the compatible SDK under `docs/sdk-selection.md`, inspect any supplied translation source repository without importing its title-specific data into Parr0t or the instance branch, and produce the two current source inventories required by `docs/coverage-gates.md` from the read-only game folder.
3. Recompute the game-build, SDK-adapter, inventory, context, and review-policy bindings. Treat a different game build as a new release target, not as proof that every string changed.
4. Match current entries by stable ID. Reuse a `reviewed` target without a model call only when its source hash, location fingerprint, dependency fingerprint, target digest, and target-scoped review fingerprint still match, the global review policy is unchanged, and the selected SDK declares the adapter compatible. The target-scoped review fingerprint must cover only the context, glossary, voice/function evidence, and constraints that can affect that target.
5. Never promote `source_preserved` to translated or reviewed. It remains unresolved input for the configured routes.
6. Analyze, translate, and review only new, changed, unresolved, or invalidated entries. Run deterministic integrity checks on reused entries without semantic model review.
7. Emit a new checkpoint for the new release; never edit the old release in place.

A new game build always requires a newly validated patch and compatibility record. It may reuse exact unchanged translations while building that patch, but the old release still fails its build binding before installation. Locale fallback is not permission to install a patch on an unsupported build.

## Selective correction

When the user requests a wording or terminology change, resolve it to stable target IDs and invalidate those entries plus the transitive reverse dependency closure declared by the SDK. Examples of dependencies include shared glossary terms, paired UI states, speaker voice anchors, and references whose interpretation depends on another line.

When the user supplies or approves the exact final target wording, use the deterministic `apply-user-correction` flow in `docs/minimal-corrections.md`. This is user-directed correction rather than model translation: do not re-run context analysis or semantic review for the named IDs. The command still verifies the released source/target bindings and all deterministic integrity constraints. If the reverse dependency closure contains unnamed IDs, stop and review only that closure before packaging.

When the user requests tone, voice, nuance, or naturalness improvement without final wording, use a bounded semantic route sized to the user's explicit positive task bounds and available capacity. Reuse existing target-scoped context and perform the requested rewrite and review work without imposing an undocumented target-count, batch-count, or universal re-review cap. This does not authorize a full-inventory review, new extraction, SDK work, branch maintenance, publication, installation, or launch unless requested.

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
