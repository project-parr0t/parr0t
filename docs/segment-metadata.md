# Segment metadata contract

An extracted string is not always a complete rendered message. A UI may render a fixed label and a separately localized value, join several fragments, or map an ambiguous source token to a semantic state. Engine SDKs should preserve these boundaries so translation and review operate on what the slot actually owns.

## Standard UI fields

All fields are optional and belong in `Segment.metadata`. Emit them only from static format or call-site evidence; do not guess them from wording alone.

- `ui_slot_role`: `standalone`, `label`, `value`, `state_value`, or `template_fragment`.
- `ui_state_semantics`: a stable semantic value such as `enabled`, `disabled`, `open`, or `closed` when the displayed source token is ambiguous.
- `ui_composed_with_ids`: stable IDs of the other slots or labels rendered with this target.
- `ui_render_pattern`: a structural description such as `{label}: {value}`. Keep private strings out of it.
- `ui_fixed_target_prefix` and `ui_fixed_target_suffix`: immutable target-locale text rendered outside this slot. These allow deterministic duplicate-affix checks.
- `ui_max_chars`: a nonnegative hard character limit only when the engine or resource format supplies one. Pixel width and dynamic layout remain SDK-specific validation.

Example with synthetic values:

```json
{
  "id": "synthetic-setting-state",
  "source": "SOURCE_STATE",
  "metadata": {
    "context_domains": ["functional"],
    "text_type": "setting_state",
    "ui_slot_role": "state_value",
    "ui_state_semantics": "enabled",
    "ui_composed_with_ids": ["synthetic-setting-label"],
    "ui_render_pattern": "{label}: {value}",
    "ui_fixed_target_prefix": "TARGET_LABEL: ",
    "ui_max_chars": 12
  }
}
```

The translator returns only the value for this segment. It must not repeat `TARGET_LABEL: `. The compact judge reports `slot_composition` when a candidate duplicates surrounding chrome, translates the wrong semantic state, or otherwise violates an evidenced slot boundary. Deterministic validation rejects an explicitly repeated fixed affix and an exceeded hard character limit before packaging.

## Extraction and review rules

### Optional empty grammatical slots

An adapter may declare `ui_empty_target` for an optional `template_fragment`
only after static format or call-site evidence establishes its composition and
the nonempty fixed target affix rendered outside it. This is inventory authority;
candidate/provider output and an `allow_empty` flag cannot grant it. A digest
records evidence identity, not proof that the evidence was inspected. The adapter
must verify that evidence and retain it privately before preparing the inventory.

The object has exactly `schema: parr0t.ui-empty-target/v1`,
`reason_code: optional_grammatical_slot`, and lowercase SHA-256 fields
`source_sha256`, `composition_sha256`, `evidence_sha256`. The source digest hashes
UTF-8 source bytes. The composition digest uses `store.stable_hash` on an object
with `target_id`, `source_sha256`, `ui_slot_role`, `ui_composed_with_ids`,
`ui_render_pattern`, `ui_fixed_target_prefix`, and `ui_fixed_target_suffix`.
Absent affixes are empty strings. Related IDs must be nonempty, unique, and omit
the slot itself; the render pattern must contain `{value}`.

Only the exact empty string is eligible. Whitespace and ordinary empty targets
remain invalid. Token, glossary, and other integrity checks still run. Native
wave import reads this authority from the prepared source and verifies its full
manifest fingerprint; changed metadata invalidates the join. Legacy unbound
shard import and unbound shard merge still reject empty success. Related IDs
must exist in that bound source. Missing, malformed, external, or nonfile source
paths fail closed; a wave import reports `E_NATIVE_WAVE_RESULT_INVALID`. The
validated source map is loaded once per wave import, merge, or legacy receipt
upgrade scan and reused across its tasks. Import is candidate transport only:
independent current-candidate review remains mandatory before checkpoint
acceptance. This contract does not relax provider envelopes or mark any candidate
reviewed, source-preserved, complete, or releasable.

1. Inspect resource bindings or render call sites when the selected SDK can do so without executing the game.
2. Use stable related IDs so a checkpoint can invalidate the affected composition dependencies after a correction.
3. Keep labels and values as separate targets when the runtime does; do not merge them merely to make translation easier.
4. Do not add fixed target affixes until their target-locale form is known and stable. Without explicit affixes, semantic review still checks the render pattern but deterministic duplicate detection cannot claim certainty.
5. Treat `ui_state_semantics` as meaning evidence, not as target text. The target wording still follows locale, style, glossary, and space constraints.

## Strong-review selection fields

Extraction or context analysis may add `review_category` and `requires_strong_review=true` from structural evidence. Supported default high-risk categories are `allusion`, `critical_logic`, `parody`, `quest_title`, `script_sensitive`, and `wordplay`. Boolean evidence flags such as `reference_cue`, `parody`, `allusion`, `wordplay`, or `logic_facing` also select strong review. Do not infer these fields in the text-free control plane or mark ordinary dialogue high-risk merely because of its wording; uncertainty is handled by complete low-cost triage and deterministic sampling.
