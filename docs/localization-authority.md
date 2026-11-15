# Localization authority contract

Parr0t must resolve source language, terminology, entity identity, and speaker voice from explicit evidence instead of global text replacement or one-register normalization. This contract is engine-neutral; SDKs emit evidence, and core inventory, prompts, validation, and review preserve it.

## Source-language authority

`ProjectContext.source_language` is the default. A segment may override it only with independently inventoried metadata:

```json
{
  "source_language_authority": {
    "schema": "parr0t.source-language-authority/v1",
    "language": "<BCP-47 language tag>",
    "reason_code": "original_implementation",
    "evidence_sha256": "<lowercase SHA-256>"
  }
}
```

Supported reason codes are `original_implementation`, `complete_source_catalog`, and `authoritative_source_metadata`. The digest binds private evidence; it is not evidence by itself. Manifest, presentation, display-language, or secondary-localization metadata cannot establish an override. Secondary locales may corroborate meaning but cannot replace the authoritative source. Both independent inventories must agree on the complete object.

## Terminology authority and entity scope

`GlossaryEntry.authority` uses this precedence, highest first:

1. `user_override`
2. `official_localization`
3. `project_glossary`
4. `source_internal`
5. `domain_standard`
6. `literal_fallback`

The default is `project_glossary`. `source_language` optionally limits an entry to the effective source language. `target_ids` limits it to exact immutable segment IDs. `concept_id` requires the segment metadata list `terminology_concept_ids` to contain the same identity.

Concept or target binding is required when a lexical form can denote several entities, appears inside a model name or identifier, or is a numeric designation. Never repair such a term with a global substring replacement. Equal-authority conflicts remain visible and must be resolved in the glossary rather than silently selected. Canonical `target` remains preferred; `targets` lists only accepted contextual or inflected renderings.

Glossary authority may preserve a source name, prescribe a target-language name, or retain punctuation. It therefore overrides generic literalization and source-punctuation heuristics for the exact bound entity only.

## Parenthetical additions

Do not add explanatory parentheses merely to carry both source and target names. An SDK may request deterministic enforcement with:

```json
{
  "source_absent_parenthetical_policy": "source_or_authoritative_glossary"
}
```

Under that policy, every target parenthetical must bind one-to-one to a balanced source parenthetical in the same ordinal, nesting, line/pipe, sentence, and inline-placement slot, or occur inside an exact applicable authoritative glossary target. The mere presence or equal count of unrelated source parentheses is never authority. Ambiguous structure fails closed for semantic review. The policy is opt-in because ordinary punctuation can be language- and format-dependent. Both inventories must agree on it.

## Speaker identity and register

A `CharacterProfile` may declare `source_language`, `register_shift_rules`, and `evidence_line_ids` in addition to baseline personality, voice, formality, address, speech, relationship, and example-line fields. Baseline voice is stable. Translation and review must not normalize all speakers to one politeness level or generic register. A shift is valid only when supplied relationship, hierarchy, scene-state, or progression evidence supports it.

Character interpretation follows the authoritative original-language source. A secondary localization may clarify ambiguity but cannot define personality or overwrite an evidenced speech pattern. Only independently reviewed same-speaker targets become style anchors.

## Correction and review

Corrections mutate target text only. Segment IDs, source text, order, placeholders, markup, control codes, and inventory authority metadata remain immutable. A terminology or speaker-profile change invalidates affected candidate bindings and requires fresh current-generation triage; selected risk targets also require strong review. Sampling never approves unsampled text.

When the locked target language is Korean, `docs/korean-localization.md` supplies the language-specific quality profile for terminology, names, register, morphology, composition, punctuation, and typography. It supplements this authority contract and cannot weaken source provenance, glossary scope, immutable metadata, or review coverage.

Review checks entity scope, authority precedence, unsupported parenthetical additions, source-language provenance, stable speaker voice, and evidence-backed register shifts in addition to meaning, fluency, UI function, and format integrity. Provider refusal or operational failure routes only that target onward and never reduces scope.
