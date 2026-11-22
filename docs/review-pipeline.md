# Provider-neutral review pipeline

A successful review retry counts toward semantic completion only when its approved verdict binds the current candidate generation. The current candidate digest must also match its text. Retry stage names and attempt ordinals remain in the audit history; a stale approval or an errored retry cannot approve a changed candidate.

The framework reviews by **role**, not by vendor or model name. Translation, compact judging, selective rewriting, and fallback judging may each use a different API, native App agent, command adapter, local model, or compatible endpoint.

Review follows the configured route pipeline; retries remain failed-only and successes are locked. Preset examples describe safe defaults, not fixed provider topology. Explicit route/model selections and configured retry counts remain authoritative; only missing settings receive documented defaults. Review remains isolated from the top-level control plane.

## Cost-aware quality flow

```text
validated translation candidate
  -> deterministic integrity checks
  -> complete low-cost triage: ok | rewrite | refused | error
       ok + ordinary, unsampled -> accept under the triage policy
       otherwise                -> selective strong judge
                                    ok -> keep candidate
                                    rewrite -> selective rewriter
                                               -> integrity check
                                               -> strong confirmation
                                    refused/error -> next strong judge
```

Triage and strong judges return only a verdict and categorical issue codes; neither echoes a correct translation. Every candidate receives fresh low-cost triage. Strong review is mandatory when triage flags or cannot decide, extraction/context metadata marks a supported high-risk category, an explicit evidence-backed risk flag is set, or the stable ID/category hash falls in `strong_review_sample_percent`. Two provenance reasons also escalate a triage `ok`: `context_reference_cue` when an analyzed reference cue names the target in its `evidence_line_ids`, and `fallback_translation` when the candidate came from a translation route other than the configured primary translator. Selection uses metadata only and is deterministic; the text-free control plane cannot read wording, infer risk, exempt an item, or add a new sampling rule.

Structured dialogue evidence adds three metadata-only escalation reasons: `dialogue_register_shift` for an evidenced shift, `dialogue_consistency_risk` for voice-state conflict, and `dialogue_context_uncertain` for unresolved context. See `docs/dialogue-localization.md`. These reasons route strong review; they do not authorize a guessed register.

For target languages with a stable script signal, the deterministic collector routes a candidate with alphabetic text but no target-script character to strong review as `target_script_absent`; a token target-script fragment also cannot mask a long, overwhelmingly Latin-script answer (`target_script_contamination`). A strong reviewer cannot waive the locked language: if its output still fails either check, the row remains rewrite/fallback work and cannot enter final delivery. Short all-caps technical tokens remain exempt. Proper names, identifiers, control tokens, and intentionally retained foreign text require both an allowed `target_language_exception` and its SHA-256 evidence in immutable segment metadata. A `reviewed` label alone never proves target-language compliance.

A rewriter runs only when a judge reports a material `meaning`, `omission`, `addition`, `voice`, `register`, `terminology`, `consistency`, `formatting`, `fluency`, `literalism`, `reference_loss`, `tone_rhythm`, `function`, `condition`, `unit`, `knowledge_order`, `length_risk`, or `slot_composition` problem. A denotationally accurate candidate still requires rewriting when its phrasing changes the intended effect, UI behavior, condition, measurement, information order, usable display length, or an evidenced label/value boundary. The corrected candidate must pass deterministic validation and strong confirmation before acceptance. Rewrite input carries every deterministic validator finding mapped into this vocabulary (for example `glossary-missing` -> `terminology`, `ui-length-limit` -> `length_risk`), then `fluency` for a target-language risk, plus at most four judge codes. The judge response remains capped at four; the deterministic core never drops a known hard gate to satisfy that semantic-response cap. For `meaning`, `omission`, or `addition`, the rewriter retranslates the source from scratch and uses the candidate only as a reference; other codes are minimal edits. When the segment binds `ui_max_chars`, the result must fit that non-negative Python character count, including whitespace, markup, and control tokens, without dropping protected tokens or meaning. A retry after a failed deterministic check receives that check's findings; the refusal-retry note is used only after an actual refusal.

A glossary entry may list `targets`: additional accepted renderings such as honorific or inflected forms. `target` stays canonical, and validation passes when any accepted rendering appears. `authority`, `source_language`, `concept_id`, and `target_ids` implement the precedence and entity binding in `docs/localization-authority.md`. For the same source identity, the highest authority wins; equal-authority conflicts remain visible. When source terms overlap, a longer phrase owns its covered occurrence; a shorter term is still required when it also appears independently. Numeric designations, homographs, model names, and identifier substrings must never be corrected by global replacement.

Speaker voice is an identity contract, not a global formality setting. Review compares baseline personality, cadence, address forms, and register against authoritative original-language evidence. It accepts a register shift only when relationship, hierarchy, scene-state, or progression evidence supports it. Source-absent explanatory parentheses are rejected when the segment opts into the authority policy, except when an applicable glossary target requires them.

