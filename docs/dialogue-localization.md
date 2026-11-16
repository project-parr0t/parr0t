# Dialogue localization contract

Dialogue localization preserves a speaker's identity while allowing evidenced change. It is not a global tone-normalization pass. This contract is language-neutral; target-language profiles may define the concrete linguistic signals used to satisfy it.

## Per-target evidence

Every dialogue target in narrative context must have a `dialogue_contexts` entry keyed by target ID. The entry records:

- speaker and addressee IDs;
- relationship state and scene phase;
- speech act, delivery, and emotion;
- situational register and address form;
- whether a register shift is baseline, supported, or uncertain, with a reason;
- a consistency group and stable, risk, or uncertain status;
- uncertainties, evidence line IDs, and confidence.

The analyzer must cite the target itself. It may use adjacent lines and approved authority as evidence, but it must not invent an addressee, relationship, emotion, or register transition. Unknown facts remain explicit uncertainty. Complete-context mode fails closed when a dialogue target lacks bound evidence.

## Translation behavior

1. Preserve the source's meaning, ambiguity, speech act, emotional force, and characterization.
2. Treat speaker profile, addressee, relationship, scene phase, and delivery as simultaneous constraints. No single trait authorizes flattening every line into one register.
3. Keep a speaker's baseline stable within compatible situations. Change it only when the current target has evidence for a shift.
4. Compare approved style anchors only when their consistency group, addressee, or relationship is compatible. An approved but incompatible line is not a style mandate.
5. Do not add exposition, motives, politeness, insults, intimacy, or identity labels absent from source or authoritative context.
6. Preserve source-language authority for character identity and intent. A pivot language may clarify meaning but must not erase evidence present in the original language.

## Review behavior

Dialogue with a supported register shift, a consistency risk, or uncertain context receives strong review. Review must compare the candidate with both the source and the current target's structured dialogue evidence. It must distinguish:

- a justified situational change from accidental voice drift;
- character cadence from generic fluency;
- stable address behavior from an isolated ending;
- deliberate ambiguity from missing context;
- source characterization from target-language stereotypes.

Reviewers may approve different registers for one speaker when the evidence differs. They must reject a globally uniform voice that contradicts scene, relationship, hierarchy, or intent. Uncertainty routes review; it never grants permission to guess.

## Consistency groups and memory

A consistency group identifies targets expected to share a compatible voice state, such as the same speaker-addressee relationship and scene role. It is not a replacement key and never changes target IDs. Approved style memory stores target-bound dialogue context and prioritizes compatible anchors over merely recent ones.

Project glossaries, localization authority, immutable tokens, placeholders, and control codes retain precedence. Target-language profiles add language-specific quality checks without changing this schema or core routing behavior.
