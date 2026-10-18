---
name: game-localization
description: Run Parr0t's context-first, fallback-routed game translation and patch workflow. Use for game unpacking, text translation, review, repacking, continuation, or reusable SDK promotion; do not use for ordinary software localization.
---

# Parr0t game localization

Review control is a deterministic router: follow only validated status and
manifest actions. Do not inspect review text, invent a plan or file format,
repair/rebind the SDK, change model/batch/budget choices, or resume a halted
review autonomously. An unknown or incomplete state must be checkpointed and
stopped. Continuation neither fills missing budget fields nor replaces a
regenerated, validated manifest.

Use `game-l10n` as the execution boundary. Read the repository `AGENTS.md`, `docs/agent-usage.md`, and the workspace `AGENTS.md`; afterward resolve only the status response's `required_contracts` against its `contract_base`.

## Run

1. Resolve three separate locations: Parr0t repository, read-only game root, and writable external workspace. Do not perform OCR or image translation unless the user requested it.
2. If the workspace does not exist, run `game-l10n init-workspace`. Ask once whether Git is allowed for this game task and record the answer with `game-l10n policy`.
3. Run `game-l10n status`. Before data-plane work, follow `docs/sdk-selection.md`; derive the actual engine/version identity, inspect plausible `sdk-*` contracts, select `reuse`, `extend`, `derive-new`, or `private-new`, and activate the exact executing implementation without broadening compatibility claims.
4. Use the selected SDK to create two independent complete ordered inventories and run `game-l10n verify-inventory`. Keep all user-visible unresolved scope visible. Static alignment never proves runtime coverage.
5. Run `game-l10n translate`: typed context → primary translation → failure-only translation fallback → complete low-cost triage → selective strong review → flagged-only rewrite → failure-only review fallback → deterministic validation. The top-level agent routes metadata only.
6. Pack to a new workspace output and verify only the changed layers. Do not install, launch, or mutate the game root.
7. Report counts, categorical coverage states, failure queues, artifact paths, and the next necessary decision only.

## Routing invariants

- Apply `request-scope-guard` after every user message that limits scope or before any optional, destructive, or external action. Workers receive only authorized targets and effects.
- Provider refusal is `fallback_required`, not an exclusion or semantic decision. Finish the primary pass before fallback. Preserve source only after all configured routes are proven exhausted.
- Each target retains relevant narrative/voice, functional/UI, or knowledge/prerequisite context even though provider calls isolate failures per target.
- Native workers start fresh with no parent history and return only a `parr0t.control-envelope.v2` path. The parent must not receive source, candidates, results, refusal prose, or semantic review details. Follow `docs/control-plane-contract.md`.
- The native v5 manifest is the only executable task-detail contract. A multi-target task uses one semantic batch call and one ordered batched result; do not create per-record prompt files or calls inside it. Follow `docs/detail-contract.md`, and stop after the pilot wave when its bulk-budget gate lacks user-approved limits.
- Manual/fallback handoff uses only `parr0t.translation-exchange/v1`; the parent neither opens the text-bearing bundle nor invents another format.
- Operational failures remain in the resumable metadata queue. Retry after a later explicit user request; if `metadata.blocking_on_failure=true`, hold the complete patch.
- A configured preset fixes provider roles. API-key presence cannot select or alter it. For `codex-app-deepseek`, follow `docs/codex-app-deepseek.md`.
- The source root is read-only. Real install, update, remove/restore, and launch are separate actions requiring explicit one-shot approval.
- Publication stays unmentioned and dormant until the user requests it. SDK promotion also requires confirmed patch success and exact target identity. Its separate non-merge engine commit must pass `game-l10n verify-sdk-promotion`; common changes belong in an independently authorized `main` commit.

## Fast routes

- Exact approved wording change: `docs/minimal-corrections.md`; no model/context pass, preserve unnamed targets, repack once, verify once.
- Earlier release or updated game build: `docs/release-checkpoint.md`; reuse only still-compatible unchanged reviewed targets.
- Composed UI text: `docs/segment-metadata.md`.
- Launcher-neutral package: `docs/filesystem-packaging.md`; `-GameRoot` supports absolute and startup-current-directory-relative paths and defaults to the validated startup current directory when omitted.
- Steam package/integration: `docs/steam-packaging.md`, then `docs/steam-faq.md` and `docs/wrapper-executable.md` only if integration is requested.
- User-requested public instance: `docs/instance-release.md`.
- Successful reusable SDK promotion: `docs/sdk-branch-contract.md` and `docs/engine-faq-contract.md`.

## Unity 2022.3 prototype branch

Read `docs/unity-2022.3.md` before selecting this profile. Use it only for its declared Unity 2022.3 Mono and single-UnityFS/ordered-INI `TextAsset` shape. Its bundle traversal is one inventory path; require an independent second inventory before context analysis or translation.

Never copy private game data, text, translations, prompts, caches, logs, keys, replacement bundles, or job provenance into the SDK repository.