`collect-native-review` also writes a private `review-metrics.json` with verdict and issue-code counts for the pass. Those counts are observational only and are never a stop, scope-reduction, or pass criterion.

Only an independently `reviewed` result may enter `approved_same_speaker_style_anchors`. A merely translated candidate or `accepted_unreviewed` result remains usable as its own candidate but is never relabeled as an approved voice example for later targets.

For native App review, compact output alone is not enough: each bounded review task runs in a fresh no-history worker and that worker ends after the task. Reusing a judge session for later tasks replays prior candidates and tool output, increases input superlinearly, and risks cross-target contamination without improving the current target's evidence.

Native review uses `prepare-native-pass --pass-kind review --review-tier triage|strong` with core-bound routes and task manifests. Packing values are configurable safe defaults, not universal hard ceilings; honor positive explicit bounds and host/model capacity. Each worker returns ordered categorical verdicts for its bound task in one semantic call.

Core imports valid results independently, locks successes, and retains missing or failed targets for continuation. Failed-subset manifests preserve provider/attempt bindings and cumulative usage. Review splitting follows the manifest's serialized depth and configurable optimization policy; do not impose hidden depth or amplification clamps. Repair-needed or not-yet-attempted targets are not semantic exhaustion.

Workers consume only their bound payload and focused context, not the complete source or prior result set.

Every current candidate requires current-generation review evidence: triage for unselected candidates and strong approval for selected ones. With no `review_triagers` route, all candidates go through `reviewers`.

`routing.review_max_rounds` and route/tier call limits are configured policy with safe defaults. User-selected limits replace model-name caps; preserve explicit existing values, including a configured Sol limit, and do not add a Sol-specific ceiling. Operational/native admission errors do not prove a provider attempt; dispatch counts are not measured usage.


Legacy manifests retain their original serialized contract and successes; continue pending targets under the current contract without resetting history or overwriting results.

If the approved join is incomplete after native passes end, use `resume-native-review-queues` with the verified primary inventory, the joined approvals and the exact earlier collector `--queue` artifacts. Core excludes approved targets and emits only unambiguous latest snapshots with their recorded candidate generations and attempt histories. Missing and conflicting histories remain held; the command does not start a review or change eligibility on any route.

After explicit user consent for one further fallback check on an exhausted cohort, prepare its Core-selected pending input with `prepare-native-pass --pass-kind strong_review --user-requested-additional-fallback-review` instead of `--output`. This binds one call per target to the ordered candidate snapshot and existing exact fallback route. The canonical manifest cannot be reissued with changed candidates, model, or output; automatic review splitting and global limit changes are not permitted.

Fallback routes default to three total judge attempts per target when no route-specific count is configured. `routing.review_attempt_limits` records explicit totals. Preserve explicit initial user/template model and count selections; a missing host binding or incompatible authorized transport leaves the exact request pending, not semantic failure or route exhaustion. Translation retries and triage coverage are separate policies.

Inspect or record an explicitly selected fallback model and its per-target total call count without changing provider mode or permissions:

```sh
game-l10n review-policy --workspace <workspace>
game-l10n review-policy --workspace <workspace> --fallback-model <exact-requested-model> --max-calls <user-total> --user-requested
```

`review-policy` binds an explicitly selected model and count to a configured fallback route (or to the selected native review-fallback role) without changing provider mode or permissions. Presets supply defaults, not immutable topology. Missing host binding or incompatible transport preserves the exact request pending; advertised availability is not permission. A selection never implicitly changes API/native execution.

Record only tier values supplied by the user or starting template. Omitted values retain safe defaults. Positive worker counts and task sizes are bounded by actual host/model capacity and resource-safe scheduling, not arbitrary universal caps. User call limits replace model-name caps; existing selected limits remain unchanged.

The command validates supplied model/count/tier settings before writing configuration; it does not require a whole-job usage ceiling or grant provider/model permissions.



After the complete review pass, continue any remaining eligible review work through the core-selected route. Residual task sizing follows configured safe defaults and explicit bounds; it never reopens approved candidates. Packaging and deterministic finalization do not spawn another reviewer unless candidate text changes.

Once the final verdict is imported, review is over. Package construction, README/release metadata, and deterministic integrity checks must not spawn another reviewer or request semantic reassurance. A finalization defect reopens only the exact failed deterministic gate unless it changes candidate text.

## Focused evidence, not context removal

The judge receives global style and terminology rules plus only the typed evidence attached to the target: current scene/speaker and same-speaker anchors for narrative text, relevant screen/feature entries for functional text, or relevant nodes/prerequisites for knowledge text. It also receives adjacent lines, segment metadata, glossary entries present in the source/candidate, relevant relationship entries, and up to four evidence-backed reference cues. The rewriter receives the same focused evidence plus categorical issue codes.

