---
name: game-localization
description: Run Parr0t's context-first, refusal-aware game translation and patch workflow. Use when Codex is asked to unpack, translate, review, repack, or preserve reusable tooling for a game; do not use for ordinary software localization outside a game workspace.
---

# Parr0t game localization

Read the repository `AGENTS.md`, `docs/agent-usage.md`, and the target workspace `AGENTS.md` before acting. Use the Parr0t CLI as the execution boundary instead of manually translating strings.

1. Confirm Codex can read the SDK repository and source game folder and can write a separate agent workspace. Prefer sibling folders under one Codex project root. Never use the SDK repository as the data workspace.
2. If there is no workspace, run `game-l10n init-workspace <workspace> --sdk-root <repo> --game-root <game-folder>`. If Git choice is pending, ask the user at the start and record it with `game-l10n policy`; do not infer consent.
3. Run `game-l10n status --workspace <workspace>` and follow its next action. Adapt `tools/unpack.py` and `tools/pack.py` for the game format, keeping game data outside the repository.
4. Extract a complete, ordered script with stable IDs, speakers, and scenes. Do not start from isolated dialogue. For partial translation, keep the full script as `--context-input`.
5. Run `game-l10n translate`. It must complete context analysis before translation and independent review. Review uses a compact judge, selective rewriter, deterministic validation, and compact confirmation. The top-level Codex agent monitors status/refusals/fallbacks and deterministic integrity failures; it does not substitute its own semantic review.
6. Treat a provider refusal as a routing event. Let the SDK perform the bounded clarification retry and then use the next configured context, translation, or review provider. Do not obfuscate text or weaken provider safeguards.
7. Pack to a new output path in the agent workspace and validate it without overwriting the installed game unless the user explicitly asks.
8. Only after the user confirms the patch succeeded, prepare a sanitized engine profile with `promote-sdk`. Ask separately before commit/push, record approval, and only then use `publish-sdk` to create `sdk-<engine>-<version>` from `main`.
9. Keep the SDK provenance-free. Do not carry real game/project names, filenames, asset paths, map/event identifiers, dialogue, translations, private exclusions, job metadata, tool paths, hashes, counts, or reports into code, docs, fixtures, commit messages, or history. Use only public engine-format names and unmistakably synthetic fixtures.
10. Add recurring engine/SDK problems to `docs/faq.md` using symptom, general cause, diagnosis, resolution, and prevention. Remove all job provenance first. Do not record an incident that cannot be explained without private details; leave it in the external workspace instead.
11. Treat translation as transformation of one existing target at a time, never generation, continuation, or roleplay. Give each worker the full context pack, character guide, adjacent lines, glossary, and prior approved same-speaker anchors as context-only evidence. A refusal must affect only its target ID.
12. Route by configured roles rather than hard-coded provider names. The recommended hybrid uses native Codex translation workers, a separate compact judge, and an independent API for translation fallback, selective rewriting, and judge fallback. API-only, native-only, command, compatible, and local providers may fill each role. Finish the primary translation stage before fallback; return only compact review verdicts; rewrite only flagged targets; confirm corrections; and route only refused/failed judgments to the next judge. Never let the top-level orchestrator substitute its own semantic judgment.
13. Selecting the preset and supplying its key environment variables authorizes its configured automatic calls; do not ask again for every fallback item. If every permitted route or quota is exhausted, preserve the exact source with `source_preserved`. Write only a new patch under the private workspace and never mutate a configured game root.
14. Distinguish API + API from Codex App + API. For the recommended App mode, follow `docs/codex-app-deepseek.md`: use native translator subagents, a separate native compact judge, and the configured external API only for fallback or selective rewriting. Do not treat the signed-in App session as an API key.
15. Keep the top-level agent on the control plane. It may see target IDs, categorical status/error codes, counts, queue transitions, artifact paths, and deterministic issue codes, but must not open dialogue/input/result files or receive source, candidate, translated text, or prose refusal explanations. Have workers read and write private workspace files directly and return metadata-only envelopes.
16. Treat every translation/review refusal as a routing event, not a judgment that the content should be omitted. Normalize its explanation before returning to the parent, continue the staged fallback chain, and still produce the patch with exact source preservation only after all configured routes are exhausted. Use supported low-creativity generation settings; never claim or attempt to disable provider safeguards.

Never copy source game data, extracted strings, translations, caches, logs, prompts, or keys into the SDK repository.

## Unity 2021.3 branch

On `sdk-unity-2021.3`, read `docs/unity-2021.3.md` and run `game-l10n unity discover` before selecting a build path. Keep the existing Mono workflow for Mono players. For IL2CPP players, generate interop assemblies offline with a compatible private-workspace BepInEx toolchain, then compile the private bridge with `build-il2cpp`. Treat the player binary, metadata, loader references, and Unity base libraries as read-only inputs; keep generated assemblies and patch packages outside the SDK repository.
