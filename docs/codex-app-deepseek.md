# Codex App + DeepSeek API workflow

This mode is the recommended hybrid setup: the signed-in Codex desktop app supplies native GPT-6 Luna translation workers and a separate GPT-6.1 Sol compact judge, while translation fallback, selective correction, and judge fallback may be sent to the DeepSeek API.

It is different from the `openai-deepseek` CLI preset:

| Mode | Primary translation | Primary review | External fallback | Required API keys |
|---|---|---|---|---|
| Codex App + DeepSeek API | Native GPT-6 Luna subagents | Separate native GPT-6.1 Sol compact judge | DeepSeek translation/rewrite/judge fallback | `DEEPSEEK_API_KEY` |
| OpenAI API + DeepSeek API | `gpt-6-luna` Responses API | `gpt-6.1-sol` compact judge | DeepSeek translation/rewrite/judge fallback | `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` |

The Codex App login is not an OpenAI API key. Do not copy account tokens, App credentials, or DeepSeek keys into prompts or files.

## Selecting the mode

When a user selects **Codex App + DeepSeek API**, the top-level Codex agent records that choice only in the private workspace and verifies `DEEPSEEK_API_KEY` without printing it. The selection plus the supplied key authorizes automatic DeepSeek fallback calls for the current localization run. Do not ask again for every refused line.

The SDK repository, Git history, neutral FAQ, and reusable engine branch must not record which mode or provider handled a particular game.

## Native agent topology

- **Top-level Codex/Sol:** owns orchestration state only. It receives target IDs, categorical statuses, counts, queue transitions, artifact paths, and deterministic issue codes. It must not open dialogue/input/result artifacts, receive candidates, read prose refusal explanations, or semantically approve translations.
- **GPT-6 Luna workers:** use as many native subagent slots as the active Codex host and configured concurrency allow. The parent gives each worker a private workspace path and one target ID, not dialogue in the task message. The worker reads exactly that target plus the relevant full-script context pack, character guide, scene state, adjacent lines, glossary, and immutable formatting rules from private files; it writes its candidate there and returns only a metadata envelope.
- **GPT-6.1 Sol judge:** must be a separate subagent/session from the top-level orchestrator. It reads the source/candidate by private artifact path and returns only a compact `ok`, `rewrite`, refusal, or error envelope. It never echoes a correct candidate to the parent.
- **DeepSeek API:** receives individual translation requests only for target IDs that the complete Luna stage refused or failed. In review, it may selectively rewrite `rewrite` targets and act as the next compact judge for Sol refusals/errors. Each request includes one target and focused relevant context. Never combine failed targets into a new batch prompt.

Subagent data-plane files use the JSON envelopes in [provider-protocol.md](provider-protocol.md). The message returned to the parent is a separate control envelope containing only `target_id`, categorical `status`, optional issue codes, and a private artifact path. Game text, candidates, results, and provider prose reasons remain in the private workspace.

## Required order

1. Analyze the entire ordered script and create the story, scene, relationship, and character-voice context pack.
2. Dispatch individual target requests across available Luna subagents. A sensitive target may be refused, but its neighboring ordinary targets continue independently.
3. Wait for every Luna worker. Normalize any refusal explanation to `provider_refusal` before returning control to the parent. Record refused IDs without source text in the audit trail, then send only those targets through DeepSeek as separate requests.
4. Collect every valid candidate and start the separate Sol compact judge. Judge one target at a time in script order with accumulated same-speaker style anchors.
5. Keep `ok` candidates unchanged. Send only `rewrite` candidates to the configured selective rewriter, validate the correction, and have the judge confirm it. After the complete Sol judge stage, send only refused, errored, invalid, or unconfirmed targets to the DeepSeek judge fallback as separate requests.
6. If DeepSeek quota is exhausted or all permitted routes fail, emit the exact original text with `source_preserved` and continue unrelated targets.
7. Write a new translated tree/archive/patch under the private agent workspace. Never modify the configured game root or installed game in place.

## Refusal containment

Every worker is instructed that localization is a bounded transformation of already-existing user-provided text, not new content generation, scene continuation, roleplay, or endorsement. Full narrative context remains available but is explicitly labeled as context-only evidence.

A worker may refuse only its one target. The parent must not:

- classify the entire batch from one target;
- cancel ordinary targets because a neighboring target was refused;
- regroup refused targets into a larger Luna or DeepSeek prompt;
- ask the user for another fallback approval after the mode was selected;
- let the top-level orchestrator rewrite or approve the translation itself.

Provider policies still apply. The workflow does not conceal content or bypass safeguards; it prevents avoidable batch-wide refusal and automatically routes genuine per-target refusals.

The DeepSeek adapter uses JSON Object output for translation and rewriting, an evaluated compact plain verdict for judging, non-thinking mode, bounded output limits, and the same faithful-transformation/review prompt. DeepSeek documents `temperature` as ineffective in non-thinking mode, so it is not used as a claimed quality control. These settings reduce unnecessary output and false-positive framing; they do not disable or evade the provider's safeguards.

A refusal from either translation or review is not a semantic verdict. The parent may only advance the target to the next configured route. It must not infer that the target should be removed, ask a worker to “work around” the refusal, inspect the dialogue itself, or cancel the patch. When every route is exhausted, the data-plane runner writes the exact source with `source_preserved`, so the patch remains structurally complete.

## Resume behavior

Keep queue and attempt state only under `.game-l10n/` in the private workspace. On resume, do not repeat completed Luna, DeepSeek, or Sol work whose input/context/provider fingerprint is unchanged. Restart only pending, refused, errored, or invalid target IDs according to the configured stage order.

Native workers write exclusive JSONL shards in the private workspace. Import each shard with `game-l10n import-native-shard`; after the Luna barrier, run `game-l10n merge-native-pass` against the ordered source segment file. The merge rejects overlapping IDs, sorts by source order rather than file append order, adds candidate generation/digest metadata, and reports only aggregate completeness to the parent.

This is the recommended deployment, not a core dependency. Native agents, API providers, command adapters, and local models can independently fill the translator, judge, rewriter, and fallback roles described in [provider-neutral review](review-pipeline.md).

See also [OpenAI + DeepSeek API preset](fallback-routing.md) and [Codex Work operation](codex-work.md).
