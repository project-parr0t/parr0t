# AGENTS.md — Parr0t repository

Parr0t is engine-neutral. Keep game data, translations, prompts, caches, logs, credentials, job metadata, and title evidence in an external workspace.

## Read order

1. Read this file before accessing game-root contents; path existence may be checked only for workspace initialization.
2. Read `docs/agent-usage.md`, initialize or resume the external workspace without content inspection, then read its `AGENTS.md`.
3. Run `game-l10n status --workspace <workspace>`. For `establish_text_free_control_plane`, read its contracts and run `game-l10n enter-control-plane --workspace <workspace> --contracts-read --source-unopened` before other work.
4. Resolve each `required_contracts` entry against `contract_base` and read only those contracts. For a user-requested route outside workspace state, use the table below.

`local_patch_ready` is the only terminal local-work status. It requires current static completion and an unchanged packed artifact bound to that completion attestation.

## Non-negotiable rules

1. **Stay within the request.** Apply `skills/request-scope-guard/SKILL.md` whenever scope is limited, the task is inspection-only, or an action is optional, destructive, or externally consequential. Re-evaluate after every user message. Access and API-key availability are not authorization.
2. **Separate locations.** The repository holds reusable code only; the source game root is read-only; generated work belongs in an external workspace created with `game-l10n init-workspace`.
3. **Text is the default scope.** Do not inspect images for text, run OCR, plan image translation, or create image tooling unless the user specifically requests image translation.
4. **Keep the control plane text-free from entry.** Before the entry gate is established, the top-level agent must not list, open, search, grep, classify, or preview any game-root file, including README, manifests, configuration, scripts, assets, archives, or extracted text. Existing-locale detection, engine/version discovery, inventory, and every semantic/content check belong to isolated data-plane workers. The parent may receive opaque control IDs, counts, categorical states, queue transitions, artifact paths, public SDK-selection state, and deterministic issue codes only. Data-plane workers return a validated `parr0t.control-envelope.v2` path. On leakage, record it with `game-l10n record-control-plane-leak` and stop; the contaminated agent must not run or confirm resume. A fresh control plane resumes metadata checkpoints under `docs/control-plane-contract.md`.
5. **Preserve pipeline order.** Verify paired inventories; analyze typed context; run primary translation, failure-only translation fallback, complete low-cost triage, selective strong review for flags/risk/sample, flagged-only rewrite, failure-only judge fallback, validation, then packing.
6. **Treat refusal as routing.** Parent-facing refusal state is only `fallback_required` / “폴백 처리 필요”. It is never a content judgment, exclusion, batch cancellation, or authority to shrink scope. Preserve source only after metadata proves every configured route was attempted and exhausted.
7. **Translate, do not invent.** Preserve target-scoped narrative, functional, or knowledge context for every item in the prepared batch; target isolation does not require separate model calls. Preserve meaning, character voice, formality, scene state, UI function, conditions, units, order, terminology, placeholders, markup, control codes, supported allusions, and natural target-language effect.
8. **Keep source and runtime immutable by default.** Build new artifacts in the workspace. Inspection, building, testing, log reading, or access to a game directory does not authorize install, update, restore/remove, launch, cleanup, or execution. Each real runtime action requires its own explicit approval under `docs/runtime-approval.md`.
9. **Select and activate before implementing.** Follow `docs/sdk-selection.md` before format or data-plane work. Reuse or extend only an exact engine/version match and execute it from the bound SDK checkout. Code from another identity is reference evidence for `derive-new`, not permission to widen that SDK. Generated pass-through tools are not an adapter. `main` never imports engine-specific data from an SDK branch.
10. **Publication is dormant.** Do not propose, prepare, mention, commit, push, tag, publish, deploy, or create a release unless the user first requests that exact external action. SDK promotion additionally requires confirmed patch success. Its engine-only commit must not touch a path present on `main`; verify it with `game-l10n verify-sdk-promotion`. Per-game publication additionally requires `docs/instance-release.md` and its release gate.
11. **Keep review control deterministic.** After `prepare-native-pass`, only `game-l10n native-pass-step` selects or advances waves. Dispatch its exact returned wave after one bounded wait; each task keeps one semantic batch call. Root reads metadata only; no model controllers or worker delegation. Never choose recovery, models, batches, budgets, or resume. Unknown state means checkpoint and stop.
12. **Keep provider mode fixed.** A selected preset defines the authorized providers and roles. A key does not change mode or promote a fallback. Native App workers and API providers are distinct; never emulate a native worker with another Codex process or silently replace the locked route.

## Standard job sequence

1. Initialize the external workspace with the read-only game-root path without inspecting contents. Establish the entry gate, then ask once whether Git is allowed; refusal means no Git initialization or publication.
2. Run status. An isolated worker records existing-locale and engine/version discovery as SDK-selection state and returns only a control envelope. Complete SDK selection/activation; inspect an engine runbook only on its trigger.
3. Produce and reconcile two independently implemented complete ordered inventories. User-visible exclusions remain unresolved; static alignment is not runtime coverage.
4. Complete typed context analysis. If every context route fails, stop before translation.
5. Run primary translation, failure-only translation fallback, complete low-cost triage, selective strong review, flagged-only rewrite, and failure-only review fallback. Operational failures enter the resumable metadata queue; blocking failures hold the patch.
6. Build and verify a recoverable patch in the workspace. Never mutate or launch the supplied game during automated validation.
7. Report categorical completion states, counts, artifact paths, unresolved queues, and only the next required user decision. Do not expose private text.

## Load-on-demand contracts

| Trigger | Read |
| --- | --- |
| Start/resume or SDK choice | `docs/agent-usage.md`, `docs/sdk-selection.md`, `docs/sdk-branch-contract.md` |
| Construct a user-facing task prompt | `docs/prompt-examples.md` |
| Inventory, context, translation, or review | `docs/coverage-gates.md`, `docs/review-pipeline.md`, `docs/fallback-routing.md`, `docs/provider-protocol.md` |
| Control-plane worker or native pass | `docs/control-plane-contract.md`, `docs/execution-budget.md`, `docs/detail-contract.md` |
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

## Unity 2021.3

Read `docs/unity-2021.3.md`.
