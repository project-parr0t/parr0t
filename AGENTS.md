# AGENTS.md — Parr0t repository

Parr0t is an engine-neutral control framework and reusable SDK repository. Keep all real game data, extracted text, translations, prompts, caches, logs, credentials, job metadata, and title-specific evidence in an external workspace.

## Read order

1. Read this file.
2. For a localization job, read `docs/agent-usage.md` and the external workspace `AGENTS.md`.
3. Run `game-l10n status --workspace <workspace>`.
4. Resolve each `required_contracts` entry against `contract_base` and read only those contracts. For a user-requested route not represented by workspace state, use the table below. Do not preload every document.

`local_patch_ready` is the only terminal local-work status. It requires current static completion and an unchanged packed artifact bound to that completion attestation.

## Non-negotiable rules

1. **Stay within the request.** Apply `skills/request-scope-guard/SKILL.md` whenever scope is limited, the task is inspection-only, or an action is optional, destructive, or externally consequential. Re-evaluate after every user message. Access and API-key availability are not authorization.
2. **Separate locations.** The repository holds reusable code only; the source game root is read-only; generated work belongs in an external workspace created with `game-l10n init-workspace`.
3. **Text is the default scope.** Do not inspect images for text, run OCR, plan image translation, or create image tooling unless the user specifically requests image translation.
4. **Keep the control plane text-free.** The top-level agent may receive opaque control IDs, counts, categorical states, queue transitions, artifact paths, and deterministic issue codes only. It must not open source/candidate/result text or receive provider refusal prose. Data-plane workers return a validated `parr0t.control-envelope.v2` path. On leakage, record it with `game-l10n record-control-plane-leak` and stop; the contaminated agent must not run or confirm resume. A fresh control plane resumes metadata checkpoints under `docs/control-plane-contract.md`.
5. **Preserve the pipeline order.** Verify two independent ordered inventories; complete typed context analysis; translate every confirmed target with the primary route; process only failures through translation fallback; independently review the complete translated set; rewrite only flagged targets; process only review failures through judge fallback; then validate and pack.
6. **Treat refusal as routing.** Parent-facing refusal state is only `fallback_required` / “폴백 처리 필요”. It is never a content judgment, exclusion, batch cancellation, or authority to shrink scope. Preserve source only after metadata proves every configured route was attempted and exhausted.
7. **Translate, do not invent.** Work on one target at a time with its relevant narrative, functional, or knowledge context. Preserve meaning, character voice, formality, scene state, UI function, conditions, units, order, terminology, placeholders, markup, control codes, supported allusions, and natural target-language effect. Segment isolation must not discard context.
8. **Keep source and runtime immutable by default.** Build new artifacts in the workspace. Inspection, building, testing, log reading, or access to a game directory does not authorize install, update, restore/remove, launch, cleanup, or execution. Each real runtime action requires its own explicit approval under `docs/runtime-approval.md`.
9. **Select and activate before implementing.** Follow `docs/sdk-selection.md` before format or data-plane work. Reuse or extend only an exact engine/version match and execute it from the bound SDK checkout. Code from another identity is reference evidence for `derive-new`, not permission to widen that SDK. Generated pass-through tools are not an adapter. `main` never imports engine-specific data from an SDK branch.
10. **Publication is dormant.** Do not propose, prepare, mention, commit, push, tag, publish, deploy, or create a release unless the user first requests that exact external action. SDK promotion additionally requires the user to confirm a successful patch. Per-game publication additionally requires `docs/instance-release.md` and its release gate.
11. **Use proportional effort.** Follow `docs/execution-budget.md`; reuse unchanged checkpoints and passed validations, cap repeated failures, keep small coupled work in the current agent, and stop after the required checks pass.
12. **Keep provider mode fixed.** A selected preset defines the authorized providers and roles. A key does not change mode or promote a fallback. Native App workers and API providers are distinct; never emulate a native worker with another Codex process or silently replace the locked route.

