# OpenAI API + DeepSeek API fallback preset

The `openai-deepseek` preset is the explicit API + API option for private workspaces. Codex or ChatGPT Work may act as the top-level operator, but provider calls use API credentials from environment variables. A ChatGPT subscription is not treated as an API credential. New workspaces default to `codex-app-native`, with no preconfigured external adapter.

For native Luna/Sol workers supplied by the signed-in desktop app plus DeepSeek API fallback, initialize the separate locked `codex-app-deepseek` preset and follow the [Codex App + DeepSeek API workflow](codex-app-deepseek.md). Do not convert between the two presets because an API key happens to be available.

## Ready-to-run setup

Set the primary OpenAI key, then initialize a workspace. The DeepSeek key is optional until a verified fallback queue is non-empty:

```powershell
$env:OPENAI_API_KEY = "<openai-api-key>"

game-l10n init-workspace <agent-workspace> `
  --provider-preset openai-deepseek `
  --game-root <read-only-game-root>

game-l10n doctor --workspace <agent-workspace>
```

`doctor` reports a missing fallback-only credential under `deferred_credentials` without blocking primary translation or review. Set `DEEPSEEK_API_KEY` only when status or a sealed exchange proves that DeepSeek fallback work exists. `process-translation-exchange` checks the key immediately before the first executable DeepSeek fallback and never demands it for an empty or already completed bundle.

The preset uses the current public API model identifiers `gpt-6-luna`, `deepseek-flash`, and `gpt-6.1-sol`. DeepSeek uses its OpenAI-compatible endpoint with JSON Object output for text-bearing transformations and the compact plain contract for judge verdicts. Model names remain editable in the private workspace configuration if account availability changes.

Selecting this preset authorizes its configured automatic provider calls. Supplying a key makes only its already configured route executable; key presence never selects a mode. The agent must not pause for a second approval whenever an individual segment enters fallback. Keys remain in environment variables and are never written into the SDK, workspace configuration, audit log, or prompts.

Long-running direct translation, review, resume, and DeepSeek fallback commands start a loopback processing UI on an operating-system-selected free port. The URL is printed as metadata when processing begins and the server closes when processing ends. The page exposes aggregate counts, stage, percentage, and elapsed time only—never source text, translated text, target IDs, prompts, or refusal explanations. See [processing progress UI](progress-ui.md).

## Ordered routing

1. Analyze the complete ordered source inventory with the context chain. Auto mode selects narrative, functional, knowledge, or hybrid analysis; the resulting pack records only evidence-backed domains and verifies their coverage.
2. Send targets through the configured primary translator and its failed-only retry policy. Route, model, retry count, and attempt ordinal follow workspace configuration; preset values are defaults, not immutable provider assumptions. Lock each valid success.
3. Continue failed targets along the configured ordered translator chain. Import available results independently; partial tasks do not block eligible cohorts or deterministic repair. An operational error advances only when core selects an already-approved successor route. Repair-needed or never-attempted targets remain pending.
4. Review current candidates through configured triage/reviewer roles. Preserve current-generation approvals; correct only candidates whose verdict requires rewriting, then validate and confirm the correction.
5. Send only unresolved eligible review IDs to the next configured reviewer. Do not substitute routes based on availability alone.
6. `source_preserved` requires actual evidence that all applicable configured semantic routes were attempted and exhausted. It is unresolved source, never translated or reviewed coverage.

The top-level Codex/Sol operator observes completion, `fallback_required`, categorical operational errors, deterministic validation codes, counts, and queue transitions. It does not replace the independent semantic reviewer and must not open the input, candidate, or patch files. Provider refusal status and prose remain private; only `fallback_required` crosses the control-plane boundary. Operational codes such as `quota_exhausted` and `rate_limited` remain categorical metadata.

## Refusal isolation

Translation is a bounded transformation of existing user-provided text, not generation of new content. Provider prompts explicitly prohibit continuation, roleplay, invention, censorship, and intensity changes. Mature content in any context-only domain must not turn an otherwise permitted target into a blanket refusal.

One provider request contains exactly one output target. A refusal becomes `fallback_required` only for that target; unrelated work continues. The orchestrator does not pre-screen transfer eligibility. `source_preserved` requires actual evidence that applicable configured routes were attempted and exhausted; repair-needed, unavailable-host, denied-admission, and unattempted work remains pending.

If the next route is a manual translator or an adapter outside the direct provider pipeline, the handoff uses only `parr0t.translation-exchange/v1` as defined in [translation exchange](translation-exchange.md). The request carries the immutable source plus typed context and their digests. The route writes only fixed success or failure rows. The top-level agent sees the sealed receipt and v2 envelope, not the exchange contents, and cannot replace the contract with a job-local script or format.

Each provider applies its own policy when it receives the exact request. Parr0t does not obfuscate or alter content. It reports only `fallback_required` to the control plane and advances that one target through the already authorized route chain.

The configured adapter uses supported task-specific output formatting and limits, with safe defaults for compact judgments and source-length-aware translation/rewriting. DeepSeek documents `temperature` as ineffective in non-thinking mode, so the preset does not present it as a determinism control. These settings do not disable provider safeguards.

## Control-plane isolation

