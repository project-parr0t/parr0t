# Codex App + DeepSeek API workflow

This mode is the recommended hybrid setup: the signed-in Codex desktop app supplies native GPT-6 Luna translation workers and a separate GPT-6.1 Sol compact judge, while translation fallback, selective correction, and judge fallback may be sent to the DeepSeek API.

It is different from the `openai-deepseek` CLI preset:

| Mode | Primary translation | Primary review | External fallback | Required API keys |
|---|---|---|---|---|
| Codex App + DeepSeek API | Native GPT-6 Luna subagents | Separate native GPT-6.1 Sol compact judge | DeepSeek translation/rewrite/judge fallback | `DEEPSEEK_API_KEY` |
| OpenAI API + DeepSeek API | `gpt-6-luna` Responses API | `gpt-6.1-sol` compact judge | DeepSeek translation/rewrite/judge fallback | `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` |

The Codex App login is not an OpenAI API key. Do not copy account tokens, App credentials, or DeepSeek keys into prompts or files.

## Selecting the mode

Initialize this mode explicitly with `game-l10n init-workspace <workspace> --provider-preset codex-app-deepseek`. The CLI records `execution_mode=codex_app_native` and a text-free `.game-l10n/provider-mode.json` lock in the private workspace. The lock is immutable for that job: neither the agent nor the presence of an API key may change the preset, turn it into `custom`, or replace a native role with an API or command provider.

When a user selects **Codex App + DeepSeek API**, the top-level Codex agent verifies `DEEPSEEK_API_KEY` without printing it. The key unlocks only the preset's `translation_fallback`, `selective_rewrite`, and `judge_fallback` roles after their corresponding native primary barriers. It does not authorize context analysis, primary translation, primary review, OpenAI API calls, a child Codex process, or any other external provider. Once a declared fallback role becomes eligible, do not ask again for every refused line.

The SDK repository, Git history, neutral FAQ, and reusable engine branch must not record which mode or provider handled a particular game.

## Native agent topology

- **Top-level Codex/Sol:** owns orchestration state only. It receives validated `parr0t.control-envelope.v2` fields: target IDs, categorical statuses, counts, queue transitions, artifact paths, and allowlisted issue codes. Provider-specific refusals are normalized to the parent-safe `fallback_required` status before crossing this boundary. It must not open dialogue/input/result artifacts, receive candidates or semantic content summaries, read prose refusal explanations, anticipate provider policy, or semantically approve translations.
- **Inventory and context workers:** are private data-plane workers too. Use fresh no-history workers for semantic or source-bearing analysis. They write their inventories/context artifacts and parent envelope inside the workspace, then return only the envelope path. They report structural coverage and execution state—not subject-matter classifications or recommendations to omit or suppress a provider route.
- **GPT-6 Luna workers:** use no more native subagent slots than the active Codex host and configured concurrency allow. Start every task as a fresh subagent with `fork_turns="none"` or the host's no-history equivalent. Give it one task path, the ordered source/context artifact paths, and the private workspace path—never game text or parent history in the task message. Inside the task, the worker handles one target at a time with only its relevant narrative, functional, or knowledge entries plus adjacent lines, glossary, and immutable formatting rules. It writes one result row per target, returns only a metadata envelope, and ends; never reuse it for the next task.
- **GPT-6.1 Sol judge:** uses the same fresh-worker/no-history/one-task rule and must be separate from the top-level orchestrator and translators. It reads the source/candidate by private artifact path and returns only a compact `ok`, `rewrite`, refusal, or error envelope. It never echoes a correct candidate to the parent.
- **DeepSeek API:** receives individual translation requests only for target IDs that the complete Luna stage refused or failed. In review, it may selectively rewrite `rewrite` targets and act as the next compact judge for Sol refusals/errors. Each request includes one target and focused relevant context. Never combine failed targets into a new batch prompt.

Native means an actual Codex App collaboration worker. Do not run `codex.exe`, `codex exec`, a second Codex session, or any model-bearing command provider to simulate one. Do not ask the user for external-transfer approval as a workaround. If the CLI reports `E_PROVIDER_MODE_DRIFT` or `E_NATIVE_WORKFLOW_REQUIRED`, stop before opening text-bearing artifacts or constructing a prompt and restore the selected topology.

Subagent data-plane files use the JSON envelopes in [provider-protocol.md](provider-protocol.md). Every inventory, context, translation, review, rewrite, application, or validation worker writes the separate parent envelope defined in [control-plane-contract.md](control-plane-contract.md). The message returned to the parent is only its workspace-relative path. Game text, candidates, results, semantic summaries, and provider prose reasons remain in the private workspace.

## Required order

