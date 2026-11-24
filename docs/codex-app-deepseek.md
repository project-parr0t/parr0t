# Codex App + DeepSeek API workflow

This mode is the recommended hybrid setup: by default the signed-in Codex desktop app supplies native GPT-6 Luna primary translation, native GPT-6.1 Sol pre-fallback translation, complete Luna triage workers, and selective Sol strong judges, while the remaining translation failures, selective correction, and judge fallback may be sent to the DeepSeek API. These model names describe preset defaults, not immutable model choices. Explicit initial user/template exact role selections take precedence through recorded native-role overrides; all unspecified roles and routes retain their existing settings. The workflow's Luna/Sol/DeepSeek labels below describe the default routes, not permission to overwrite an explicit model selection.

It is different from the `openai-deepseek` CLI preset:

| Mode | Primary translation | Primary review | External fallback | Required API keys |
|---|---|---|---|---|
| Codex App + DeepSeek API | Native Luna, then native Sol; one initial call per route, zero same-route retries by default | Luna triage and selective native Sol strong review by default | Configured, role-authorized external adapters; DeepSeek initially | Key for the actual selected external adapter, only when invoked |
| OpenAI API + DeepSeek API | Luna, then Sol; one initial call per route, zero same-route retries by default | Luna triage and selective Sol strong review by default | Configured external fallback chain | Credentials for actual invoked routes |

The Codex App login is not an OpenAI API key. Do not copy account tokens, App credentials, or DeepSeek keys into prompts or files.

## Selecting the mode

Initialize explicitly with `game-l10n init-workspace <workspace> --provider-preset codex-app-deepseek`. The private provider-mode lock preserves native App transport; API-key presence cannot change it. Presets initialize—not freeze—role models, route lists, retry counts, and compatible-chat fallback adapters. Every external adapter must retain explicit allowed-role consent and credential binding. A changed endpoint, service, or data scope needs its actual authorization; a native primary cannot silently become an API or command process.

When a user selects **Codex App + DeepSeek API**, the top-level Codex agent does not require `DEEPSEEK_API_KEY` during initialization, inventory, context, native translation, or native review. It verifies the key without printing it when core exposes eligible `translation_fallback`, `selective_rewrite`, or still-DeepSeek-bound `judge_fallback` work; no complete-wave barrier is required. The key enables only that preset-configured fallback route; an explicitly selected native review fallback is never sent to DeepSeek instead. It does not authorize context analysis, primary translation, primary review, OpenAI API calls, a child Codex process, or any other external provider. Once a declared fallback role becomes eligible, do not ask again for every refused line.

The SDK repository, Git history, neutral FAQ, and reusable engine branch must not record which mode or provider handled a particular game.

## Native agent topology

Bind `native-runtime --workspace <workspace> --host codex|omp` before dispatch. Repeat `--role-model role=exact-id --user-requested-models` for selected roles: `translation`, `triage`, `strong-review`, `context`, `review-fallback`, and `rewrite`. Their routes are respectively `native-translation`, `native-triage`, `native-strong-review`, `native-context`, `native-review-fallback`, and `native-rewrite`. Explicit `--model route=exact-id` is also supported. Preserve provider/upstream/effort literally; OMP bare identities require a unique authoritative host metadata match, never a guessed provider prefix. Catalog availability is not permission. Unused legacy bindings are not required.

To replace a single configured external translation fallback with an explicitly approved native model, bind `--role-model translation-fallback=<exact-id> --user-requested-models`. Core uses `native-translation-fallback` at the existing fallback position and preserves the retry count, earlier routes, and the provider-mode lock. This does not replace an external rewrite or judge slot.

An explicit model-only change for the unstarted final native translation fallback takes effect at the next retry collection boundary. It does not change the current wave, earlier route attempts, or configured retry counts.

For example, `game-l10n native-runtime --workspace <workspace> --host omp --role-model translation=fixture/translator:low --role-model review-fallback=fixture/judge:high --user-requested-models` records only those two selections, without changing triage, strong review, context, or other routes. Record the fallback's model and per-target call count together with `game-l10n review-policy --workspace <workspace> --fallback-model fixture/judge:high --max-calls <user-total> --user-requested`. This binds the authorized native fallback instead of merely comparing the request with the default DeepSeek model. If its host/transport cannot yet be bound, preserve the exact model/count pending; do not substitute DeepSeek, infer semantic refusal, reset per-target counters, or reopen successes.

