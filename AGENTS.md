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
3. **Text is the default scope.** Do not inspect images for text or run OCR unless the user requests image translation. Then record `image-localization-policy`, follow `docs/image-localization.md`, and process only definitively bound player-visible UI text regions; never scan all images.
4. **Keep the control plane text-free.** Before entry, root may confirm paths but not inspect game content. Use only the exact core relay. Root may observe core CLI metadata, validated `parr0t.control-envelope.v2`/receipt paths, and direct-relay categorical lifecycle—never session transcripts/logs, agent trees, nested-worker status/output, or semantic-worker logs. Usage comes only from core ledgers/receipts. Forbidden detail requires leak recording and rotation through `docs/control-plane-contract.md`.
5. **Preserve pipeline order.** Verify inventories/context; translate with failed-only Luna retries, Sol retries, then external fallback; complete low-cost triage, strong-review, flagged rewrite, review fallback, validate, pack. Defaults: Luna 5 retries, Sol 3.
6. **Treat refusal as routing.** Parent-facing refusal state is only `fallback_required` / “폴백 처리 필요”. It is never a content judgment, exclusion, batch cancellation, or authority to shrink scope. Preserve source only after metadata proves every configured route was attempted and exhausted.
7. **Use the locked target language; translate, do not invent.** Korean is the default; another language requires an explicit user policy change. Preserve target-scoped context, meaning, voice, formality, UI function, conditions, units, order, terminology, placeholders, markup, control codes, supported allusions, and natural effect.
8. **Keep source and runtime immutable by default.** Build in the workspace. Inspection or testing never authorizes install, update, restore/remove, launch, cleanup, or execution. Each runtime action needs explicit `docs/runtime-approval.md` approval.
9. **Select and activate before implementing.** Follow `docs/sdk-selection.md`. Bind exact matches only to an inventoried Git checkout/worktree or authenticated snapshot with matching full-content fingerprint. Game-workspace Git choice is unrelated. Other identities are `derive-new`; pass-through tools are not adapters and `main` stays engine-neutral.
10. **Publication is dormant.** Do not propose, prepare, mention, commit, push, tag, publish, deploy, or create a release unless the user first requests that exact external action. SDK promotion requires confirmed patch success. Its engine-only commit must not touch `main` paths; check paths with `game-l10n verify-sdk-promotion --working-tree`, pin its base, and verify the final commit before packaging. Per-game publication requires `docs/instance-release.md` and its release gate.
11. **Keep review control deterministic.** Only `native-pass-step` advances waves; relay termination runs `--relay-ended` so core queues missing tasks. Translation/rewrite uses one batch. Review: 2,048 records/262,144 tokens; initial 256/65,536. One call/worker. Core locks successes and emits smaller failed-only manifests. V7 workers never read full source. Root sees metadata only; no model controllers or worker delegation. Unknown state stops.
12. **Keep provider mode fixed.** A selected preset defines the authorized providers and roles. A key does not change mode or promote a fallback. Native App workers and API providers are distinct; never emulate a native worker with another Codex process or silently replace the locked route.
13. **Bind every write to the current work lane.** Before writing, bind the turn to game, supervision, core, or SDK work and resolve its checkout and branch anew. Supervision and inspection stay read-only unless the current request names the mutation. Never inherit another lane's checkout or push plan; reject outgoing commits unrelated to the current request.
14. **Start large jobs linearly.** At 10,000+ records, scan once per inventory method. Reconcile cached outputs and continue only reported gaps. Never repeat full scans or create per-record startup artifacts or speculative tooling.
15. **Use the branch updater.** Run `scripts/update_branches.py`; never manually merge a hold. Fix its cause and replan.
16. **All limits are opt-in.** Only user-set manifest values apply; size/defaults create none.

## Standard job sequence

1. Initialize against the unopened, read-only game root; establish entry and record the user's game-workspace Git choice. Never infer it or apply it to SDK acquisition/activation.
2. Follow status through isolated discovery, SDK activation, paired inventories, and context. Whenever status exposes `data_plane_task`, prepare and dispatch that exact relay; never replace it with a direct provider command or request an ad-hoc exception. A sealed failure is a retry/repair route, not proof that the artifact should be verified or the job should stop.
3. Translate through failed-only Luna retries, Sol retries, then external fallback. Lock successes. Run compact triage, selective strong review, flagged rewrite, and review fallback. Queue operational failures; blocking failures hold the patch.
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
| User-requested UI-image translation | `docs/image-localization.md` |
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
