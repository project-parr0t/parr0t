# Codex App + DeepSeek API workflow

This mode is the recommended hybrid setup: the signed-in Codex desktop app supplies native GPT-6 Luna primary translation, native GPT-6.1 Sol pre-fallback translation, complete Luna triage workers, and selective Sol strong judges, while the remaining translation failures, selective correction, and judge fallback may be sent to the DeepSeek API.

It is different from the `openai-deepseek` CLI preset:

| Mode | Primary translation | Primary review | External fallback | Required API keys |
|---|---|---|---|---|
| Codex App + DeepSeek API | Native Luna first attempt + 5 retries, then native Sol first attempt + 3 retries | Luna complete triage + selective native GPT-6.1 Sol | DeepSeek translation/rewrite/judge fallback | `DEEPSEEK_API_KEY` |
| OpenAI API + DeepSeek API | Luna first attempt + 5 retries, then Sol first attempt + 3 retries | Luna complete triage + selective `gpt-6.1-sol` | DeepSeek translation/rewrite/judge fallback | `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` |

The Codex App login is not an OpenAI API key. Do not copy account tokens, App credentials, or DeepSeek keys into prompts or files.

## Selecting the mode

Initialize this mode explicitly with `game-l10n init-workspace <workspace> --provider-preset codex-app-deepseek`. The CLI records `execution_mode=codex_app_native` and a text-free `.game-l10n/provider-mode.json` lock in the private workspace. The lock is immutable for that job: neither the agent nor the presence of an API key may change the preset, turn it into `custom`, or replace a native role with an API or command provider.

When a user selects **Codex App + DeepSeek API**, the top-level Codex agent does not require `DEEPSEEK_API_KEY` during initialization, inventory, context, native translation, or native review. It verifies the key without printing it only after the corresponding native barrier has produced a non-empty `translation_fallback`, `selective_rewrite`, or `judge_fallback` queue. The key unlocks only that configured fallback route. It does not authorize context analysis, primary translation, primary review, OpenAI API calls, a child Codex process, or any other external provider. Once a declared fallback role becomes eligible, do not ask again for every refused line.

The SDK repository, Git history, neutral FAQ, and reusable engine branch must not record which mode or provider handled a particular game.

## Native agent topology

- **Top-level Codex/Sol:** owns orchestration state only. It receives validated `parr0t.control-envelope.v2` fields: target IDs, categorical statuses, counts, queue transitions, artifact paths, and allowlisted issue codes. Provider-specific refusals are normalized to the parent-safe `fallback_required` status before crossing this boundary. It must not open dialogue/input/result artifacts, receive candidates or semantic content summaries, read prose refusal explanations, anticipate provider policy, or semantically approve translations.
- **Inventory and context workers:** are private data-plane workers too. Use fresh no-history workers for semantic or source-bearing analysis. They write their inventories/context artifacts and parent envelope inside the workspace, then return only the envelope path. They report structural coverage and execution state—not subject-matter classifications or recommendations to omit or suppress a provider route.
- **GPT-6 Luna and GPT-6.1 Sol translation workers:** the root never starts them directly. It starts one fresh `isolated_wave_relay` from the exact path returned by `native-pass-step`; that relay launches only the route-bound no-history workers within the configured concurrency limit. The relay cannot inspect task payloads, plan, retry, split, change routes, or forward raw child responses. Every semantic worker receives one v7 task path, must not open the full ordered source, returns one ordered private result from one semantic call, and ends. The deterministic retry checkpoint locks successes and schedules only failed IDs: Luna gets five retries after its first attempt, then Sol gets three retries after its first attempt.
- **GPT-6 Luna triager:** is a fresh worker, separate from the translator. It judges every candidate in the bounded task once and returns compact per-target verdicts in one response. `rewrite`, refusal, error, explicit high-risk metadata, and the deterministic sample enter strong review.
- **GPT-6.1 Sol strong judge:** is separate from the top-level orchestrator, translators, and triagers. It receives only the selected targets, judges the bounded set in one response, and never echoes a correct candidate to the parent.
- **DeepSeek API:** receives individual translation requests only for target IDs that remain after the complete Luna and Sol translation retry barriers. In review, it may selectively rewrite `rewrite` targets and act as the next compact judge for Sol refusals/errors. Each request includes one target and focused relevant context. Never combine failed targets into a new batch prompt.

Native means an actual Codex App collaboration worker. Do not run `codex.exe`, `codex exec`, a second Codex session, or any model-bearing command provider to simulate one. Do not ask the user for external-transfer approval as a workaround. `E_PROVIDER_MODE_DRIFT` requires restoring the selected topology. `E_NATIVE_WORKFLOW_REQUIRED` means the attempted direct-provider command is the wrong path; keep the parent text-free, run `game-l10n status`, and use its reported `data_plane_task` or native-pass relay. It is not a terminal job state.

