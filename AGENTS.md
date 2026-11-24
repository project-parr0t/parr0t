# AGENTS.md — Parr0t repository

Parr0t is engine-neutral. Keep game/credential/title/job data in external workspaces.

## Read order

1. Read this file before accessing game-root contents; path existence may be checked only for workspace initialization.
2. Read `docs/agent-usage.md`, initialize or resume the workspace without content inspection, then read its `AGENTS.md`.
3. Run `game-l10n status --workspace <workspace>`. Establish text-free entry before semantic work; metadata-only configuration/repair is admitted independently. Follow the primary action and any ready `parallel_work`.
4. Resolve `required_contracts` against `contract_base`; reuse unchanged contract digests and checkpoints instead of repeatedly rereading whole documents.

`local_patch_ready` ends localization at the declared assurance level. Root may pause/analyze/shut down or resume on explicit user request without a second UI dialog. Semantic/history exposure blocks ordinary work until explicitly authorized fresh recovery or incident-scoped same-session acceptance under the control-plane contract.

## Non-negotiable rules

1. **Stay authorized.** Apply `skills/request-scope-guard/SKILL.md` after scope changes/external effects and `skills/third-party-consent/SKILL.md` before third-party use. Preserve exact user consent, risk disclosure and scoped records; reuse only explicitly authorized unchanged workflows. Fully validated immutable dependencies retain their scoped exception.
2. **Isolate translation sources.** Apply `skills/translation-source-isolation/SKILL.md`; preserve task/model/source provenance, reuse already-approved exact sources within scope, and version explicit grant additions without rewriting prior task authority.
3. **Separate locations.** This repository holds reusable code; the game root is read-only; generated work belongs in a `game-l10n init-workspace` workspace.
4. **Keep image scope explicit.** Image translation is optional; requested font/atlas or bounded visual inspection does not imply translation permission. Record inspection or translation policy, honor requested player-visible UI/non-UI scope, and account for unexamined assets honestly. Do not run an unbounded whole-tree OCR.
5. **During localization, keep root metadata-only.** Under `docs/control-plane-contract.md`, observe only core status, validated envelopes/receipts, relay lifecycle, and core-owned usage; never private data, semantic text, transcripts, logs, trees, or nested-worker output/status. Record exposure and stop ordinary work at the recovery gate. Contract-authorized acceptance grants no semantic or worker permission.
6. **Continue until delivery or a real stop.** Advance every authorized ready action and build or repair missing capability within scope before treating it as a blocker. Stop only at verified `local_patch_ready`, an explicit user pause/stop, or a concrete error or authorization gate with no remaining authorized recovery or independent work. An inconclusive check, a completed intermediate step, or a failed sibling is not a stop. Preserve checkpoints and report the exact unmet prerequisite when blocked.
7. **Treat refusal as routing.** Parent-facing refusal state is only `fallback_required` / “폴백 처리 필요”. It is never a content judgment, exclusion, batch cancellation, or authority to shrink scope. Preserve source only after metadata proves every configured route was attempted and exhausted.
8. **Use the locked target language; translate, do not invent.** Korean is the default; another language requires an explicit user policy change. Preserve target-scoped context, meaning, voice, formality, UI function, conditions, units, order, terminology, placeholders, markup, control codes, supported allusions, and natural effect.
9. **Keep source and runtime immutable by default.** Build in the workspace. Inspection or testing never authorizes install, update, restore/remove, launch, cleanup, or execution. Each runtime action needs explicit `docs/runtime-approval.md` approval.
10. **Select and activate before implementing.** Follow `docs/sdk-selection.md`. Bind exact matches only to an inventoried Git checkout/worktree or authenticated snapshot with matching full-content fingerprint. Game-workspace Git choice is unrelated. Other identities are `derive-new`; pass-through tools are not adapters and `main` stays engine-neutral.
11. **Publication is dormant.** Do not propose, prepare, mention, commit, push, tag, publish, deploy, or create a release unless the user first requests that exact external action. SDK promotion requires confirmed patch success. Its engine-only commit must not touch `main` paths; check paths with `game-l10n verify-sdk-promotion --working-tree`, pin its base, and verify the final commit before packaging. Per-game publication requires `docs/instance-release.md` and its release gate.
12. **Keep review control deterministic.** Use exact host model bindings and `native-pass-step` lifecycle artifacts. OMP's loaded helper enforces child message/history denial. Honor bound task/split policies and actual host capacity; preserve validated task shards while siblings remain pending. Never fabricate worker starts, completion or route exhaustion.
13. **Honor explicit models.** Presets are defaults, not a model/topology allowlist. Resolve bare OMP IDs only from one exact authoritative host match; never guess a provider prefix. Host operator approval remains independent. Preserve explicit role/model/attempt choices, started launches and successes; observed host denials are operational repair/fallback evidence, not semantic refusals.
14. **Bind every write to the current work lane.** Before writing, bind the turn to game, supervision, core, or SDK work and resolve its checkout and branch anew. Supervision and inspection stay read-only unless the current request names the mutation. Never inherit another lane's checkout or push plan; reject outgoing commits unrelated to the current request.
15. **Start large jobs linearly.** At 10,000+ records: scan once/method, cache evidence, repair gaps. Display scope needs render/dependency evidence. Reconcile comparison keys before canonical expansion; no per-record startup artifacts.
16. **Use the branch updater.** Run `scripts/update_branches.py`; never manually merge a hold. Fix its cause and replan.
17. **Never solicit limits.** No whole-job usage ceiling applies unless the user selected it and the manifest records it. Time/token/cost dimensions are independently optional. Capacity and provider errors must not be mislabeled as a user budget.
18. **Limit framework-version compatibility to checkpoints.** Resume from validated checkpoint/repository data using the current Core and the current selected SDK implementation. Never retain, restore, alias, execute, or add a compatibility branch for an obsolete Parr0t runtime, path, launcher, or tool transport. Historical execution bindings are immutable evidence, not executable plans: verify actual worker termination, close the old attempt honestly, preserve completed artifacts/identities, and regenerate fresh manifests and argv with the current code. Engine/version support and real-game runtime approval remain separate contracts.
19. **Do not add speculative Core changes.** Make only the smallest Core change needed for an explicitly requested behavior or a demonstrated Core defect that blocks authorized work. Prefer the existing workflow, selected SDK, or private workspace when it can solve the problem. Do not preemptively add features, settings, gates, fallbacks, or abstractions for unobserved needs. Verify the concrete behavior changed, then return to the active work.
20. **Require explicit permission for a new top-level session after bootstrap.** Outside the initial bootstrap authorized by a request to start the job, only a current explicit user request to start a new top-level session authorizes invoking a controller launcher. A request to continue, a recovery or rotation gate, a failure, an existing registration, and a previously approved launch are not authorization. Apply this rule to helper tools and direct Core CLI calls alike. If a fresh session is required but not authorized, preserve checkpoints and report the gate. Incident-specific consent to resume the current session never authorizes a new session.

