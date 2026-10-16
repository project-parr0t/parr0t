# Codex Work setup

Codex must be granted access to three distinct locations:

```text
translation-project-root/
├── agent-game-translation-sdk/   # Git repository; reusable code only
├── my-game-agent-work/           # private extracted/translated data
└── game-source-link-or-folder/   # read-only installed/unpacked source, when appropriate
```

The exact folders may live elsewhere if the Codex task has all of them as approved workspace roots. A convenient setup is one common parent project with the SDK and private workspace as siblings. Do not place `my-game-agent-work` inside the SDK repository.

The repository `AGENTS.md` is automatically injected by Codex when the task runs in the repository tree. `init-workspace` creates a second `AGENTS.md` in the private workspace. The bundled `game-localization` skill packages the repeatable workflow for Codex/ChatGPT Work plugin installation, while the CLI enforces the data boundary, approval state, full-script context gate, model fallback, route-exhaustion evidence, and cache behavior.

Start a task with a request containing the three absolute paths and the intended source/target languages. Codex can then initialize the private workspace and report `ACTION_REQUIRED` if a human choice is required.

The top-level Codex/Sol agent is orchestration-only for translation quality: it may inspect schemas, target IDs, categorical status/error codes, hashes, counts, artifact paths, queue transitions, and deterministic validation codes, but semantic approval must come from a configured compact judge. It must not open dialogue/input/result files or receive source, candidate, translated text, or provider prose refusal explanations. It may edit unpack/pack adapters and configuration as ordinary implementation work.

Choose the execution mode explicitly. The `openai-deepseek` preset is API + API and uses `OPENAI_API_KEY` for GPT-6 Luna/Sol plus `DEEPSEEK_API_KEY` for fallback. The `codex-app-deepseek` preset in the [Codex App + DeepSeek API workflow](codex-app-deepseek.md) instead uses native Luna/Sol subagents from the signed-in App and only `DEEPSEEK_API_KEY` externally. Do not silently switch between these authentication modes. API-key presence does not choose a preset or authorize a role outside that preset.

In `codex-app-deepseek`, never spawn `codex.exe`, use `codex exec`, start a second Codex session, or wrap any of them as a command provider. Native workers are launched only through the host's collaboration mechanism. The private provider-mode lock prevents converting this preset to `custom`; an `E_PROVIDER_MODE_DRIFT` or `E_NATIVE_WORKFLOW_REQUIRED` error is a stop signal, not permission to build a job-local bridge.

In either mode, use no more concurrency than `routing.max_workers` and the active host. API requests still contain exactly one translation target. Native App workers should receive cached exclusive target-ID shards created by `prepare-native-pass`, then process one target at a time inside the shard with full analyzed story/scene/relationship/character context as read-only evidence. This avoids one worker launch per target while preserving per-target refusal isolation. Complete all primary work before releasing only refused or failed target IDs to fallback.

Review must run as a separate compact judge agent/provider, not as semantic judgment by the top-level orchestrator. Keep `ok` candidates without echoing them, send only `rewrite` verdicts to the configured correction chain, and confirm corrected candidates. Finish the primary judge stage before releasing only refused, failed, invalid, or unconfirmed targets to the next judge. The recommended App + DeepSeek mapping uses a separate native GPT-6.1 Sol judge and DeepSeek fallback, but the roles are provider-neutral. Selecting a preset and supplying its API-key environment variables authorizes these configured fallback calls, so the orchestrator does not request approval again for each item. A provider refusal never becomes a batch refusal.

Codex workspace access does not authorize runtime side effects. Treat every configured game root as read-only unless the user explicitly requests one exact `install`, `update`, `remove`, or `launch` action. “Test,” “validate,” “inspect logs,” and “check runtime data” are not synonyms for those actions. Follow `docs/runtime-approval.md`, consume the exact one-shot approval immediately before the side effect, and default to giving the artifact to the user for execution.

Native Codex workers and command adapters use the same data envelope documented in [provider-protocol.md](provider-protocol.md), but they belong to different explicitly selected execution modes. A command adapter must never impersonate a native worker. The parent sees only `fallback_required`, never a provider refusal explanation or content-based transfer decision. The deterministic SDK state machine remains authoritative for target isolation, fallback queues, route-exhaustion evidence, `source_preserved`, and patch-only output.

For native App workers, the parent task message contains only the private workspace path, target ID, operation, and artifact destination. The worker reads data itself and returns a metadata-only control envelope; its text-bearing data envelope is written privately. Normalize any refusal explanation before parent delivery. A refusal can only cause a route transition and cannot be used by the top-level agent as a reason to omit dialogue or abandon patch creation.