## Standard job sequence

1. Resolve repository, read-only game root, and writable external workspace. Ask once whether Git may be used for the game task and record the answer; refusal means no Git initialization or publication.
2. Run `game-l10n status`, complete SDK inventory/evaluation/selection/activation, and inspect an engine runbook entry only when its trigger occurs.
3. Produce and reconcile two independently implemented complete ordered inventories. User-visible exclusions remain unresolved; static alignment is not runtime coverage.
4. Complete typed context analysis. If every context route fails, stop before translation.
5. Run primary translation, translation fallback, complete independent review, selective rewriting, and review fallback in that order. Operational failures enter the resumable metadata-only failure queue; blocking failures hold the patch.
6. Build and verify a recoverable patch in the workspace. Never mutate or launch the supplied game during automated validation.
7. Report categorical completion states, counts, artifact paths, unresolved queues, and only the next required user decision. Do not expose private text.

## Load-on-demand contracts

| Trigger | Read |
| --- | --- |
| Start/resume or SDK choice | `docs/agent-usage.md`, `docs/sdk-selection.md`, `docs/sdk-branch-contract.md` |
| Construct a user-facing task prompt | `docs/prompt-examples.md` |
| Inventory, context, translation, or review | `docs/coverage-gates.md`, `docs/review-pipeline.md`, `docs/fallback-routing.md`, `docs/provider-protocol.md` |
| Control-plane worker or native pass | `docs/control-plane-contract.md`, `docs/execution-budget.md` |
| Codex App + DeepSeek preset | `docs/codex-app-deepseek.md` |
| Manual or external fallback exchange | `docs/translation-exchange.md` |
| Existing release, game update, or selective continuation | `docs/release-checkpoint.md` |
| Exact user-supplied wording correction | `docs/minimal-corrections.md` |
| Composed UI text | `docs/segment-metadata.md` |
| Real install/update/remove/launch | `docs/runtime-approval.md` |
| Windows package | `docs/installer-contract.md` plus `docs/filesystem-packaging.md` or `docs/steam-packaging.md` |
| Steam discovery or Play-button integration | `docs/steam-faq.md`, `docs/wrapper-executable.md` |
| User-requested per-game publication | `docs/instance-release.md` |
| Confirmed SDK promotion/publication | `docs/sdk-branch-contract.md`, `docs/engine-faq-contract.md` |
| Framework troubleshooting | `docs/faq.md` |
| Triggered engine exception | selected SDK branch `docs/engine-faq.md` under `docs/engine-faq-contract.md` |

For launcher-neutral Windows packages, `-GameRoot` accepts absolute paths and paths relative to the startup current directory; when omitted, the validated startup current directory is tried before prompting.

## Repository acceptance

- `main` contains only engine-neutral framework behavior. Engine-specific discovery, unpack/apply/pack logic, fixtures, and `docs/engine-faq.md` belong to `sdk-<engine>-<version>`.
- SDK content must be provenance-free. Use public format names and unmistakably synthetic fixtures only. Title-specific incidents and unresolved private evidence stay outside this repository.
- A deterministic engine problem belongs in code plus a regression test. Use an engine runbook only when safe continuation needs external tooling, user material, runtime evidence, authorization, or an uninferable decision.
- Before committing repository changes, run `python scripts/check_repo_privacy.py` and `python -m unittest discover -s tests -t . -v`.
- API keys belong only in environment variables and must never be printed or persisted.

## Unity 2022.3 prototype branch

Read `docs/unity-2022.3.md` for exact capability bounds. Bundle INI extraction remains incomplete; an independent inventory is mandatory. Standalone helpers support v22 little-endian object relocation, Font payload replacement and caller-supplied UI metrics. Build workspace artifacts and preserve unowned data. Static metrics do not prove runtime font or layout behavior; load the engine runbook only for a matching trigger.
