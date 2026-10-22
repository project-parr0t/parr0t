# Provider-neutral review pipeline

A successful review retry counts toward semantic completion only when its approved verdict binds the current candidate generation. The current candidate digest must also match its text. Retry stage names and attempt ordinals remain in the audit history; a stale approval or an errored retry cannot approve a changed candidate.

The framework reviews by **role**, not by vendor or model name. Translation, compact judging, selective rewriting, and fallback judging may each use a different API, native App agent, command adapter, local model, or compatible endpoint.

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

Triage and strong judges return only a verdict and categorical issue codes; neither echoes a correct translation. Every candidate receives fresh low-cost triage. Strong review is mandatory when triage flags or cannot decide, extraction/context metadata marks a supported high-risk category, an explicit evidence-backed risk flag is set, or the stable ID/category hash falls in `strong_review_sample_percent`. Selection uses metadata only and is deterministic; the text-free control plane cannot read wording, infer risk, exempt an item, or add a new sampling rule.

A rewriter runs only when a judge reports a material `meaning`, `omission`, `addition`, `voice`, `register`, `terminology`, `consistency`, `formatting`, `fluency`, `literalism`, `reference_loss`, `tone_rhythm`, `function`, `condition`, `unit`, `knowledge_order`, `length_risk`, or `slot_composition` problem. A denotationally accurate candidate still requires rewriting when its phrasing changes the intended effect, UI behavior, condition, measurement, information order, usable display length, or an evidenced label/value boundary. The corrected candidate must pass deterministic validation and strong confirmation before acceptance.

For native App review, compact output alone is not enough: each bounded review task runs in a fresh no-history worker and that worker ends after the task. Reusing a judge session for later tasks replays prior candidates and tool output, increases input superlinearly, and risks cross-target contamination without improving the current target's evidence.

Prepare native judging with `prepare-native-pass --pass-kind review`. The public limits of 2,048 candidates and 262,144 estimated material tokens are hard ceilings, not initial packing targets. A new v7 review task starts at no more than 256 candidates and 65,536 estimated material tokens. The judge loads shared rules/context once and produces all bound `ok | rewrite | refused | error` verdicts in one semantic response. It must not create or submit one prompt per candidate.

Within that fresh worker, one semantic call covers the whole bound task and the worker ends. If a batch refusal, error, malformed response, or target failure remains, the deterministic core imports and locks successful verdicts, then emits a child manifest containing only failed targets at half the previous task bounds. Neither the root nor the relay inspects text, chooses target membership, or reopens successful rows. The core may descend at most 11 split levels and preserves cumulative usage across the chain. A one-record failed subset, a one-record task bound, or a failure remaining at the limit proceeds to the configured review fallback.

The worker reads only its bound v7 task payload plus the referenced focused context. It must not reopen the complete ordered source or prior result set for every batch. The full artifacts remain framework validation inputs; repeating them in each worker turns a linear pass into superlinear input cost without improving verdict evidence.

This is selective **strong** review, not selective review coverage. Every new or reused candidate receives a fresh triage verdict through the configured route. Prior approval never exempts a candidate. An unselected candidate counts as reviewed only when a current-generation `review-triage` approval and candidate digest both match; selected candidates require a current strong approval. With no `review_triagers` route, Parr0t retains the legacy behavior and sends every candidate through `reviewers`.

`routing.review_max_rounds` bounds judge/rewrite cycles. The recommended value is `2`: one initial judgment and one confirmation after at most one rewrite. Increase it only when representative evaluations show that another correction cycle improves accepted quality enough to justify the extra calls.

After a complete review pass, freeze the flagged target set. If no more than `execution.residual_direct_records` remain, process them as one cached residual task in one fresh no-history worker, then aggregate once. Do not create a new controller, ownership/lease layer, proof bundle, or supplemental context workflow for the tail, and never reopen already approved candidates. A capacity failure preserves the exact residual task for resumption instead of restarting the control turn.

Once the final verdict is imported, review is over. Package construction, README/release metadata, and deterministic integrity checks must not spawn another reviewer or request semantic reassurance. A finalization defect reopens only the exact failed deterministic gate unless it changes candidate text.

## Focused evidence, not context removal

The judge receives global style and terminology rules plus only the typed evidence attached to the target: current scene/speaker and same-speaker anchors for narrative text, relevant screen/feature entries for functional text, or relevant nodes/prerequisites for knowledge text. It also receives adjacent lines, segment metadata, glossary entries present in the source/candidate, relevant relationship entries, and up to four evidence-backed reference cues. The rewriter receives the same focused evidence plus categorical issue codes.

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
    "review_refusal_policy": "fallback"
  }
}
```

- `review_triagers` provide complete low-cost verdict coverage. A non-`ok` outcome routes the target to strong review; it never drops the target.
- `reviewers` are selective strong judges. Refusal or operational failure advances to the next strong judge.
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

The recommended hybrid deployment uses native Codex App Luna translation workers, fresh Luna triage workers, selective native Sol strong judges, and a compatible API for refused translations, selective rewrites, and strong-judge fallback. Sol is retained for the cases where its quality has the highest value instead of paying its cost on every ordinary candidate. The roles remain provider-neutral.

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
