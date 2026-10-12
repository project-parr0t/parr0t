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

The top-level Codex/Sol agent is orchestration-only for translation quality: it may inspect schemas, status, hashes, and validation findings, but semantic approval must come from a configured reviewer provider. It may edit unpack/pack adapters and configuration as ordinary implementation work.
