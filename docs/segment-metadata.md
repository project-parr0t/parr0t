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

1. Inspect resource bindings or render call sites when the selected SDK can do so without executing the game.
2. Use stable related IDs so a checkpoint can invalidate the affected composition dependencies after a correction.
3. Keep labels and values as separate targets when the runtime does; do not merge them merely to make translation easier.
4. Do not add fixed target affixes until their target-locale form is known and stable. Without explicit affixes, semantic review still checks the render pattern but deterministic duplicate detection cannot claim certainty.
5. Treat `ui_state_semantics` as meaning evidence, not as target text. The target wording still follows locale, style, glossary, and space constraints.
