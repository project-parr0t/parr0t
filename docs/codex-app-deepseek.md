# Codex App + DeepSeek API workflow

This mode is the recommended hybrid setup: the signed-in Codex desktop app supplies native GPT-6 Luna translation and complete triage workers plus selective GPT-6.1 Sol strong judges, while translation fallback, selective correction, and judge fallback may be sent to the DeepSeek API.

It is different from the `openai-deepseek` CLI preset:

| Mode | Primary translation | Primary review | External fallback | Required API keys |
|---|---|---|---|---|
| Codex App + DeepSeek API | Native GPT-6 Luna subagents | Luna complete triage + selective native GPT-6.1 Sol | DeepSeek translation/rewrite/judge fallback | `DEEPSEEK_API_KEY` |
| OpenAI API + DeepSeek API | `gpt-6-luna` Responses API | Luna complete triage + selective `gpt-6.1-sol` | DeepSeek translation/rewrite/judge fallback | `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` |

The Codex App login is not an OpenAI API key. Do not copy account tokens, App credentials, or DeepSeek keys into prompts or files.

## Selecting the mode

Initialize this mode explicitly with `game-l10n init-workspace <workspace> --provider-preset codex-app-deepseek`. The CLI records `execution_mode=codex_app_native` and a text-free `.game-l10n/provider-mode.json` lock in the private workspace. The lock is immutable for that job: neither the agent nor the presence of an API key may change the preset, turn it into `custom`, or replace a native role with an API or command provider.

When a user selects **Codex App + DeepSeek API**, the top-level Codex agent verifies `DEEPSEEK_API_KEY` without printing it. The key unlocks only the preset's `translation_fallback`, `selective_rewrite`, and `judge_fallback` roles after their corresponding native primary barriers. It does not authorize context analysis, primary translation, primary review, OpenAI API calls, a child Codex process, or any other external provider. Once a declared fallback role becomes eligible, do not ask again for every refused line.

The SDK repository, Git history, neutral FAQ, and reusable engine branch must not record which mode or provider handled a particular game.

## Native agent topology

- **Top-level Codex/Sol:** owns orchestration state only. It receives validated `parr0t.control-envelope.v2` fields: target IDs, categorical statuses, counts, queue transitions, artifact paths, and allowlisted issue codes. Provider-specific refusals are normalized to the parent-safe `fallback_required` status before crossing this boundary. It must not open dialogue/input/result artifacts, receive candidates or semantic content summaries, read prose refusal explanations, anticipate provider policy, or semantically approve translations.
- **Inventory and context workers:** are private data-plane workers too. Use fresh no-history workers for semantic or source-bearing analysis. They write their inventories/context artifacts and parent envelope inside the workspace, then return only the envelope path. They report structural coverage and execution state—not subject-matter classifications or recommendations to omit or suppress a provider route.
- **GPT-6 Luna workers:** the root never starts them directly. It starts one fresh `isolated_wave_relay` from the exact path returned by `native-pass-step`; that relay launches only the bound no-history workers within the configured concurrency limit. The relay cannot inspect task payloads, plan, retry, split, change routes, or forward raw child responses. Every semantic worker receives one v6 task path, must not open the full ordered source, returns one ordered private result, and ends.
- **GPT-6 Luna triager:** is a fresh worker, separate from the translator. It judges every candidate in the bounded task once and returns compact per-target verdicts in one response. `rewrite`, refusal, error, explicit high-risk metadata, and the deterministic sample enter strong review.
- **GPT-6.1 Sol strong judge:** is separate from the top-level orchestrator, translators, and triagers. It receives only the selected targets, judges the bounded set in one response, and never echoes a correct candidate to the parent.
- **DeepSeek API:** receives individual translation requests only for target IDs that the complete Luna stage refused or failed. In review, it may selectively rewrite `rewrite` targets and act as the next compact judge for Sol refusals/errors. Each request includes one target and focused relevant context. Never combine failed targets into a new batch prompt.

