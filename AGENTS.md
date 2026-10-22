# AGENTS.md — Parr0t repository

Parr0t is engine-neutral. Keep all game, credential, title, and job data in an external workspace.

## Read order

1. Read this file before accessing game-root contents; path existence may be checked only for workspace initialization.
2. Read `docs/agent-usage.md`, initialize or resume the workspace without content inspection, then read its `AGENTS.md`.
3. Run `game-l10n status --workspace <workspace>`. For `establish_text_free_control_plane`, read its contracts and run `game-l10n enter-control-plane --workspace <workspace> --contracts-read --source-unopened` before other work.
4. Resolve each `required_contracts` entry against `contract_base`; read only those contracts. For other user-requested routes, use the table.

Only `local_patch_ready` is terminal; it requires current static completion and an unchanged packed artifact bound to its attestation.

## Non-negotiable rules

1. **Stay within the request.** Apply `skills/request-scope-guard/SKILL.md` whenever scope is limited, the task is inspection-only, or an action is optional, destructive, or externally consequential. Re-evaluate after every user message. Access and API-key availability are not authorization.
2. **Separate locations.** This repository holds reusable code; the game root is read-only; generated work belongs in a `game-l10n init-workspace` workspace.
3. **Text is the default scope.** Do not inspect images for text, run OCR, plan image translation, or create image tooling unless the user specifically requests image translation.
4. **Keep the control plane text-free from entry.** Before entry, the top-level agent may confirm paths but must not inspect game-root contents. Discovery, inventory, reconciliation, and typed context analysis use the exact relay from `prepare-data-plane-task`; translation, review, and rewrite use the relay from `native-pass-step`. A fresh relay has no planning, retry, or routing authority; root receives only validated `parr0t.control-envelope.v2` metadata. On leakage, record it and stop. Resume only through the canonical handoff receipt in `docs/control-plane-contract.md`.
5. **Preserve pipeline order.** Verify paired inventories; analyze typed context; run primary translation, failure-only fallback, complete low-cost triage, selective strong review, flagged-only rewrite, failure-only judge fallback, validation, then packing.
6. **Treat refusal as routing.** Parent-facing refusal state is only `fallback_required` / “폴백 처리 필요”. It is never a content judgment, exclusion, batch cancellation, or authority to shrink scope. Preserve source only after metadata proves every configured route was attempted and exhausted.
7. **Translate, do not invent.** Preserve target-scoped narrative, functional, or knowledge context; isolation does not require separate calls. Preserve meaning, voice, formality, scene, UI function, conditions, units, order, terminology, placeholders, markup, control codes, supported allusions, and natural effect.
8. **Keep source and runtime immutable by default.** Build in the workspace. Inspection or testing never authorizes install, update, restore/remove, launch, cleanup, or execution. Each runtime action needs explicit `docs/runtime-approval.md` approval.
9. **Select and activate before implementing.** Follow `docs/sdk-selection.md` first. Bind exact-match reuse/extension to an inventoried Git checkout/worktree or authenticated portable snapshot with matching full-content fingerprint. Game-workspace Git choice is unrelated. Other identities are `derive-new` references only. Pass-through tools are not adapters; `main` never imports engine data.
10. **Publication is dormant.** Do not propose, prepare, mention, commit, push, tag, publish, deploy, or create a release unless the user first requests that exact external action. SDK promotion requires confirmed patch success. Its engine-only commit must not touch `main` paths; check paths with `game-l10n verify-sdk-promotion --working-tree`, pin its base, and verify the final commit before packaging. Per-game publication requires `docs/instance-release.md` and its release gate.
11. **Keep review control deterministic.** Only `native-pass-step` advances waves. Translation/rewrite uses one semantic batch. Review hard ceilings are 2,048 records/262,144 estimated tokens; initial tasks cap at 256/65,536. Workers make one semantic call. Core locks successes and emits smaller failed-only manifests. V7 workers never read full source. Root sees metadata only; no model controllers or worker delegation. Unknown state stops.
12. **Keep provider mode fixed.** A selected preset defines the authorized providers and roles. A key does not change mode or promote a fallback. Native App workers and API providers are distinct; never emulate a native worker with another Codex process or silently replace the locked route.
13. **Bind every write to the current work lane.** Before writing, bind the turn to game, supervision, core, or SDK work and resolve its checkout and branch anew. Supervision and inspection stay read-only unless the current request names the mutation. Never inherit another lane's checkout or push plan; reject outgoing commits unrelated to the current request.
14. **Start large jobs linearly.** At 10,000+ records, scan once per inventory method. Reconcile cached outputs; while the core gap score improves, continue only reported gaps. Never repeat full discovery/scans or create per-record startup artifacts, proof files, or speculative tooling.
15. **Use the branch updater.** Run `scripts/update_branches.py`; never manually merge a hold. Fix its cause and replan.

## Standard job sequence

1. Initialize against the unopened, read-only game root; establish entry and record the user's game-workspace Git choice. Never infer it or apply it to SDK acquisition/activation.
2. Follow status through isolated discovery, SDK activation, paired inventories, and context. Whenever status exposes `data_plane_task`, prepare and dispatch that exact relay; never replace it with a direct provider command or request an ad-hoc exception. A sealed failure is a retry/repair route, not proof that the artifact should be verified or the job should stop.
3. Run primary translation, failure-only fallback, compact triage, selective strong review, flagged-only rewrite, and review fallback. Queue operational failures; blocking failures hold the patch.
4. Build and verify a recoverable workspace patch without mutating or launching the game. Report states, counts, paths, queues, and next decision.

## Load-on-demand contracts

| Trigger | Read |
| --- | --- |
| Start/resume or SDK choice | `docs/agent-usage.md`, `docs/sdk-selection.md`, `docs/sdk-branch-contract.md` |
| Construct a user-facing task prompt | `docs/prompt-examples.md` |
| Inventory, context, translation, or review | `docs/coverage-gates.md`, `docs/engine-static-scope.md`, `docs/review-pipeline.md`, `docs/fallback-routing.md`, `docs/provider-protocol.md` |
| Control-plane worker or native pass | `docs/control-plane-contract.md`, `docs/execution-budget.md`, `docs/detail-contract.md` |
| Codex App + DeepSeek preset | `docs/codex-app-deepseek.md` |
| Manual or external fallback exchange | `docs/translation-exchange.md` |
| Existing release, game update, or selective continuation | `docs/release-checkpoint.md` |
| Exact wording, dialogue/reference flag, small tone/style correction, or bounded data correction | `docs/minimal-corrections.md` |
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
- Put deterministic engine problems in code and regression tests. Use a runbook only when continuation needs external tooling, user material, runtime evidence, authorization, or a user decision.
- Before committing repository changes, run `python scripts/check_repo_privacy.py` and `python -m unittest discover -s tests -t . -v`.
- API keys belong only in environment variables and must never be printed or persisted.
