---
name: game-localization
description: Run a context-first, refusal-aware game translation and patch workflow with the Agent Game Translation SDK. Use when Codex is asked to unpack, translate, review, repack, or preserve reusable tooling for a game; do not use for ordinary software localization outside a game workspace.
---

# Game localization

Read the repository `AGENTS.md` and the target workspace `AGENTS.md` before acting. Use the SDK CLI as the execution boundary instead of manually translating strings.

1. Confirm Codex can read the SDK repository and source game folder and can write a separate agent workspace. Prefer sibling folders under one Codex project root. Never use the SDK repository as the data workspace.
2. If there is no workspace, run `game-l10n init-workspace <workspace> --sdk-root <repo> --game-root <game-folder>`. If Git choice is pending, ask the user at the start and record it with `game-l10n policy`; do not infer consent.
3. Run `game-l10n status --workspace <workspace>` and follow its next action. Adapt `tools/unpack.py` and `tools/pack.py` for the game format, keeping game data outside the repository.
4. Extract a complete, ordered script with stable IDs, speakers, and scenes. Do not start from isolated dialogue. For partial translation, keep the full script as `--context-input`.
5. Run `game-l10n translate`. It must complete context analysis before translation and independent review. The top-level Codex agent monitors status/refusals/fallbacks and deterministic integrity failures; it does not substitute its own semantic review.
6. Treat a provider refusal as a routing event. Let the SDK perform the bounded clarification retry and then use the next configured context, translation, or review provider. Do not obfuscate text or weaken provider safeguards.
7. Pack to a new output path in the agent workspace and validate it without overwriting the installed game unless the user explicitly asks.
8. Only after the user confirms the patch succeeded, prepare a sanitized engine profile with `promote-sdk`. Ask separately before commit/push, record approval, and only then use `publish-sdk` to create `sdk-<engine>-<version>` from `main`.
9. Keep the SDK provenance-free. Do not carry real game/project names, filenames, asset paths, map/event identifiers, dialogue, translations, private exclusions, job metadata, tool paths, hashes, counts, or reports into code, docs, fixtures, commit messages, or history. Use only public engine-format names and unmistakably synthetic fixtures.
10. Add recurring engine/SDK problems to `docs/faq.md` using symptom, general cause, diagnosis, resolution, and prevention. Remove all job provenance first. Do not record an incident that cannot be explained without private details; leave it in the external workspace instead.

Never copy source game data, extracted strings, translations, caches, logs, prompts, or keys into the SDK repository.
