# OpenAI API + DeepSeek API fallback preset

The `openai-deepseek` preset is the explicit API + API option for private workspaces. Codex or ChatGPT Work may act as the top-level operator, but provider calls use API credentials from environment variables. A ChatGPT subscription is not treated as an API credential. New Codex App workspaces default to the separate locked `codex-app-deepseek` preset.

For native Luna/Sol workers supplied by the signed-in desktop app plus DeepSeek API fallback, initialize the separate locked `codex-app-deepseek` preset and follow the [Codex App + DeepSeek API workflow](codex-app-deepseek.md). Do not convert between the two presets because an API key happens to be available.

## Ready-to-run setup

Set both keys in the agent environment, then initialize a workspace:

```powershell
$env:OPENAI_API_KEY = "<openai-api-key>"
$env:DEEPSEEK_API_KEY = "<deepseek-api-key>"

game-l10n init-workspace <agent-workspace> `
  --provider-preset openai-deepseek `
  --game-root <read-only-game-root>

game-l10n doctor --workspace <agent-workspace>
```

The preset uses the current public API model identifiers `gpt-6-luna`, `deepseek-flash`, and `gpt-6.1-sol`. DeepSeek uses its OpenAI-compatible endpoint with JSON Object output for text-bearing transformations and the compact plain contract for judge verdicts. Model names remain editable in the private workspace configuration if account availability changes.

Selecting this preset and supplying its key environment variables authorizes its configured automatic provider calls. The agent must not pause for a second approval whenever an individual segment enters fallback. Keys remain in environment variables and are never written into the SDK, workspace configuration, audit log, or prompts.

## Ordered routing

1. Analyze the complete ordered source inventory with the context chain. Auto mode selects narrative, functional, knowledge, or hybrid analysis; the resulting pack records only evidence-backed domains and verifies their coverage.
2. Send one translation target per GPT-6 Luna request. Run up to `routing.max_workers` independent requests concurrently. Every request receives only its relevant typed context entries, adjacent lines, glossary, and control-code rules as context-only evidence.
3. Record refusal/error metadata without source or translated text. Finish the entire Luna stage before releasing only failed target IDs to DeepSeek.
4. After all translation routes finish, obtain compact `ok` or `rewrite` judgments in script order. An `ok` result keeps the existing candidate without making the judge echo it. Approved lines from the same speaker become style anchors for later judgments.
5. Send only `rewrite` candidates to the configured rewriter chain, run deterministic validation, and ask the compact judge to confirm the corrected candidate. Release only refused, errored, invalid, or unconfirmed review IDs to the next judge.
6. If every permitted route fails, including provider quota or rate-limit exhaustion, emit the exact source text with status `source_preserved`. This is an explicit unresolved state, never a claim that the line was translated or reviewed.

The top-level Codex/Sol operator observes completion, categorical refusals/errors, deterministic validation codes, counts, and queue transitions. It does not replace the independent semantic reviewer and must not open the input, candidate, or patch files. Provider prose explanations are normalized to codes such as `provider_refusal`, `quota_exhausted`, and `rate_limited` before control-plane logging.

## Refusal isolation

Translation is a bounded transformation of existing user-provided text, not generation of new content. Provider prompts explicitly prohibit continuation, roleplay, invention, censorship, and intensity changes. Mature content in any context-only domain must not turn an otherwise permitted target into a blanket refusal.

One provider request contains exactly one output target. Context remains available but is labeled as non-target evidence. A refusal therefore queues only that target; the orchestrator continues every unrelated target and never resubmits the refused collection as one larger fallback batch.

Provider policy still applies. The SDK does not obfuscate content or bypass safeguards; it minimizes unnecessary refusals and limits the effect of a genuine refusal to one target.

The DeepSeek-compatible translation and rewrite requests use JSON Object output, while the compact judge uses the evaluated plain `OK`/`REWRITE` contract. Requests use `thinking: {"type":"disabled"}`, `reasoning_effort: "none"`, and task-specific output limits. Compact judgments have a small fixed cap; translations and rewrites use source-length-bounded limits. DeepSeek documents `temperature` as ineffective in non-thinking mode, so the preset does not present it as a determinism control. These are supported non-thinking settings, not a vendor safety-disable switch. The system prompt identifies the request as faithful transformation/review of pre-existing text and forbids continuation, roleplay, invention, censorship, and intensity changes.

## Control-plane isolation

The CLI is the boundary between the top-level agent and game text. It prints aggregate counts and output paths only. Source, candidates, reviewed text, provider explanations, and context packs stay under the private workspace. The audit stream contains target IDs and categorical status/issue codes without dialogue. A refusal is therefore interpreted only as “route this target,” never as advice to omit it or abandon the patch.

If a run is interrupted, provider-stage state is retained under `.game-l10n/deferred/`. With unchanged input, context, settings, and provider fingerprints, resuming skips complete Luna, DeepSeek, or Sol stages and continues at the first incomplete stage. `--force` deliberately discards that staged resume decision.

Every valid candidate carries a content digest and monotonically increasing generation. A revised candidate advances the generation, and review attempts record which generation they examined. Resume state with a missing or mismatched candidate digest is discarded before review so an older decision cannot approve changed text.

Retryable malformed responses, temporary provider failures, rate limits, timeouts, and integrity-invalid outputs receive the configured `routing.grace_retries` attempts on the same isolated target. Explicit provider refusals, authentication failures, and exhausted quota move directly to the next route. Every attempt records its ordinal, model, categorical issue code, candidate generation, and duration without provider prose or game text.

`routing.review_refusal_policy` is explicit:

- `fallback`: use the next reviewer and preserve source after exhaustion when configured;
- `accept_candidate_unreviewed`: keep the translation candidate with `review_outcome=refused`, `acceptance_authority=user_policy`, and `user_accepted_after_review_refusal` rather than falsely marking it approved;
- `source_preserve`: keep the exact source after review exhaustion.

For Codex App native workers, import exclusive JSONL shards with `game-l10n import-native-shard`, then use `game-l10n merge-native-pass` to enforce unique target IDs, stable source ordering, candidate digests, and completeness before releasing fallback work.

## Read-only source and patch output

Configured game roots are input-only. Translation output must be a different file under the private agent workspace. Unpack and pack tools reject an output equal to, or nested inside, their input tree. Build translated data and replacement archives under the workspace, validate them there, and give the user a recoverable patch without modifying the installed game.

## Custom combinations

Use `--provider-preset custom` to create placeholder providers, then edit `providers` and the ordered `routing.translators`, `routing.reviewers`, and `routing.rewriters` lists in the private `game-l10n.json`. These roles may use different vendors, one vendor, native agents, command adapters, or local models. The same staged fallback, source preservation, review independence, and read-only source rules apply to every combination. See [provider-neutral review](review-pipeline.md).

API references: [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), [OpenAI models](https://developers.openai.com/api/docs/models), and [DeepSeek API quick start](https://api-docs.deepseek.com/guides/codex).