Subagent data-plane files use the JSON envelopes in [provider-protocol.md](provider-protocol.md). Every inventory, context, translation, review, rewrite, application, or validation worker writes the separate parent envelope defined in [control-plane-contract.md](control-plane-contract.md). The message returned to the parent is only its workspace-relative path. Game text, candidates, results, semantic summaries, and provider prose reasons remain in the private workspace.

## Required order

1. After two independent inventories pass, status exposes `context_analyzer`. Run `prepare-data-plane-task`, dispatch only that relay to a fresh separate native Sol worker—not the top-level Sol—and seal it. The worker analyzes the entire ordered script and writes the typed context pack privately; the core validates coverage and writes context state. The parent receives only the sealed receipt.
2. Run `game-l10n prepare-native-pass` for the first Luna translation attempt, then run `game-l10n native-pass-step`. Do not open its relay artifact or dispatch task paths directly. Each translation task makes one semantic call. Route and attempt are bound in the manifest; the relay cannot choose another model.
3. Give only the returned relay-artifact path to one fresh isolated relay. After one bounded wait and relay termination, that relay runs its bound `seal-native-wave-relay --relay-ended` command and returns only the receipt path. The core derives every still-missing bound task and converts it into an operational-error row without overwriting a valid result; the relay does not enumerate workers, invent another envelope, or expose their output. Then call `native-pass-step` again. Do not dispatch semantic workers from the root, poll unchanged workers, create a worker per target, or send follow-up tasks to a completed worker. Keep provider refusal details private and expose only `fallback_required` to the parent.
4. After the complete first Luna merge, run `start-native-translation-retries`. For each reported `native_retry`, prepare and execute exactly the returned pending input with its returned provider and attempt, merge it, and run `advance-native-translation-retries`. The checkpoint never reopens a success. Only when it reports `external_fallback` or `complete` may `prepare-native-followup --retry-state ...` create fixed `parr0t.translation-exchange/v1` bundles for the remaining targets. Run `process-translation-exchange` on each returned bundle; this command is the only core-owned Codex App adapter path to the locked DeepSeek `translation_fallback` role. Then run `collect-native-followup`.
5. Run complete Luna triage over that review input in fresh no-history tasks. Keep current-generation triage-`ok` candidates unless the manifest selects them by high-risk metadata or the deterministic sample; route every non-`ok` result to strong review.
6. Send only selected targets to the separate Sol strong judge. Keep `ok` candidates unchanged; send only `rewrite` candidates to the configured rewriter, validate the correction, and have Sol confirm it. After the selective Sol stage, send only refused, errored, invalid, or unconfirmed selected targets to the DeepSeek judge fallback.
7. If DeepSeek quota is exhausted or all permitted routes fail, emit the exact original text with `source_preserved` and continue unrelated targets.
8. Write a new translated tree/archive/patch under the private agent workspace. Never modify the configured game root or installed game in place.

## Refusal containment

Every worker is instructed that localization is a bounded transformation of already-existing user-provided text, not new content generation, scene continuation, roleplay, or endorsement. Relevant typed context remains available but is explicitly labeled as context-only evidence.

A worker may refuse only the affected target or targets inside its batched result. Refusal is eligible for the current route's configured failed-only retry budget; it is not a reason to mutate or soften the source. The parent must not:

- classify the entire batch from one target;
- cancel ordinary targets because a neighboring target was refused;
- regroup refused targets into a larger Luna or DeepSeek prompt;
- ask the user for another fallback approval after the mode was selected;
- let the top-level orchestrator rewrite or approve the translation itself.

Provider policies still apply independently at each configured invocation. The parent does not pre-screen content or determine whether the fallback may receive it. A fresh no-history data-plane worker sends the exact, unmodified request to the already authorized fallback, and the parent records only `fallback_required`.

The DeepSeek adapter uses JSON Object output for translation and rewriting, an evaluated compact plain verdict for judging, non-thinking mode, bounded input/output limits, and the same faithful-transformation/review prompt. Each text-bearing request receives focused context only; a complete context pack must never be nested in segment metadata or repeated per target. Stable fields come first for provider prefix caching, and returned cache-hit/cache-miss usage remains visible in private attempt telemetry. The default 24,000-character preflight bound rejects an oversized text request before transmission while leaving that target pending. DeepSeek documents `temperature` as ineffective in non-thinking mode, so it is not used as a claimed quality control. These settings reduce unnecessary input/output and false-positive framing; they do not disable or evade the provider's safeguards.

