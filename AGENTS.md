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
4. **Keep the control plane text-free from entry.** Before entry, the top-level agent may confirm paths but must not list, open, search, grep, classify, or preview game-root files. Discovery and semantic checks belong to isolated data-plane workers. The root never directly parents one: it gives only the core wave artifact to a fresh relay with no planning, retry, or routing authority. Root receives only validated `parr0t.control-envelope.v2` metadata. On leakage, record it and stop; the contaminated agent cannot resume. A fresh root uses only the canonical handoff receipt under `docs/control-plane-contract.md`; prose handoffs and self-attestation alone are invalid.
5. **Preserve pipeline order.** Verify paired inventories; analyze typed context; run primary translation, failure-only translation fallback, complete low-cost triage, selective strong review for flags/risk/sample, flagged-only rewrite, failure-only judge fallback, validation, then packing.
6. **Treat refusal as routing.** Parent-facing refusal state is only `fallback_required` / “폴백 처리 필요”. It is never a content judgment, exclusion, batch cancellation, or authority to shrink scope. Preserve source only after metadata proves every configured route was attempted and exhausted.
7. **Translate, do not invent.** Preserve target-scoped narrative, functional, or knowledge context for every item in the prepared batch; target isolation does not require separate model calls. Preserve meaning, character voice, formality, scene state, UI function, conditions, units, order, terminology, placeholders, markup, control codes, supported allusions, and natural target-language effect.
8. **Keep source and runtime immutable by default.** Build new artifacts in the workspace. Inspection, building, testing, log reading, or access to a game directory does not authorize install, update, restore/remove, launch, cleanup, or execution. Each real runtime action requires its own explicit approval under `docs/runtime-approval.md`.
9. **Select and activate before implementing.** Follow `docs/sdk-selection.md` before format or data-plane work. Reuse or extend only an exact engine/version match and execute it from the bound SDK checkout. Code from another identity is reference evidence for `derive-new`, not permission to widen that SDK. Generated pass-through tools are not an adapter. `main` never imports engine-specific data from an SDK branch.
10. **Publication is dormant.** Do not propose, prepare, mention, commit, push, tag, publish, deploy, or create a release unless the user first requests that exact external action. SDK promotion requires confirmed patch success. Its engine-only commit must not touch `main` paths; check paths with `game-l10n verify-sdk-promotion --working-tree`, pin its base, and verify the final commit before packaging. Per-game publication requires `docs/instance-release.md` and its release gate.
11. **Keep review control deterministic.** Only `native-pass-step` advances waves. Translation/rewrite uses one semantic batch. Review starts with at most 2,048 candidates, locks successes, and binary-splits only failures. V6 workers read bound tasks, never full source. Root sees metadata only; no model controllers or worker delegation inside semantic workers. The relay dispatches exact bindings only. Unknown state stops.
12. **Keep provider mode fixed.** A selected preset defines the authorized providers and roles. A key does not change mode or promote a fallback. Native App workers and API providers are distinct; never emulate a native worker with another Codex process or silently replace the locked route.
13. **Bind every write to the current work lane.** Before writing, bind the turn to game, supervision, core, or SDK work and resolve its checkout and branch anew. Supervision and inspection stay read-only unless the current request names the mutation. Never inherit another lane's checkout or push plan; reject outgoing commits unrelated to the current request.
14. **Start large jobs linearly.** At 10,000+ records, use only two inventory scans and one reconciliation before context. Reuse attestations; no per-record startup artifacts, repeated discovery, proof files, or speculative tooling.

## Standard job sequence

1. Initialize the external workspace against the unopened, read-only game root; establish the entry gate and record the one-time Git choice.
2. Follow status through isolated locale/engine discovery, SDK selection and activation, paired complete inventories, and typed context. Stop before translation if context fails.
3. Run primary translation, failure-only fallback, compact triage, selective strong review, flagged-only rewrite, and review fallback. Queue operational failures; blocking failures hold the patch.
4. Build and verify a recoverable workspace patch without mutating or launching the game. Report states, counts, paths, queues, and next decision.

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
| Exact wording, small tone/style correction, or bounded data correction | `docs/minimal-corrections.md` |
| Composed UI text | `docs/segment-metadata.md` |
| Real install/update/remove/launch | `docs/runtime-approval.md` |
| Windows package | `docs/installer-contract.md` plus `docs/filesystem-packaging.md` or `docs/steam-packaging.md` |
| Steam discovery or Play-button integration | `docs/steam-faq.md`, `docs/wrapper-executable.md` |
| User-requested per-game publication | `docs/instance-release.md` |
| SDK branch maintenance/update | `docs/branch-update-contract.md` |
| Confirmed SDK promotion/publication | `docs/sdk-branch-contract.md`, `docs/engine-faq-contract.md` |
| Framework troubleshooting | `docs/faq.md` |
| Triggered engine exception | selected SDK branch `docs/engine-faq.md` under `docs/engine-faq-contract.md` |

Launcher-neutral packages accept absolute or startup-relative `-GameRoot`; omission uses the validated startup directory before prompting.

## Repository acceptance

- `main` contains only engine-neutral framework behavior. Engine-specific discovery, unpack/apply/pack logic, fixtures, and `docs/engine-faq.md` belong to `sdk-<engine>-<version>`.
- SDK content must be provenance-free. Use public format names and unmistakably synthetic fixtures only. Title-specific incidents and unresolved private evidence stay outside this repository.
- A deterministic engine problem belongs in code plus a regression test. Use an engine runbook only when safe continuation needs external tooling, user material, runtime evidence, authorization, or an uninferable decision.
- Before committing repository changes, run `python scripts/check_repo_privacy.py` and `python -m unittest discover -s tests -t . -v`.
- API keys belong only in environment variables and must never be printed or persisted.