The text-free selection record is `config.model_policy = {"source":"user_prompt","native_roles":{"<selected-role>":"<exact-id>"}}`. Only listed native roles receive overrides; do not invent entries for omitted roles or treat that record as authorization to change provider mode, transport, endpoints, credentials, or a started worker's evidence.

- **Top-level Codex/Sol:** owns orchestration state only. It receives validated `parr0t.control-envelope.v2` fields: target IDs, categorical statuses, counts, queue transitions, artifact paths, and allowlisted issue codes. Provider-specific refusals are normalized to the parent-safe `fallback_required` status before crossing this boundary. It must not open dialogue/input/result artifacts, receive candidates or semantic content summaries, read prose refusal explanations, anticipate provider policy, or semantically approve translations.
- **Inventory and context workers:** are private data-plane workers too. Use fresh no-history workers for semantic or source-bearing analysis. They write their inventories/context artifacts and parent envelope inside the workspace, then return only the envelope path. They report structural coverage and execution state—not subject-matter classifications or recommendations to omit or suppress a provider route.
- **Native workers:** the root starts only the isolated relay selected by `native-pass-step`; bound workers use the configured exact models and routes, not fixed Luna/Sol identities. Each worker consumes one bound task and returns one ordered result. Retry counts and provider order come from current workspace configuration: preset values are defaults, while explicit selections remain authoritative.
- **Review roles:** triage and strong review are separate configured roles. They may use preset defaults or explicit model selections; selected targets and corrections follow current candidate-generation evidence.
- **External fallback:** DeepSeek is the preset's configured fallback route, not a substitute for an unavailable native or otherwise bound route. It processes only eligible targets, using the configured adapter and focused context. Partial tasks remain resumable; completed results stay locked.

Native means an actual Codex App collaboration worker. Do not run `codex.exe`, `codex exec`, a second Codex session, or any model-bearing command provider to simulate one. Do not ask the user for external-transfer approval as a workaround. `E_PROVIDER_MODE_DRIFT` requires restoring the selected topology. `E_NATIVE_WORKFLOW_REQUIRED` means the attempted direct-provider command is the wrong path; keep the parent text-free, run `game-l10n status`, and use its reported `data_plane_task` or native-pass relay. It is not a terminal job state.

Subagent data-plane files use the JSON envelopes in [provider-protocol.md](provider-protocol.md). Every inventory, context, translation, review, rewrite, application, or validation worker writes the separate parent envelope defined in [control-plane-contract.md](control-plane-contract.md). The message returned to the parent is only its workspace-relative path. Game text, candidates, results, semantic summaries, and provider prose reasons remain in the private workspace.

## Required order

1. After inventory verification, run the core-selected context task through its bound relay. Context workers claim disjoint chunks and commit slot-owned fragments; the finalizer merges them in source order. Worker concurrency and route/model are configured defaults, subject to explicit settings and host capacity. Partial receipts preserve completed fragments and expose remaining repair or operational work; do not require a complete wave or no-progress cycle before continuing.
2. Prepare the configured native translation pass and call `native-pass-step`. Dispatch only its exact relay artifact. Each task's route and attempt are manifest-bound; the relay cannot choose another model.

3. Let core import available results and retain missing/failed tasks. Continue with available independent tasks and deterministic repair; partial completion does not impose a whole-wave barrier. Do not dispatch workers directly or expose private result content.
4. Follow core retry state for failed targets, using only returned routes, attempts, and inputs. The preset's retry values are defaults; explicit configured values and user-selected routes take precedence. Preserve successes. Prepare external fallback bundles only when core marks those targets eligible, then collect partial results without losing awaiting work.
5. Run complete triage through the configured role over current candidates. Keep current-generation triage-`ok` candidates unless selected by configured high-risk metadata or sampling; route every non-`ok` result to strong review.
6. Strong review and correction use the configured roles. Preserve current-generation approvals and successful candidates; route only unresolved selected targets onward. A configured fallback may use DeepSeek or an explicitly bound native role, but no host/model availability signal itself grants permission.
7. Preserve source only when actual core evidence establishes exhaustion of the applicable configured semantic routes. Repair-needed, unavailable-host, admission-denied, or never-attempted targets are not semantic exhaustion; keep them pending and continue authorized repairs or alternate work.
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

The initially configured DeepSeek adapter uses focused transformation/review prompts and its declared output settings. Stable fields precede target-specific material for prefix caching; telemetry remains private. Per-request input/output bounds are explicit optional capacity controls, not a hidden 24,000-character or whole-job quota. Oversized input stays pending for safe sharding, compaction, or actionable repair; provider safeguards remain in force.

