# Minimal user-directed corrections

An exact user-requested typo or wording correction is not a new translation run. When the released source inventory, result artifact, and release checkpoint still match, Parr0t applies the user's exact target text without context analysis, translator calls, independent semantic review, subagents, or full-inventory rebuilding.

## Fast path

Keep the correction request in the private workspace:

```json
{
  "schema": "parr0t.user-correction-request/v1",
  "changes": [
    {"id": "synthetic-target-001", "target": "TARGET_CORRECTED_TEXT"}
  ]
}
```

Then run:

```text
game-l10n apply-user-correction \
  --workspace <external-workspace> \
  --input <verified-current-primary-inventory> \
  [--existing <released-full-results.jsonl>] \
  --checkpoint <matching-parr0t-release-checkpoint.json> \
  --request <private-correction-request.json> \
  --output <new-corrected-results.jsonl> \
  --user-requested
```

The command requires exact ID agreement across the current verified inventory and checkpoint. Supply `--existing` to preserve the workspace's unchanged result records field-for-field; when it is unavailable, Parr0t materializes the released candidates from the checkpoint. It verifies every source and released-target binding, changes only named IDs, preserves every other target, increments the changed candidate generation, records explicit user authority, and runs deterministic placeholder, control-code, script-intrusion, UI-composition, and hard-length checks. It makes zero model calls and does not inspect or mutate the game installation.

`--user-requested` is valid only when the user supplied or approved the exact replacement wording. If the user merely reports a problem without specifying the final wording, use a focused translation/review route for the affected IDs instead of inventing user authority.

## Dependency boundary

The checkpoint's reverse dependency closure remains authoritative. If a changed ID invalidates another target not named in the request, the command writes no output and reports those IDs as requiring targeted review. Review only that closure; never turn it into a complete re-review unless the user asks for one or a global glossary, voice, context, or review policy changed.

For a label and paired state values, resolve every runtime slot ID first. A visually single setting may have two or more independently stored targets. If the user supplied exact wording for all affected slots, include all of them in one request rather than issuing successive packages.

## Proportional packaging

After the correction result is ready:

1. Reuse the existing SDK adapter, pack specification, installer contract, and validated staging inputs.
2. Repack only the payload container that owns a changed ID. A monolithic engine asset may still require rebuilding that one asset.
3. Run the changed-target deterministic checks and one package verification.
4. Emit the new release checkpoint from the corrected result.
5. Do not re-run context analysis, translate or review unchanged targets, spawn a discovery subagent, hash the complete game tree, inspect an installation, build a new validator, or produce extra reports unless a concrete failed check requires it.

Installation state is irrelevant to building a correction from verified workspace artifacts. Use `inspect-installed-state` only when the requested operation depends on the real installed state, such as diagnosing an update or preparing an explicitly requested install/update action.
