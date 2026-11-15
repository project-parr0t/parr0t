# Korean localization quality profile

This document adds Korean-specific translation and review rules without changing Parr0t's engine-neutral data model or core behavior. Apply it when `ProjectContext.target_language` is Korean. Source-language authority, glossary precedence, segment metadata, and review coverage remain governed by their existing contracts.

## Authority and names

1. Reuse an applicable official Korean localization before project glossaries, domain convention, or literal translation. Bind ambiguous forms by concept or exact target IDs; never repair a homonym, numeric name, model number, or substring globally.
2. A title, organization, person, product, place, substance, or fictional term is an identity, not ordinary prose. Reuse an authorized Korean form when one exists. Otherwise follow the declared identity or transliteration policy: preserve the source form when authority requires it, or derive one consistent Korean form without ad hoc literal translation. Do not mix that form with the source form or append a parenthetical duplicate unless the source or an applicable authoritative glossary contains it.
3. Transliteration must be consistent for the same identity. Distinct identities that share spelling may use different Korean forms only with scoped evidence.
4. Prefer established Korean professional terminology when it preserves the source concept. Do not force a native-Korean paraphrase over a standard loanword or technical term, and do not preserve an unfamiliar literal calque when a well-established Korean term exists. Deliberate source verbosity, wordplay, and characterization remain semantic evidence and must not be shortened automatically.

## Voice, register, and UI style

1. Korean speech level is speaker- and situation-bound. Preserve each speaker's baseline personality, hierarchy, relationship, address forms, cadence, and degree of politeness. Never normalize all dialogue to honorific speech, polite speech, or casual speech.
2. A register shift requires relationship, hierarchy, scene-state, or progression evidence. Neighboring lines from the same speaker are style evidence only when independently reviewed and context-compatible.
3. UI labels, traits, descriptions, tutorials, notifications, and dialogue do not share one universal sentence ending. Follow the authoritative Korean baseline for the same surface and function. Keep parallel items grammatically parallel, but do not impose dialogue endings on UI prose or UI formality on character speech.
4. Avoid translationese subjects and pronouns when Korean naturally omits them, unless omission changes agency, contrast, quest logic, or speaker stance. Preserve explicit ambiguity when the source does not resolve it.

## Morphology and composition

1. Treat particles, counters, spacing, and inflection around placeholders as part of the render contract. Do not hard-code a particle whose correct form depends on a dynamic value unless the runtime supplies a Korean particle-selection mechanism or all possible values are proven compatible.
2. Preserve placeholder identity, order, formatting, markup, and control codes. Translate only static fragments around dynamic values. Representative composed-output checks must verify Korean spacing and affixes while leaving numbers, names, and runtime values dynamic.
3. Do not duplicate labels, units, counters, or grammatical affixes already rendered by adjacent slots. Conversely, a fragment table is incomplete if the final composed value retains source-language prose.
4. Distinguish a serialized line-break escape such as `\n` from the runtime line break it represents. Preserve the resource format's required encoding, and verify that the final display breaks the line instead of showing a literal backslash and `n`.

## Punctuation and typography

1. Replace source-language sentence punctuation with the established Korean project style unless punctuation is an immutable token, markup, identifier, deliberate voice cue, or authoritative name component.
2. Chinese full-width stops, commas, colons, brackets, and quotation marks are review defects when they leak into ordinary Korean prose without an evidenced stylistic purpose.
3. Preserve meaningful pauses, interruptions, ellipses, emphasis, and line breaks, but render them consistently with the project's Korean typography. Do not add explanatory parentheses, translator notes, bilingual glosses, or unsupported quotation marks.
4. Check spacing around Korean particles, counters, units, Latin abbreviations, and inline markup in the final composed value rather than judging isolated fragments only.

## Korean review checklist

Review every affected target for:

- official-term and identity consistency under the declared authority;
- speaker-specific speech level, address form, and evidence-backed register shifts;
- natural Korean syntax without lost agency, conditions, units, or logic;
- established domain terminology rather than avoidable literal calques;
- scoped handling of names, numbers, models, acronyms, and source-language exceptions;
- Korean punctuation, spacing, particles, counters, line breaks, and typography;
- absence of unsupported parenthetical explanations or bilingual duplication;
- complete translation of composed UI text with dynamic values preserved.

These checks supplement independent semantic review. They do not authorize global replacement, source mutation, runtime execution, or a claim of runtime completeness.