For dialogue, focused evidence includes the current target's addressees, relationship state, scene phase, speech act, delivery, situational register, address form, shift evidence, and consistency group. Same-speaker style memory prioritizes anchors with a compatible group, addressee, or relationship over merely recent lines.

Translation remains context-first. Focused review is not isolated sentence review: it removes unrelated entries while preserving the evidence needed to assess meaning, character voice, idiomatic expression, recognizability, pragmatic effect, UI behavior and constraints, knowledge structure, and function-specific tone or rhythm.

For composed UI, focused evidence includes the standard fields in `docs/segment-metadata.md`. The candidate must contain only the current slot payload, express any declared state semantics, and avoid repeating fixed labels or affixes rendered outside the slot. Explicit duplicate affixes and hard character limits are checked deterministically so model tokens are reserved for semantic judgment.

Reference cues are advisory evidence, not a license to invent. Context analysis records only high- or medium-confidence cues grounded in source wording or metadata. If no supported cue exists, the judge and rewriter must not force a familiar phrase, external work, joke, or canonical wording into the target.

## Configuration

```json
{
  "routing": {
    "translators": ["translator-primary", "translator-fallback"],
    "review_triagers": ["low-cost-triager"],
    "reviewers": ["strong-judge", "judge-fallback"],
    "rewriters": ["rewriter-efficient", "rewriter-quality-fallback"],
    "strong_review_sample_percent": 5.0,
    "strong_review_high_risk_categories": [
      "allusion", "critical_logic", "parody", "quest_title",
      "script_sensitive", "wordplay"
    ],
    "review_max_rounds": 2,
    "review_attempt_limits": {"judge-fallback": 3},
    "review_refusal_policy": "fallback"
  }
}
```

- `review_triagers` provide complete low-cost verdict coverage. A non-`ok` outcome routes the target to strong review; it never drops the target.
- `reviewers` are selective strong judges. Refusal or operational failure advances to the next strong judge.
- Explicit `review_attempt_limits` values take precedence; an unspecified fallback route uses the documented default of three total attempts per target. The configuration example is illustrative, not a request to overwrite explicit settings. A successful verdict stops early; counters include prior attempts and confirmations.
- `rewriters` are correction editors. They are called only for `rewrite` verdicts and tried in order.
- An empty `rewriters` list reuses the active judge as the rewriter for compatibility.
- Translation and review chains remain independent. Do not use the top-level orchestrator as the semantic judge.

Providers may define per-task output caps without changing their general context or translation limits:

```json
{
  "task_max_output_tokens": {
    "game_translation_review_judge": 128,
    "game_translation_review_rewrite": 2048
  }
}
```

For providers that reliably obey an exact response contract, `"review_verdict_format":"plain"` reduces the judge response to `OK`, `REFUSED`, `ERROR`, or a compact result such as `REWRITE literalism,reference_loss`. Keep `json` for providers whose plain-output compliance has not passed representative evaluation; malformed plain output is retried and then routed like any other protocol failure.

Choose limits with representative evaluations. Reasoning models may need a larger judge allowance than non-reasoning models even though the visible verdict is short. Provider-reported input, output, total, and reasoning token counts are normalized into text-free attempt metadata when available.

## Recommended and alternative deployments

The recommended hybrid deployment uses native Codex App Luna translation and triage defaults, selective native Sol strong-review defaults, and a compatible API for eligible translation fallback, selective rewrites, and judge fallback. These names and routes are defaults; roles remain provider-neutral and explicit workspace selections take precedence.

Supported alternatives include:

- API translator + API judge + API rewriter, from one or several vendors;
- native App translator + native App judge without an external route;
- command adapters for Codex, OpenCode, internal runners, or local models;
- a low-cost triager paired with a stronger selective judge and rewriter;
- one full-review judge chain by leaving `review_triagers` empty;
- a local triager paired with API strong judgment and rewrite fallback.

Every combination must preserve target-level outcome isolation, control-plane isolation, staged fallback, candidate generation/digest checks, deterministic validation, and exact-source preservation after route exhaustion. API adapters may use the configured one-target protocol; native App tasks use the manifest's bounded multi-target semantic batch and must not emulate that API isolation with a serial per-target call loop.

The completion attestation counts the review denominator, current-generation approved triage or strong verdicts, candidate digests, reused candidates, and freshly reviewed reused candidates. A sample alone cannot approve unsampled text: those candidates still require triage. Static inventory and runtime coverage are separate gates described in `docs/coverage-gates.md`.

The optimization defaults follow the official guidance to choose models by workload, use low reasoning effort for classification or routine rewrites, measure task success and token cost on representative evaluations, provide only relevant context, and use provider-supported thinking controls. References: [OpenAI API deployment checklist](https://developers.openai.com/api/docs/guides/deployment-checklist), [OpenAI model optimization](https://developers.openai.com/api/docs/guides/model-optimization), [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/).
