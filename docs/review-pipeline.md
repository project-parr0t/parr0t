# Provider-neutral review pipeline

The framework reviews by **role**, not by vendor or model name. Translation, compact judging, selective rewriting, and fallback judging may each use a different API, native App agent, command adapter, local model, or compatible endpoint.

## Cost-aware quality flow

```text
validated translation candidate
  -> compact judge: ok | rewrite | refused | error
       ok       -> keep the candidate without returning it again
       rewrite  -> selective rewriter -> deterministic integrity check
                                  -> compact confirmation judge
       refused  -> next configured judge
       error    -> bounded retry, then next configured judge
```

The judge returns only a verdict and categorical issue codes. It never echoes a correct translation. A rewriter runs only when the judge reports a material `meaning`, `omission`, `addition`, `voice`, `register`, `terminology`, `consistency`, `formatting`, `fluency`, `literalism`, `reference_loss`, `tone_rhythm`, `function`, `condition`, `unit`, `knowledge_order`, `length_risk`, or `slot_composition` problem. A denotationally accurate candidate still requires rewriting when its phrasing changes the intended effect, UI behavior, condition, measurement, information order, usable display length, or an evidenced label/value boundary. The corrected candidate must pass deterministic validation and another compact judgment before acceptance.

This selective rewrite optimization does not make review selective. Every newly translated candidate receives the initial compact judgment. When an existing translation is reused for a requested full re-review, every existing candidate also receives a fresh initial judgment through `game-l10n review-existing`; only the rewrite call remains selective. IDs, hashes, prior approval, and automatic risk flags cannot exempt a candidate from that pass.

`routing.review_max_rounds` bounds judge/rewrite cycles. The recommended value is `2`: one initial judgment and one confirmation after at most one rewrite. Increase it only when representative evaluations show that another correction cycle improves accepted quality enough to justify the extra calls.

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
    "reviewers": ["judge-primary", "judge-fallback"],
    "rewriters": ["rewriter-efficient", "rewriter-quality-fallback"],
    "review_max_rounds": 2,
    "review_refusal_policy": "fallback"
  }
}
```

- `reviewers` are compact judges. Refusal or operational failure advances to the next judge.
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

The recommended hybrid deployment uses native Codex App translation workers, a separate native compact judge, and a compatible API for refused translations, selective rewrites, and review fallback. It is recommended because it combines native agent concurrency with an independent external route; it is not a framework dependency.

Supported alternatives include:

- API translator + API judge + API rewriter, from one or several vendors;
- native App translator + native App judge without an external route;
- command adapters for Codex, OpenCode, internal runners, or local models;
- a low-cost judge paired with a stronger rewriter;
- a stronger judge paired with a low-cost rewriter and a stronger rewrite fallback.

Every combination must preserve the same one-target protocol, control-plane isolation, staged fallback, candidate generation/digest checks, deterministic validation, and exact-source preservation after route exhaustion.

The completion attestation counts the review denominator, approved candidate digests, reused candidates, and freshly reviewed reused candidates. A targeted audit may be useful, but it cannot set `semantic_review.complete=true` for candidates it never judged. Static inventory and runtime coverage are separate gates described in `docs/coverage-gates.md`.

The optimization defaults follow the official guidance to choose models by workload, use low reasoning effort for classification or routine rewrites, measure task success and token cost on representative evaluations, provide only relevant context, and use provider-supported thinking controls. References: [OpenAI API deployment checklist](https://developers.openai.com/api/docs/guides/deployment-checklist), [OpenAI model optimization](https://developers.openai.com/api/docs/guides/model-optimization), [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/).