1. After two independent inventories pass, dispatch a fresh separate native Sol data-plane worker—not the top-level Sol—to analyze the entire ordered script and create the story, scene, relationship, and character-voice context pack. Validate only its metadata envelope in the parent.
2. Run `game-l10n prepare-native-pass` once to create cached exclusive ID tasks, then dispatch at most the available concurrency. Use a fresh no-history worker for each task; it processes the bounded task sequentially and records refusal per target. A sensitive target must not cancel the rest of that task.
3. Use bounded waits for active workers and spawn a new worker for the next task only when a slot becomes available. Do not poll unchanged workers, create a worker per target, or send follow-up tasks to a completed worker. Keep provider refusal details private, return only `fallback_required` to the parent, then send those IDs through DeepSeek as separate requests after every primary task completes.
4. Collect every valid candidate and start the separate Sol compact judge. Judge one target at a time in script order with accumulated same-speaker style anchors.
5. Keep `ok` candidates unchanged. Send only `rewrite` candidates to the configured selective rewriter, validate the correction, and have the judge confirm it. After the complete Sol judge stage, send only refused, errored, invalid, or unconfirmed targets to the DeepSeek judge fallback as separate requests.
6. If DeepSeek quota is exhausted or all permitted routes fail, emit the exact original text with `source_preserved` and continue unrelated targets.
7. Write a new translated tree/archive/patch under the private agent workspace. Never modify the configured game root or installed game in place.

## Refusal containment

Every worker is instructed that localization is a bounded transformation of already-existing user-provided text, not new content generation, scene continuation, roleplay, or endorsement. Relevant typed context remains available but is explicitly labeled as context-only evidence.

A worker may refuse only its one target. The parent must not:

- classify the entire batch from one target;
- cancel ordinary targets because a neighboring target was refused;
- regroup refused targets into a larger Luna or DeepSeek prompt;
- ask the user for another fallback approval after the mode was selected;
- let the top-level orchestrator rewrite or approve the translation itself.

Provider policies still apply independently at each configured invocation. The parent does not pre-screen content or determine whether the fallback may receive it. A fresh no-history data-plane worker sends the exact, unmodified request to the already authorized fallback, and the parent records only `fallback_required`.

The DeepSeek adapter uses JSON Object output for translation and rewriting, an evaluated compact plain verdict for judging, non-thinking mode, bounded input/output limits, and the same faithful-transformation/review prompt. Each text-bearing request receives focused context only; a complete context pack must never be nested in segment metadata or repeated per target. Stable fields come first for provider prefix caching, and returned cache-hit/cache-miss usage remains visible in private attempt telemetry. The default 24,000-character preflight bound rejects an oversized text request before transmission while leaving that target pending. DeepSeek documents `temperature` as ineffective in non-thinking mode, so it is not used as a claimed quality control. These settings reduce unnecessary input/output and false-positive framing; they do not disable or evade the provider's safeguards.

A refusal from either translation or review is not a semantic verdict. The parent receives only `fallback_required` and may only advance the target to the next configured route. It must not infer that the target should be removed, inspect the dialogue, classify transfer eligibility, or cancel the patch. A generated worker contract must not add a pre-transfer eligibility gate. Only after v2 route evidence proves that every configured route was actually attempted and exhausted may the data-plane runner write the exact source with `source_preserved`.

If a worker pastes source, candidate text, content classification, or a prose refusal explanation into the parent conversation, do not summarize or use it. Follow [control-plane-contract.md](control-plane-contract.md): record a fixed leak incident, checkpoint metadata, stop the contaminated top-level agent, and resume in a fresh control plane. This is a state-isolation response, not a content-based omission.

## Resume behavior

Keep queue and attempt state only under `.game-l10n/` in the private workspace. On resume, do not repeat completed Luna, DeepSeek, or Sol work whose input/context/provider fingerprint is unchanged. Restart only pending, refused, errored, or invalid target IDs according to the configured stage order.

All DeepSeek calls go through the configured compatible adapter. Do not write a job-local request driver, re-encode the exchange into another schema, or attach the whole bundle to every request; doing so bypasses prompt projection, request budgeting, cache telemetry, result validation, and resumable state.

Create tasks with `game-l10n prepare-native-pass --workspace <workspace> --input <ordered-script> --output <private-task-directory>`. Version 2 defaults to 32 IDs and 12,000 estimated private material tokens per fresh worker task; tune them with `--max-records` and `--max-material-tokens`. Identical ordered input and bounds reuse the task manifest instead of rebuilding it. Each manifest declares no inherited history, no worker reuse, and metadata-only parent results. Native workers write exclusive result JSONL shards in the private workspace. Import each result with `game-l10n import-native-shard`; after the Luna barrier, run `game-l10n merge-native-pass` against the ordered source segment file. The merge rejects overlapping IDs, sorts by source order rather than file append order, adds candidate generation/digest metadata, and reports only aggregate completeness to the parent.

This is the recommended deployment, not a core dependency. Other API, command, compatible, local, and native combinations remain available through an explicitly selected `openai-deepseek` or `custom` workspace. They may not be introduced by mutating a `codex-app-deepseek` workspace.

See also [OpenAI + DeepSeek API preset](fallback-routing.md) and [Codex Work operation](codex-work.md).