A refusal from either translation or review is not a semantic verdict. The parent receives only `fallback_required` and may only advance the target to the next configured route. It must not infer that the target should be removed, inspect the dialogue, classify transfer eligibility, or cancel the patch. A generated worker contract must not add a pre-transfer eligibility gate. Only after v2 route evidence proves that every configured route was actually attempted and exhausted may the data-plane runner write the exact source with `source_preserved`.

If a semantic worker pastes source, candidate text, content classification, or a prose refusal explanation, the disposable relay must not forward it. Quarantine that relay, preserve completed result files, and leave missing tasks pending. If any such content nevertheless reaches the root, record a fixed leak incident, stop the contaminated top-level agent, and resume only from the canonical digest-bound handoff receipt in a fresh control plane. This is state isolation, not a content-based omission.

## Resume behavior

Keep queue and attempt state only under `.game-l10n/` in the private workspace. On resume, do not repeat completed Luna, Sol, or DeepSeek work whose input/context/provider fingerprint is unchanged. Restart only pending, refused, errored, or invalid target IDs according to the configured stage order. The native retry state reports only route, attempt, counts, and paths; the control plane must not open its text-bearing candidate or pending files.

All DeepSeek calls go through the configured compatible adapter. Do not write a job-local request driver, re-encode the exchange into another schema, or attach the whole bundle to every request; doing so bypasses prompt projection, request budgeting, cache telemetry, result validation, and resumable state.

Create native tasks with `prepare-native-pass` using configured safe packing defaults or explicit positive bounds, sized for focused context, transport, and host/model capacity. The manifest binds the selected route and exact model. Run `native-pass-step` and dispatch its returned relay artifact; import available results and continue pending tasks. Review defaults are scheduling defaults, not universal hard ceilings or whole-job usage limits.

The retry bridge follows the configured core state. The command sketch below illustrates one route-specific retry; use only the provider, attempt, and pending paths returned by core:

```text
game-l10n start-native-translation-retries --workspace <workspace> --input <ordered-script> --results <merged-luna-1.jsonl> --output <private-retry-dir>
# Repeat only while next_action=native_retry, using the returned provider/attempt/pending path:
game-l10n prepare-native-pass --workspace <workspace> --input <returned-pending-input> --output <attempt-task-dir> --native-provider <returned-provider> --route-attempt <returned-attempt>
game-l10n native-pass-step --workspace <workspace> --manifest <attempt-task-dir>/manifest.json --results-dir <attempt-results-dir>
game-l10n merge-native-pass --workspace <workspace> --input <returned-pending-input> --manifest <attempt-task-dir>/manifest.json --output <merged-attempt.jsonl>
game-l10n advance-native-translation-retries --workspace <workspace> --state <private-retry-dir>/retry-state.json --results <merged-attempt.jsonl>
# Completed successes or genuinely native-exhausted targets may advance independently:
game-l10n export-native-translation-retry-cohort --workspace <workspace> --state-dir <private-retry-dir> --cohort <successful|native_exhausted>
game-l10n prepare-native-followup --workspace <workspace> --input <returned-cohort-input> --primary-results <returned-cohort-results> --retry-state <returned-cohort-lineage-export-path> --output <private-followup-dir> --exchange-prefix <stable-id>
game-l10n process-translation-exchange --workspace <workspace> --bundle <each-returned-fallback-bundle>
game-l10n collect-native-followup --workspace <workspace> --input <returned-cohort-input> --followup <private-followup-dir> --review-output <review-input.jsonl> --unresolved-output <translation-unresolved.jsonl>
```

Do not build retry subsets or candidate bindings with a job-local script. The core validates stable order, route/provider binding, attempt, and source fingerprint; locks each valid candidate and retains only unfinished targets for continuation. Retry counts are configured defaults unless explicitly selected. Authentication/admission errors may select another already-approved configured route; an unattempted or repair-needed task is not exhaustion. No whole-job budget is required. The adapter's per-request safety defaults still apply, and optional user-set whole-job limits apply only to dimensions actually selected. Partial exchange results remain resumable; no fresh human request is required to continue an already-authorized retry.

Other API and command workflows remain available through their explicitly selected modes. Native App jobs retain their mode/transport lock while accepting authorized exact role/model selections and consent-scoped compatible-chat fallback configuration; no key, catalog entry, or operational failure itself authorizes a new service, external primary, or model-bearing child process.

See also [OpenAI + DeepSeek API preset](fallback-routing.md) and [Codex Work operation](codex-work.md).