A refusal from either translation or review is not a semantic verdict. The parent receives only `fallback_required` and may only advance the target to the next configured route. It must not infer that the target should be removed, inspect the dialogue, classify transfer eligibility, or cancel the patch. A generated worker contract must not add a pre-transfer eligibility gate. Only after v2 route evidence proves that every configured route was actually attempted and exhausted may the data-plane runner write the exact source with `source_preserved`.

If a semantic worker pastes source, candidate text, content classification, or a prose refusal explanation, the disposable relay must not forward it. Quarantine that relay, preserve completed result files, and leave missing tasks pending. If any such content nevertheless reaches the root, record a fixed leak incident, stop the contaminated top-level agent, and resume only from the canonical digest-bound handoff receipt in a fresh control plane. This is state isolation, not a content-based omission.

## Resume behavior

Keep queue and attempt state only under `.game-l10n/` in the private workspace. On resume, do not repeat completed Luna, Sol, or DeepSeek work whose input/context/provider fingerprint is unchanged. Restart only pending, refused, errored, or invalid target IDs according to the configured stage order. The native retry state reports only route, attempt, counts, and paths; the control plane must not open its text-bearing candidate or pending files.

All DeepSeek calls go through the configured compatible adapter. Do not write a job-local request driver, re-encode the exchange into another schema, or attach the whole bundle to every request; doing so bypasses prompt projection, request budgeting, cache telemetry, result validation, and resumable state.

Create translation/rewrite tasks with `game-l10n prepare-native-pass --workspace <workspace> --input <ordered-script> --output <private-task-directory>`; they default to 64 IDs and a transport-safe 8,000 estimated material tokens. For judging, use `--pass-kind review --review-tier triage` for complete Luna triage and `--pass-kind review --review-tier strong` only for the selected Sol subset. The manifest binds the native provider, so a result from another model is rejected. Review uses structural hard ceilings of 2,048 IDs and 262,144 estimated material tokens; these are batch-shape limits, not whole-job usage limits. Next run `game-l10n native-pass-step --workspace <workspace> --manifest <private-task-directory>/manifest.json --results-dir <private-wave-results>`. Dispatch only the exact wave it returns, wait once, and run the same step again. Do not select tasks from the manifest or invoke legacy import as recovery. Merge with the same manifest after the barrier.

The fixed bridge after the first Luna merge is:

```text
game-l10n start-native-translation-retries --workspace <workspace> --input <ordered-script> --results <merged-luna-1.jsonl> --output <private-retry-dir>
# Repeat only while next_action=native_retry, using the returned provider/attempt/pending path:
game-l10n prepare-native-pass --workspace <workspace> --input <returned-pending-input> --output <attempt-task-dir> --native-provider <returned-provider> --route-attempt <returned-attempt>
game-l10n native-pass-step --workspace <workspace> --manifest <attempt-task-dir>/manifest.json --results-dir <attempt-results-dir>
game-l10n merge-native-pass --workspace <workspace> --input <returned-pending-input> --manifest <attempt-task-dir>/manifest.json --output <merged-attempt.jsonl>
game-l10n advance-native-translation-retries --workspace <workspace> --state <private-retry-dir>/retry-state.json --results <merged-attempt.jsonl>
# Only after next_action=external_fallback or complete:
game-l10n prepare-native-followup --workspace <workspace> --input <ordered-script> --primary-results <returned-pre-fallback-results> --retry-state <private-retry-dir>/retry-state.json --output <private-followup-dir> --exchange-prefix <stable-id>
game-l10n process-translation-exchange --workspace <workspace> --bundle <each-returned-fallback-bundle>
game-l10n collect-native-followup --workspace <workspace> --input <ordered-script> --followup <private-followup-dir> --review-output <review-input.jsonl> --unresolved-output <translation-unresolved.jsonl>
```

Do not build these subsets or candidate bindings with a job-local script. The bridge validates stable order, route/provider binding, retry ordinal, and source fingerprint; locks each first valid candidate; and partitions only failed targets into the next stage. Five and three are retry counts, so the maximum calls per still-failing target are six for Luna and four for Sol. Authentication or unavailable-route failures may advance early, but the control plane cannot decide this from prose. Do not ask for a whole-job budget before or during native work. DeepSeek fallback uses its configured per-request input preflight by default; only a whole-job limit the user stated first adds the optional ceiling and baseline arguments described in the translation-exchange contract. `collect-native-followup` exits nonzero while any translation route remains unresolved; do not skip the target or start review early.

This is the recommended deployment, not a core dependency. Other API, command, compatible, local, and native combinations remain available through an explicitly selected `openai-deepseek` or `custom` workspace. They may not be introduced by mutating a `codex-app-deepseek` workspace.

See also [OpenAI + DeepSeek API preset](fallback-routing.md) and [Codex Work operation](codex-work.md).
