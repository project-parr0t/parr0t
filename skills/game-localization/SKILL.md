---
name: game-localization
description: Run Parr0t's context-first, fallback-routed game translation and patch workflow. Use for game unpacking, text translation, review, repacking, continuation, or reusable SDK promotion; do not use for ordinary software localization.
---

# Parr0t game localization

Review control is a deterministic router: follow only validated status and
manifest actions. Do not inspect review text, invent a plan or file format,
repair/rebind the SDK, change model/batch/budget choices, or resume a halted
review autonomously. Checkpoint unknown or incomplete state. Never invent a
budget or replace a manifest; only user-set limits execute.

`game-l10n` owns execution; follow core/workspace instructions and status. Shutdown/diagnosis uses `framework-mode` under `docs/control-plane-contract.md`, suspending localization directives but not host safety/privacy.

## Run

1. Resolve three separate locations: Parr0t repository, read-only game root, and writable external workspace. Images require a user request, recorded policy, and `docs/image-localization.md`; never scan all images.
2. If the workspace does not exist, run `game-l10n init-workspace`. Ask once whether Git is allowed for this game task and record the answer with `game-l10n policy`.
3. Run `game-l10n status`. Before data-plane work, follow `docs/sdk-selection.md`; derive the actual engine/version identity, inspect plausible `sdk-*` contracts, select `reuse`, `extend`, `derive-new`, or `private-new`, and activate the exact executing implementation without broadening compatibility claims.
4. Use the selected SDK to create two independent complete ordered inventories and run `game-l10n verify-inventory`. Keep all user-visible unresolved scope visible. Static alignment never proves runtime coverage.
5. Run `game-l10n translate`: context → primary → failure fallback → complete triage → selective strong review → flagged rewrite → review fallback → validation. The parent routes metadata only. Native review uses `--pass-kind review`: up to 2,048 candidates, then failed-only binary split; translation sizing stays separate.
6. Pack to a new workspace output and verify only the changed layers. Do not install, launch, or mutate the game root.
7. Report counts, categorical coverage states, failure queues, artifact paths, and the next necessary decision only.

## Routing invariants

- Apply `request-scope-guard` after every user message that limits scope or before any optional, destructive, or external action. Workers receive only authorized targets and effects.
- Apply `translation-source-isolation` to every semantic worker. It defines default-deny external content access, exact task-bound grants, immutable policy/direct-model provenance, and taint rejection.
- Provider refusal is `fallback_required`, not an exclusion or semantic decision. Finish the primary pass before fallback. Preserve source only after all configured routes are proven exhausted.
- Each target retains relevant narrative/voice, functional/UI, or knowledge/prerequisite context even though provider calls isolate failures per target.
- Native workers start without parent history and return only a `parr0t.control-envelope.v2` path; semantic content never reaches the parent. Follow `docs/control-plane-contract.md`.
- Native v7 executes; v6 is completion-only. Use one batch, `native-pass-step`, and terminal `--relay-ended`; core locks successes and splits failed review subsets. Never loop per record, infer limits, enumerate workers, poll, or open the full source. Follow `docs/detail-contract.md`.
- Manual/fallback handoff uses only `parr0t.translation-exchange/v1`; the parent never opens its text-bearing bundle.
- Operational failures remain in the resumable metadata queue. Retry after a later explicit user request; if `metadata.blocking_on_failure=true`, hold the complete patch.
- A configured preset fixes provider roles. API-key presence cannot select or alter it. For `codex-app-deepseek`, follow `docs/codex-app-deepseek.md`.
- The source root is read-only. Real install, update, remove/restore, and launch are separate actions requiring explicit one-shot approval.
- Publication stays unmentioned and dormant until the user requests it. SDK promotion also requires confirmed patch success and exact target identity. Its separate non-merge engine commit must pass `game-l10n verify-sdk-promotion`; common changes belong in an independently authorized `main` commit.

## Fast routes

- Correction: `docs/minimal-corrections.md`; resolve a missing ID once in its local UI, freeze that export, preserve all else, and never repeat global search. Exact values use no model; generated wording uses one batch.
- Earlier release or updated game build: `docs/release-checkpoint.md`; reuse only still-compatible unchanged reviewed targets.
- Composed UI text: `docs/segment-metadata.md`.
- Launcher-neutral package: `docs/filesystem-packaging.md`; `-GameRoot` supports absolute and startup-current-directory-relative paths and defaults to the validated startup current directory when omitted.
- Steam package/integration: `docs/steam-packaging.md`, then `docs/steam-faq.md` and `docs/wrapper-executable.md` only if integration is requested.
- User-requested public instance: `docs/instance-release.md`.
- Successful reusable SDK promotion: `docs/sdk-branch-contract.md` and `docs/engine-faq-contract.md`.

Never copy private game data, text, translations, prompts, caches, logs, keys, or job provenance into the SDK repository.
