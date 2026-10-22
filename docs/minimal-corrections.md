# Minimal user-directed corrections

A typo, a few tone/voice adjustments, or a bounded data change is not a new translation run. Start from the current release checkpoint and classify the request before doing any discovery:

- **Exact value:** the user supplied or approved the final target text or data value. Apply it deterministically with zero model calls.
- **Bounded wording:** the user identified a small set that needs tone, voice, nuance, or naturalness work but did not supply final wording. Freeze those IDs and their reverse dependency closure, then use the focused semantic route below.
- **Structural/global change:** stable mapping is ambiguous, more than 32 targets are directly named, or a glossary, character voice, context, schema, or review policy changed globally. Use selective continuation from `docs/release-checkpoint.md`; do not pretend it is an exact correction.

One narrow private data-plane lookup may resolve a user-visible label or description to stable IDs. The control plane receives only the resolved IDs, counts, fingerprints, and deterministic codes. It must not search the full translation, reopen the game root, or read candidate text.

## Local interpretation and search UI

When the user identifies wording by dialogue, translation, or reference rather than a stable ID, use the private local correction UI. It creates one SQLite state file inside the external workspace; it does not use an external database, embedding API, browser service, or provider call.

```text
game-l10n correction-ui \
  --workspace <external-workspace> \
  --input <verified-current-primary-inventory> \
  --existing <released-full-results.jsonl> \
  --checkpoint <matching-parr0t-release-checkpoint.json>
```

Open the loopback URL printed by the command. The URL contains an in-memory session token in its fragment. The server binds only to `127.0.0.1`, serves no game text before token validation, and persists private text, flags, user interpretations, references, and staged changes only in `translations/user-interpretation.sqlite3`. Reopening the same file requires identical source, result, checkpoint, and inventory bindings.

The UI supports three deterministic search modes:

- **Exact:** normalized equality against ID, source, current target, user note, or user reference.
- **Character distance:** bounded candidate selection followed by normalized Levenshtein distance.
- **Vector distance:** local character n-gram vectors with cosine distance. This is a file-local lexical vector search, not an external semantic embedding claim.

Search executes in the local index. An agent must not imitate it by opening the full inventory, reading every result, or creating per-query scan files. Result limits and candidate ceilings are search boundaries, not permission to omit a user-flagged target.

Each flag has one of two routes:

- **Direct:** the user enters the final replacement. Deterministic integrity checks run immediately; pressing apply writes the standard `parr0t.user-correction-request/v1` and one new full result artifact. It makes zero model calls.
- **Agent:** the user records an interpretation, problem, or reference. Export writes `parr0t.user-interpretation-task/v1` containing only the flagged IDs and their reverse dependency closure, capped at 32 records. A fresh data-plane agent may read that task and return `parr0t.user-interpretation-proposals/v1`. Proposals are not accepted translations until the user reviews and approves them in the UI.

The proposal file has exactly:

```json
{
  "schema": "parr0t.user-interpretation-proposals/v1",
  "task_id": "<copied task id>",
  "proposals": [
    {
      "id": "synthetic-target-001",
      "decision": "replace",
      "target": "TARGET_PROPOSAL",
      "source_sha256": "<copied source binding>",
      "context_sha256": "<copied context binding>"
    }
  ]
}
```

Use `decision: replace` with a changed target. For a reverse dependency that was checked and needs no wording change, use `decision: unchanged` and copy its current target exactly. The UI records that decision only after the user approves it; it never treats an omitted dependency as reviewed. Do not add explanations, hidden source copies, alternative schemas, or unflagged suggestions. The UI rejects stale bindings, unknown IDs, malformed proposals, invalid decision/target pairs, and deterministic integrity failures. The control plane sees only counts and paths; it never opens the SQLite state, task, proposal, request, or corrected result.

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

The result summary is the correction receipt. Retain its request, checkpoint, inventory, base-result, output, and changed-candidate digests in the private workspace so a later correction can prove exactly what changed without rereading content.

`--user-requested` is valid only when the user supplied or approved the exact replacement wording. If the user merely reports a problem without specifying the final wording, use a focused translation/review route for the affected IDs instead of inventing user authority.

## Focused semantic route

For a bounded wording request, use the already verified inventory, released results, checkpoint, target-scoped context fingerprints, glossary, and character profiles. Do not repeat extraction, inventory reconciliation, whole-flow context analysis, or full review.

1. Freeze the directly requested IDs and their reverse dependency closure. The combined set must remain at or below 32; otherwise use selective continuation.
2. Load only the bound records and already attested context relevant to that set. If required context is missing, gather it once for those IDs only.
3. Send the whole frozen set as one ordered semantic batch to one fresh data-plane worker. Do not create a call, prompt file, agent, or retry loop per record.
4. Run one ordered independent focused review batch only for generated wording. Rewrite only flagged records in one batch. Exact values and non-semantic data changes do not receive semantic review.
5. Run deterministic validation on the frozen set, merge it into the unchanged released result once, repack owning payloads once, and verify the package once.

If the UI already resolved the requested IDs, that fixed set is authoritative. Do not run another label search, global text search, vector search, inventory scan, or context reconstruction. An agent may read only the exported focused task and must return only proposals for its bound records.

The normal ceiling is two semantic calls for the complete correction set: one rewrite and one focused review. A provider failure enters the existing resumable failure queue for only the affected IDs; it must not reopen the full pipeline. Stop after two failures with the same cause. Do not spawn a controller, promote an SDK, maintain branches, publish, install, launch, or create a new task/result format as part of a correction.

## Dependency boundary

The checkpoint's reverse dependency closure remains authoritative. If an exact changed ID invalidates another target not named in the request, the command writes no output and reports those IDs as requiring targeted review. Review only that closure; never turn it into a complete re-review unless the user asks for one or a global glossary, voice, context, or review policy changed.

For a label and paired state values, resolve every runtime slot ID first. A visually single setting may have two or more independently stored targets. If the user supplied exact wording for all affected slots, include all of them in one request rather than issuing successive packages.

## Proportional packaging

After the correction result is ready:

1. Reuse the existing SDK adapter, pack specification, installer contract, and validated staging inputs.
2. Repack only the payload container that owns a changed ID. A monolithic engine asset may still require rebuilding that one asset.
3. Run the changed-target deterministic checks and one package verification.
4. Emit the new release checkpoint from the corrected result.
5. Do not re-run context analysis, translate or review unchanged targets, spawn a discovery subagent, hash the complete game tree, inspect an installation, build a new validator, or produce extra reports unless a concrete failed check requires it.

For a correction-only request, this section is the stopping rule. A passing corrected artifact and one verified package are completion; repository maintenance, SDK promotion, publication, and runtime actions remain separate user requests.

Installation state is irrelevant to building a correction from verified workspace artifacts. Use `inspect-installed-state` only when the requested operation depends on the real installed state, such as diagnosing an update or preparing an explicitly requested install/update action.