The CLI is the boundary between the top-level agent and game text. It prints aggregate counts and output paths only. Source, candidates, reviewed text, provider explanations, and context packs stay under the private workspace. The audit stream contains target IDs and categorical status/issue codes without dialogue. A refusal is therefore interpreted only as “route this target,” never as advice to omit it or abandon the patch.

If interrupted, provider-stage state remains under `.game-l10n/deferred/`. With unchanged input, context, settings, and provider fingerprints, resume skips completed work and continues pending eligible targets under the configured stage order. A user may stop or pause work; resuming an already authorized queue does not require a new user request.

Each valid API candidate is committed atomically as soon as its request finishes, so interruption replays only unfinished or invalid targets. A stage transition does not require every independent cohort to finish first; preserve route order for each target while allowing available work to proceed.

Every valid candidate carries a content digest and monotonically increasing generation. A revised candidate advances the generation, and review attempts record which generation they examined. Resume state with a missing or mismatched candidate digest is discarded before review so an older decision cannot approve changed text.

`routing.translator_retries` configures failed-only retries per route. Preset counts are defaults; explicit route/model/count selections remain authoritative and only missing limits receive defaults. Each retry uses the immutable target and focused context; successful results remain locked. Operational failures may advance to an already-approved successor when the core policy selects it. A missing/denied route or repair-needed target is not semantic exhaustion. Preserve attempt ordinals and categorical evidence; dispatch counts are not provider usage.

Per-request safeguards belong to the configured compatible adapter. It projects focused context and rejects unsafe oversized input before network transmission; such a target remains pending for repair and is not route exhaustion. Do not replace the adapter with a job-local request loop. Retain provider-reported cache telemetry when available.

`routing.review_refusal_policy` is explicit:

- `fallback`: use the next reviewer and preserve source after exhaustion when configured;
- `accept_candidate_unreviewed`: keep the translation candidate with `review_outcome=refused`, `acceptance_authority=user_policy`, and `user_accepted_after_review_refusal` rather than falsely marking it approved;
- `source_preserve`: keep the exact source after review exhaustion.

For native workers, import available results and retain incomplete targets for continuation. Stable IDs, order, candidate digests, and route binding remain validated; completeness of an entire wave is not a prerequisite for importing successes or progressing independent work.

## Read-only source and patch output

Configured game roots are input-only. Translation output must be a different file under the private agent workspace. Unpack and pack tools reject an output equal to, or nested inside, their input tree. Build translated data and replacement archives under the workspace, validate them there, and give the user a recoverable patch without modifying the installed game.

## Custom combinations

Use `--provider-preset custom` to create placeholder providers, then edit `providers` and the ordered `routing.translators`, `routing.reviewers`, and `routing.rewriters` lists in the private `game-l10n.json`. These roles may use different vendors, one vendor, native agents, command adapters, or local models. The same staged fallback, source preservation, review independence, and read-only source rules apply to every combination. See [provider-neutral review](review-pipeline.md).

For custom native chains, keep `execution_mode: custom_provider`; native routes are not API or command-provider aliases and need no placeholder adapter in `providers`. For example, configure `routing.translators` as `["native-luna", "native-sol", "native-qwen"]` and `routing.translator_retries` as `{"native-luna": 5, "native-sol": 3}`. This means six failed-only Luna attempts, four Sol attempts, then one Qwen attempt for only the remainder. Bind each additional route to its own exact host model:

```text
game-l10n native-runtime --workspace <workspace> --host omp --model native-qwen=<exact-model-id>
game-l10n prepare-native-pass --workspace <workspace> --input <ordered-source> --output <private-task-directory>
game-l10n native-pass-step --workspace <workspace> --manifest <private-task-directory>/manifest.json --results-dir <private-result-directory>
```

After the first merged pass, `start-native-translation-retries` reads the configured ordered native routes and retry counts; no route-plan CLI override is needed. For each pending subset, pass the returned `next_provider` and `next_attempt` to `prepare-native-pass --native-provider <route> --route-attempt <ordinal>`, using its returned input/output paths. Run every wave through the same `native-pass-step` isolated relay and advance with `advance-native-translation-retries`; successes stay locked. Native routing never skips an earlier external route. Once the native chain ends, `prepare-native-followup --retry-state <state>` and `collect-native-followup` join its candidates. An all-native chain creates no external exchange or credential requirement; exhausted failures remain explicit unresolved input.

Custom native reviews use `prepare-native-pass --pass-kind review --review-tier triage|strong`. Core selects the next configured `review_triagers` or `reviewers` route from current-candidate history; collect with `collect-native-review`. Prepare its failed-only `fallback-input.jsonl` through the same command to reach the next native judge. Flagged candidates use `--pass-kind rewrite`, the configured `rewriters`, and `collect-native-rewrite`; corrections still require a fresh strong confirmation. `--native-provider` may assert the selected route but cannot skip an earlier reviewer. `status` identifies this topology with `run_native_translation_review_pipeline` / `resolve_native_translation_review_gaps` and metadata-only `native_routes`. The locked `codex-app-deepseek` preset and its DeepSeek-only `process-native-review-followup`/`process-translation-exchange` processors are unchanged; never use them to execute a custom native fallback.

API references: [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), [OpenAI models](https://developers.openai.com/api/docs/models), and [DeepSeek API quick start](https://api-docs.deepseek.com/guides/codex).
