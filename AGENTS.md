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

API keys belong in environment variables. Do not print or persist them.
