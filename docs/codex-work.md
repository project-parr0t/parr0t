# Codex Work setup

Codex must be granted access to three distinct locations:

```text
translation-project-root/
├── agent-game-translation-sdk/   # Git repository; reusable code only
├── my-game-agent-work/           # private extracted/translated data
└── game-source-link-or-folder/   # read-only installed/unpacked source, when appropriate
```

The exact folders may live elsewhere if the Codex task has all of them as approved workspace roots. A convenient setup is one common parent project with the SDK and private workspace as siblings. Do not place `my-game-agent-work` inside the SDK repository.

The repository `AGENTS.md` is automatically injected by Codex when the task runs in the repository tree. `init-workspace` creates a second `AGENTS.md` in the private workspace. The bundled `game-localization` skill packages the repeatable workflow for Codex/ChatGPT Work plugin installation, while the CLI enforces the data boundary, approval state, full-script context gate, model fallback, and cache behavior.

Start a task with a request containing the three absolute paths and the intended source/target languages. Codex can then initialize the private workspace and report `ACTION_REQUIRED` if a human choice is required.

The top-level Codex/Sol agent is orchestration-only for translation quality: it may inspect schemas, target IDs, categorical status/error codes, hashes, counts, artifact paths, queue transitions, and deterministic validation codes, but semantic approval must come from a configured reviewer provider. It must not open dialogue/input/result files or receive source, candidate, translated text, or provider prose refusal explanations. It may edit unpack/pack adapters and configuration as ordinary implementation work.

Choose the execution mode explicitly. The `openai-deepseek` preset is API + API and uses `OPENAI_API_KEY` for GPT-6 Luna/Sol plus `DEEPSEEK_API_KEY` for fallback. The [Codex App + DeepSeek API workflow](codex-app-deepseek.md) instead uses native Luna/Sol subagents from the signed-in App and only `DEEPSEEK_API_KEY` externally. Do not silently switch between these authentication modes.

In either mode, use the maximum concurrency allowed by `routing.max_workers` and the active host, but give every worker exactly one translation target. Each worker still receives the full analyzed story/scene/relationship/character context as read-only evidence. Complete all Luna work before releasing only refused or failed target IDs to DeepSeek.

Review must run as a separate GPT-6.1 Sol agent/provider, not as semantic judgment by the top-level Sol orchestrator. Finish the Sol review stage before releasing only refused or failed reviews to DeepSeek. Selecting the preset and supplying its API-key environment variables is authorization for these configured fallback calls, so the orchestrator does not request approval again for each item. A provider refusal never becomes a batch refusal.

Native Codex workers and command adapters must return the same JSON envelope documented in [provider-protocol.md](provider-protocol.md). The deterministic SDK state machine remains authoritative for target isolation, fallback queues, integrity checks, `source_preserved`, and patch-only output.

For native App workers, the parent task message contains only the private workspace path, target ID, operation, and artifact destination. The worker reads data itself and returns a metadata-only control envelope; its text-bearing data envelope is written privately. Normalize any refusal explanation before parent delivery. A refusal can only cause a route transition and cannot be used by the top-level agent as a reason to omit dialogue or abandon patch creation.