Native means an actual Codex App collaboration worker. Do not run `codex.exe`, `codex exec`, a second Codex session, or any model-bearing command provider to simulate one. Do not ask the user for external-transfer approval as a workaround. If the CLI reports `E_PROVIDER_MODE_DRIFT` or `E_NATIVE_WORKFLOW_REQUIRED`, stop before opening text-bearing artifacts or constructing a prompt and restore the selected topology.

Subagent data-plane files use the JSON envelopes in [provider-protocol.md](provider-protocol.md). Every inventory, context, translation, review, rewrite, application, or validation worker writes the separate parent envelope defined in [control-plane-contract.md](control-plane-contract.md). The message returned to the parent is only its workspace-relative path. Game text, candidates, results, semantic summaries, and provider prose reasons remain in the private workspace.

## Required order

1. After two independent inventories pass, dispatch a fresh separate native Sol data-plane worker—not the top-level Sol—to analyze the entire ordered script and create the story, scene, relationship, and character-voice context pack. Validate only its metadata envelope in the parent.
2. Run `game-l10n prepare-native-pass` once, using `--pass-kind review` for judging, then run `game-l10n native-pass-step`. Do not open its relay artifact or dispatch task paths directly. Translation/rewrite uses one batch; review may split only failed rows under its manifest.
3. Give only the returned relay-artifact path to one fresh isolated relay. After one bounded wait, that relay runs `seal-native-wave-relay` and returns only its receipt path. Then call `native-pass-step` again. Do not dispatch semantic workers from the root, poll unchanged workers, create a worker per target, or send follow-up tasks to a completed worker. Keep provider refusal details private and expose only `fallback_required` to the parent.
4. Collect every valid candidate and run complete Luna triage in fresh no-history tasks. Keep current-generation triage-`ok` candidates unless the manifest selects them by high-risk metadata or the deterministic sample; route every non-`ok` result to strong review.
5. Send only selected targets to the separate Sol strong judge. Keep `ok` candidates unchanged; send only `rewrite` candidates to the configured rewriter, validate the correction, and have Sol confirm it. After the selective Sol stage, send only refused, errored, invalid, or unconfirmed selected targets to the DeepSeek judge fallback.
6. If DeepSeek quota is exhausted or all permitted routes fail, emit the exact original text with `source_preserved` and continue unrelated targets.
7. Write a new translated tree/archive/patch under the private agent workspace. Never modify the configured game root or installed game in place.

## Refusal containment

Every worker is instructed that localization is a bounded transformation of already-existing user-provided text, not new content generation, scene continuation, roleplay, or endorsement. Relevant typed context remains available but is explicitly labeled as context-only evidence.

A worker may refuse only the affected target or targets inside its batched result. The parent must not:

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

Keep queue and attempt state only under `.game-l10n/` in the private workspace. On resume, do not repeat completed Luna, DeepSeek, or Sol work whose input/context/provider fingerprint is unchanged. Restart only pending, refused, errored, or invalid target IDs according to the configured stage order.

All DeepSeek calls go through the configured compatible adapter. Do not write a job-local request driver, re-encode the exchange into another schema, or attach the whole bundle to every request; doing so bypasses prompt projection, request budgeting, cache telemetry, result validation, and resumable state.

Create translation/rewrite tasks with `game-l10n prepare-native-pass --workspace <workspace> --input <ordered-script> --output <private-task-directory>`; they default to 64 IDs and 24,000 estimated material tokens. Add `--pass-kind review` for judging; it defaults to 2,048 IDs and 262,144 estimated material tokens. Next run `game-l10n native-pass-step --workspace <workspace> --manifest <private-task-directory>/manifest.json --results-dir <private-wave-results>`. Dispatch only the exact wave it returns, wait once, and run the same step again. Do not select tasks from the manifest or invoke legacy import as recovery. Merge with the same manifest after the barrier.

This is the recommended deployment, not a core dependency. Other API, command, compatible, local, and native combinations remain available through an explicitly selected `openai-deepseek` or `custom` workspace. They may not be introduced by mutating a `codex-app-deepseek` workspace.

See also [OpenAI + DeepSeek API preset](fallback-routing.md) and [Codex Work operation](codex-work.md).
