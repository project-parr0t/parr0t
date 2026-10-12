# AGENTS.md — SDK repository

This repository contains reusable orchestration code only. Never place actual game files, extracted strings, translations, prompts, caches, logs, or credentials here.

## Required agent behavior

1. Create every per-game working directory outside this repository with `game-l10n init-workspace`.
2. At the beginning of a game task, ask whether Git use is allowed and record the choice with `game-l10n policy`. A refusal means no `git init`, branch, commit, or push for that task.
3. Analyze the complete ordered script before translating. Do not translate from isolated examples or dialogue alone. If all context-analysis routes fail or refuse, stop.
4. The top-level orchestrator monitors completion, refusal, malformed output, integrity checks, and fallback routing. It does not perform semantic review.
5. Use independent translation and review routes. A refusal or invalid result must move to the next configured provider.
6. Keep character voice, formality, forms of address, story/scene state, glossary terms, placeholders, markup, and control codes consistent.
7. Only after the user explicitly confirms that the translation/patch succeeded may a reusable unpack/pack tool be prepared with `game-l10n promote-sdk`.
8. After successful preparation, ask separately whether the sanitized profile may be committed/pushed. Only then record publish approval and use `game-l10n publish-sdk`.
9. Engine branches are named `sdk-<engine>-<version>`, branch from `main`, and contain only reusable SDK/tool files. Example: `sdk-unity-2023.1`.
10. Run `python scripts/check_repo_privacy.py` and `python -m unittest discover -s tests -t . -v` before committing.
11. Translation is a bounded transformation of existing user-provided text, never scene generation, continuation, roleplay, or endorsement. Translate one target segment per provider request while supplying the full analyzed story, scene, relationship, character-voice, adjacent-line, and glossary context as read-only evidence. A refusal for one target must never cancel, suppress, or contaminate unrelated targets.
12. When the user selects a provider preset and supplies its API-key environment variables, that selection authorizes automatic calls through the configured context, translation, and review chains; do not stop for a second per-item fallback approval. For `openai-deepseek`, complete all GPT-6 Luna translation requests first, queue only refused/failed targets for DeepSeek, complete GPT-6.1 Sol review next, and queue only refused/failed reviews for DeepSeek. Record refusal metadata without text. If every permitted route or the configured DeepSeek quota is exhausted, keep the exact source with status `source_preserved`.
13. Treat every configured game root as immutable. Translation files, rebuilt data, archives, and patches must be new outputs under the external agent workspace. Never overwrite or install into the source game unless the user separately requests installation after reviewing a recoverable patch.
14. Distinguish `OpenAI API + DeepSeek API` from `Codex App + DeepSeek API`. In App mode, use native GPT-6 Luna translator subagents and a separate native GPT-6.1 Sol reviewer; require only `DEEPSEEK_API_KEY` for the external fallback. The signed-in App session is not an API key. Follow `docs/codex-app-deepseek.md` and keep the top-level Sol agent orchestration-only.
15. Separate the data plane from the orchestration control plane. The top-level agent may receive target IDs, categorical status/error codes, counts, queue transitions, artifact paths, and deterministic issue codes only. It must not open dialogue/input/result files, receive candidate text, or receive a provider's prose refusal explanation. Workers read private files themselves, write results under the private workspace, and return metadata-only completion envelopes.
16. A translation or review refusal is only a routing event, never a content judgment and never grounds for omitting the target or cancelling patch creation. Normalize provider explanations to categorical codes before they reach the parent. DeepSeek requests use the faithful-transformation prompt and deterministic low-creativity settings supported by the API; do not claim or attempt to disable provider safeguards.

## Zero-provenance SDK rule

The SDK repository must reveal nothing about any localization job. This applies to the current tree, documentation, tests, fixtures, commit messages, branch descriptions, tags, and retained history.

- Never retain a real game title, developer/publisher, character or location name, dialogue, translation, source filename, asset/resource path, map/event identifier, job-specific tool/workspace path, model/provider used for a job, run count, timestamp, hash, report, or description of what private game data was edited.
- Engine-defined canonical names may appear only when required to implement or document the public file format. Test-only identifiers and text must be unmistakably synthetic (`fixture`, `synthetic`, `SOURCE_*`, `TARGET_*`) and must not be copied from a game.
- Reimplement the minimal reusable behavior from a private workspace; never copy its notes, examples, exclusions, fixtures, logs, or reports. Before publishing, inspect both the final diff and branch history. If provenance entered an unpublished branch, amend/squash it; if it reached a remote branch, rewrite that branch with `--force-with-lease` after explicit authorization.

## Neutral FAQ maintenance

Actively record reusable problems in `docs/faq.md`, but only after removing all provenance. An entry is eligible only when it follows from the SDK/engine design or can reasonably recur across independent projects.

- Use the format: symptom, general cause, diagnosis, resolution, prevention.
- Do not mention where or when the issue occurred, the affected game or files, actual job commands/paths, source or translated text, specific model/provider identities, counts, hashes, screenshots, or outcomes from a particular job.
- Keep project-specific incidents only in the external private workspace. If an issue cannot be useful without private details, do not add it to the repository FAQ.
- Update or merge an existing FAQ entry instead of accumulating job narratives.

API keys belong in environment variables. Do not print or persist them.