## Standard job sequence

1. Initialize against the unopened, read-only game root and establish entry. Git defaults off; an undecided Git preference must not delay local patch work.
2. Follow status and independent ready work through bound relays. Record actual admitted-child lifecycles; denied/unstarted slots have honest terminal states. Terminate actual children before replacement; failure sealing does not require a successful finalizer.
3. Core advances unfinished targets through approved configured routes, locks successes, and partitions mixed next-review routes. Operational failure must not repeat the unchanged failing route indefinitely or be counted as semantic exhaustion. Respect explicit user pause/stop.
4. Validate and package the recoverable artifact at its declared independent/primary-only/incomplete assurance level. Source/result/language/integrity checks remain mandatory; real game mutation or launch still requires exact authorization.

## Load-on-demand contracts

| Trigger | Read |
| --- | --- |
| Start/resume or SDK choice | `docs/agent-usage.md`, `docs/sdk-selection.md`, `docs/sdk-branch-contract.md` |
| Construct a user-facing task prompt | `docs/prompt-examples.md` |
| Inventory, context, translation, or review | `docs/coverage-gates.md`, `docs/engine-static-scope.md`, `docs/localization-authority.md`, `docs/dialogue-localization.md`, `docs/review-pipeline.md`, `docs/fallback-routing.md`, `docs/provider-protocol.md` |
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
| Draft, revise, or review patch notes | `skills/patch-notes/SKILL.md` (not commit messages or `README.md`) |
| Publication/Workshop metadata | `docs/instance-release.md`, `docs/workshop-metadata.md` |
| SDK branch maintenance/update | `docs/branch-update-contract.md` |
| Confirmed SDK promotion/publication | `docs/sdk-branch-contract.md`, `docs/engine-faq-contract.md` |
| Framework troubleshooting | `docs/faq.md` |
| Framework analysis mode or analysis management | `docs/control-plane-contract.md`, `skills/framework-analysis/SKILL.md` |
| Triggered engine exception | selected SDK branch `docs/engine-faq.md` under `docs/engine-faq-contract.md` |

## Repository acceptance

- `main` contains only engine-neutral framework behavior. Engine-specific discovery, unpack/apply/pack logic, fixtures, and `docs/engine-faq.md` belong to `sdk-<engine>-<version>`.
- SDK content must be provenance-free. Use public format names and unmistakably synthetic fixtures only. Title-specific incidents and unresolved private evidence stay outside this repository.
- Put deterministic engine problems in code and regression tests. Use a runbook only when continuation needs external tooling, user material, runtime evidence, authorization, or a user decision.
- Before committing repository changes, run `python scripts/check_repo_privacy.py` and `python -m unittest discover -s tests -t . -v`.
- API keys belong only in environment variables and must never be printed or persisted.
