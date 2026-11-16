# Korean localization quality profile

This document adds Korean-specific translation and review rules without changing Parr0t's engine-neutral data model or core behavior. Apply it when `ProjectContext.target_language` is Korean. Source-language authority, glossary precedence, segment metadata, and review coverage remain governed by their existing contracts.

## Authority and names

1. Reuse an applicable official Korean localization before project glossaries, domain convention, or literal translation. Bind ambiguous forms by concept or exact target IDs; never repair a homonym, numeric name, model number, or substring globally.
2. A title, organization, person, product, place, substance, or fictional term is an identity, not ordinary prose. Reuse an authorized Korean form when one exists. Otherwise follow the declared identity or transliteration policy: preserve the source form when authority requires it, or derive one consistent Korean form without ad hoc literal translation. Do not mix that form with the source form or append a parenthetical duplicate unless the source or an applicable authoritative glossary contains it.
3. Transliteration must be consistent for the same identity. Distinct identities that share spelling may use different Korean forms only with scoped evidence.
4. Prefer established Korean professional terminology when it preserves the source concept. Do not force a native-Korean paraphrase over a standard loanword or technical term, and do not preserve an unfamiliar literal calque when a well-established Korean term exists. Deliberate source verbosity, wordplay, and characterization remain semantic evidence and must not be shortened automatically.

## Voice, register, and UI style

Apply `docs/dialogue-localization.md` first. Korean speech level is speaker- and situation-bound; it is not a sentence-ending substitution.

1. Record the speaker's baseline and the current addressee, hierarchy, intimacy, conflict state, public/private setting, speech act, and emotional pressure. Preserve personality, cadence, preferred address forms, and degree of directness. Never normalize all dialogue to honorific speech, polite speech, or casual speech.
2. Distinguish formal-polite, informal-polite, intimate/plain, familiar-authoritative, semi-formal, and mixed or marked Korean speech levels when the source evidence supports them. Also inspect subject honorification, addressee honorification, titles, vocatives, first- and second-person forms, particles, contractions, profanity, dialect, and omission. Matching only the final ending is insufficient.
3. A speech-level or address-form shift requires relationship, hierarchy, scene-state, emotional, performative, or progression evidence in the current target's dialogue context. Preserve deliberate code-switching. When evidence is uncertain, retain source ambiguity and route strong review instead of choosing a convenient level.
4. Approved neighboring lines from the same speaker are style evidence only when their addressee, relationship state, scene role, and consistency group are compatible. A speaker may address a superior, customer, friend, enemy, and self differently without becoming inconsistent.
5. Natural Korean may omit subjects and pronouns, but omission must not change agency, contrast, quest logic, social stance, or who addresses whom. Do not introduce a generic second-person pronoun, kinship titles, professional titles, or honorific markers merely to make a sentence sound Korean.
6. UI labels, traits, descriptions, tutorials, notifications, and dialogue do not share one universal sentence ending. Follow the authoritative Korean baseline for the same surface and function. Keep parallel items grammatically parallel, but do not impose dialogue endings on UI prose or UI formality on character speech.

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