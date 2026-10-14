# Parr0t agent operation guide

This guide is for coding agents. It is not a human command cookbook. The public `README.md` explains the project; this document defines how an agent should operate it.

## Supported operating baseline

Parr0t is designed around either of these control paths:

1. Codex App or Codex CLI directly.
2. Codex routed through an [OpenCodex-compatible provider proxy](https://github.com/lidge-jun/opencodex).

OpenCodex is an optional routing layer, not a Parr0t dependency. It does not replace repository instructions, workspace isolation, provider policies, or user approval boundaries. Another agent harness may be used only when it can honor `AGENTS.md`, keep private data outside the repository, call the stable `game-l10n` boundary, and preserve the same control-plane separation.

## Required locations

The agent must resolve three distinct locations before work starts:

- the Parr0t repository;
- the read-only source root;
- a writable external agent workspace.

The external workspace stores extracted content, context packs, translations, caches, reports, rebuilt data, and patches. None of those artifacts belong in the Parr0t repository.

## Startup contract

1. Read the repository `AGENTS.md`, this guide, and any `AGENTS.md` in the external workspace.
2. Follow `docs/sdk-selection.md`. First derive the target engine/version compatibility identity from the inspected runtime. Inventory visible local and remote-tracking SDK branches, inspect plausible public contracts, and bind every evaluation and selection to that target. Reuse or extend only the exact target branch. When a different identity contains reusable implementation, record it as a reference and select `derive-new`; use `private-new` only when no candidate is reusable even as a reference. After selecting an exact SDK, inspect its `docs/engine-faq.md` headings and triggers; load a complete entry only if that condition occurs.
3. Confirm that the source root is readable and the external workspace is writable.
4. Ask once at the beginning whether Git may be used for this job. Record the answer; do not infer consent.
5. Initialize or resume the external workspace and inspect `game-l10n status` before performing work.
6. Never initialize Git when the recorded choice is disabled.

## Execution lifecycle

```text
instructions and approvals
  -> format discovery and SDK selection
  -> two-method read-only extraction into the external workspace
  -> independent inventory coverage gate
  -> complete ordered-source typed context analysis
  -> primary translation for every target
  -> fallback translation for refused or failed targets only
  -> compact independent judgment
  -> selective rewrite for flagged targets only
  -> fallback judgment for refused or failed reviews only
  -> deterministic integrity validation
  -> static/review/runtime completion attestation
  -> new patch, or a private held checkpoint when a blocking target failed
```

Follow `docs/coverage-gates.md`. An extractor's own manifest cannot prove that it found every target. The primary and independent inventories must agree and declare no excluded user-visible scope before context analysis starts. Report result-manifest alignment, semantic review coverage, and runtime coverage separately; never shorten `manifest_missing_count=0` to “no in-game omissions.”

Context analysis must finish before translation. `project.context_mode` defaults to `auto` and may be set to `narrative`, `functional`, `knowledge`, or `hybrid`. Auto mode uses stable segment metadata plus speaker/scene evidence to select the smallest suitable schema. Narrative context covers story, scene, relationship, voice, and supported references. Functional context covers screens, actions, states, conditions, units, controls, and layout constraints. Knowledge context covers ordered nodes, prerequisites, terminology, and cross-entry consistency. Hybrid context combines only the domains present in the source.

The extraction layer should set `metadata.context_mode` or `metadata.context_domains` when it knows the correct domain; otherwise `text_type`, `text_function`, `category`, speaker, and scene are used as hints. Every functional or knowledge segment must be cited by an analyzed evidence entry, while narrative data must cover every named scene and speaker. A worker receives one target plus only its relevant typed entries. Segment isolation limits failure scope without reducing context to an isolated sentence.

For UI strings assembled at render time, the selected SDK should inspect static bindings or call sites and emit the evidence-backed fields in `docs/segment-metadata.md`. A label and its value remain separate targets when the runtime treats them separately. Translation and review return only the current slot payload, preserve declared state semantics, and do not repeat fixed labels, prefixes, or suffixes. This structure is part of context analysis and review, not an isolated post-processing guess.

The compact judge evaluates effect as well as denotational accuracy. It flags mechanical literalism, lost recognition cues, unsuitable tone/rhythm, changed UI function or conditions, unit errors, broken knowledge order, and material layout risk, while receiving only target-relevant evidence to keep token cost bounded.

Stable IDs and digests permit reuse but do not prove semantic quality. When the user requests a complete review of an existing translation, use `game-l10n review-existing`; it sends every current candidate, including previously unflagged entries, through a fresh independent verdict. A flagged-subset pass must be described as targeted review, never full review.

An exact user-specified typo or wording change follows `docs/minimal-corrections.md`, not the full translation lifecycle. Use the prior verified inventory and release checkpoint with `game-l10n apply-user-correction`; supply the full results when available, or let the checkpoint materialize the released candidates. It makes no model calls and preserves every unnamed target. Keep the work in the current agent. Repack the owning payload once and verify the package once. Do not reopen discovery, re-run context, delegate a correction worker, or inspect the installed game unless a concrete dependency or requested runtime action requires it.

When the user supplies an earlier Parr0t release together with the game folder, follow `docs/release-checkpoint.md` before any model call. If a title-specific translation source repository is also supplied, treat it as a separate implementation/state input and use it rather than reconstructing or copying it into Parr0t or the instance repository. Validate `parr0t-release-checkpoint.json` and re-extract the current inventories from the read-only game folder. A new game build needs a new compatibility record but does not by itself invalidate all translations; reuse exact unchanged `reviewed` targets only when their compatible-SDK, source, location, dependency, target digest, and target-scoped context/review bindings still match under the same review policy. Translate and review only new, changed, unresolved, or invalidated entries. For a requested wording correction, invalidate the selected stable IDs and their declared reverse dependencies rather than retranslating the whole game. A complete re-review remains complete only when every target receives a new verdict.

If every context-analysis route fails, stop before translation. If every permitted translation route fails for a target, preserve the exact source and mark it `source_preserved`. Never cancel unrelated targets because one target or batch was refused.

A context artifact is usable only with a matching successful per-part execution attestation. An interrupted worker or operational provider failure cannot be treated as complete merely because it wrote a partial file. Capacity, timeout, rate-limit, quota, authentication, malformed-response, and similar failures enter the metadata-only failure queue. Retry that queue only after a later explicit user request. If an unresolved target has `metadata.blocking_on_failure=true`, hold the entire patch and emit no new distributable artifact.

## Provider roles

Configure providers by role rather than by company name:

- context analyzer;
- primary translator;
- fallback translator;
- compact judge;
- selective rewriter;
- fallback judge.

The recommended `codex-app-deepseek` hybrid uses native Codex collaboration workers for primary translation and compact judgment, with DeepSeek limited to refused translations, selective rewriting, and review fallback after the corresponding primary barriers. Supplying a key never changes the selected mode or role map. Native-only, API-only, command, compatible, and local combinations remain available through another preset selected at workspace creation; they cannot be injected into the locked App preset.

Provider refusal is a routing event, not a semantic verdict. Return a categorical status to the parent and keep provider prose inside the private data plane. The top-level orchestrator must not receive source text, candidate text, translated text, or prose refusal explanations.

## Patch and publication boundaries

Always create a new patch under the external workspace. Do not overwrite or install into the source root unless the user separately requests installation after reviewing a recoverable patch. “Build,” “fix,” “test,” “validate,” “inspect logs,” and access to the directory are not installation or launch permission.

Follow `docs/runtime-approval.md` for every real external effect. Record `install`, `update`, `remove`, and `launch` separately, bind the record to the exact artifact and target, and consume it once immediately before the effect. Default to user-executed runtime validation. Never launch a game or generated plugin merely to collect execution data, and never delete runtime data or logs without a separate request.

Reusable extraction, application, packing, or validation code may be promoted only after the user confirms that the patch succeeded. Promotion must copy only generic implementation, public-format knowledge, synthetic fixtures, and neutral documentation. It must not copy job data, filenames, paths, dialogue, translations, logs, counts, hashes, or incident history.

Before promotion, classify each material engine problem under `docs/engine-faq-contract.md`. A deterministic condition is an adapter fix plus a regression test. Only a condition the SDK cannot safely resolve without external tooling, user material, runtime evidence, authorization, or an uninferable decision belongs in the engine branch's `docs/engine-faq.md`. Title-specific issues stay private and unresolved causes stay queued. Run `python scripts/check_engine_faq.py docs/engine-faq.md` on the SDK branch; a reviewed file with zero entries is valid.

After sanitization, keep the SDK profile local and stop. A successful translation does not imply publication intent. Unless the user first issues an explicit publication command, do not ask whether to commit or push, do not suggest a repository or release, and do not surface publication metadata, gates, or next steps in progress or completion reports.

A public per-game instance is another, separate publication target whose workflow remains dormant and unmentioned until the user explicitly commands publication. Once activated, follow `docs/instance-release.md`: target the player's execution environment rather than the agent/build host and use Windows by default unless another platform is explicitly specified. Derive architecture and prerequisites from the supported game build; do not leak build-machine paths or require agent/developer tooling. Preserve the user's existing README and keep the branch lean: README/legal notices, canonical checkpoint and `parr0t-instance-release.json`, plus only strictly necessary minimal title/engine build glue. Do not commit archives, installers, generated payloads, game data, copied SDKs, or duplicate translation exports; upload the exact verified distributable bytes only as GitHub Release assets. Record every validated original game version/build, build the release once from clean referenced commits, and make the checkpoint, release manifest, release notes, asset name, embedded versions, and installer compatibility checks identify the same release. Do not rebuild after verification. Format the visible GitHub Release title as `<Game Display Name> Patch <Patch Version> (Game Version <Game Version>)`; use `Game Versions` followed by the exact enumerated versions when one release supports multiple builds. A package-validation pass is not a release gate: after the lean instance tree is committed locally, run `game-l10n instance-release-gate` and require `ready` for that exact commit and exact external asset before any push, tag, `gh release create`, or upload.

## Codex operation

Codex discovers repository `AGENTS.md` instructions automatically. Keep the user prompt focused on the desired outcome and paths; do not paste the full policy into every request. Use `docs/prompt-examples.md` as the starting point and let the agent read the repository contract before acting.

The stable internal interface is:

```text
python -m pip install -e .
game-l10n init-workspace <external-workspace> --sdk-root <repository> --game-root <source-root>
game-l10n status --workspace <external-workspace>
game-l10n sdk-inventory --workspace <external-workspace> --refresh-remote
game-l10n sdk-evaluate --workspace <external-workspace> --target-engine <engine> --target-version <version-line> --branch <sdk-branch> --result <compatible|partial|reference|incompatible> --reason <reason-code> --evidence-path <public-contract-path>
game-l10n sdk-select --workspace <external-workspace> --target-engine <engine> --target-version <version-line> --mode <reuse|extend|derive-new|private-new> [--branch <sdk-branch>]
```

These commands are execution boundaries for the agent, not an end-user workflow.

## OpenCodex-routed operation

When using an OpenCodex-compatible proxy, configure and start that proxy according to its own documentation, then run Codex against the Parr0t checkout. For the commonly used OpenCodex CLI, the outline is:

```text
ocx start
ocx init
codex "<prompt from docs/prompt-examples.md>"
```

Provider credentials, routing rules, model availability, and proxy terms remain the user's responsibility. Do not place proxy configuration or keys in this repository, and do not let the proxy relax Parr0t's data or approval boundaries.

## Validation before completion

For repository changes, run:

```text
python scripts/check_repo_privacy.py
python -m unittest discover -s tests -t . -v
```

For a localization job, the agent must also validate format structure, placeholders, markup, control codes, glossary constraints, source immutability, patch recoverability, and output paths using the selected SDK branch.

It must also preserve the four separate completion states from `docs/coverage-gates.md`. Real runtime observation remains optional and separately authorized, but without it the report must say `runtime_complete=false` rather than implying full in-game coverage.

The final report to the parent contains only counts, categorical states, deterministic issue codes, artifact paths, and the next required approval. Private content remains in the external workspace.

## Execution economy

Read `docs/execution-budget.md` before a multi-stage job. Freeze the active deliverable, create the minimum working artifact, and validate only the layers that changed. Do not broaden an installer or packaging request into migration, backup, rollback, or compatibility infrastructure without a demonstrated requirement. A passed check remains valid while its input fingerprint is unchanged.

Keep simple or tightly coupled work in the current agent. Parallel workers are appropriate for independent translation or review shards, not for small scripts, status polling, repeated package inspection, or duplicate QA.

Native translation and review tasks follow the context budget in `docs/execution-budget.md`. Use `prepare-native-pass` v2, start every task in a fresh no-history worker, and end that worker after its metadata-only result. Never reuse an idle worker for another task or inherit the parent chat. The default 32-ID and 12,000 estimated-material-token bounds prevent accumulated target/tool history from being replayed on every model event without reducing per-target context quality.

## Steam delivery packages

Steam support on `main` packages an already prepared payload; it does not replace an engine SDK. Keep the payload and package spec in the private workspace, then run:

```text
game-l10n steam-package --workspace <external-workspace> --payload <payload-directory> --spec <steam-spec.json> --output <patch.zip>
game-l10n verify-steam-package --workspace <external-workspace> --package <patch.zip>
game-l10n inspect-installed-state --workspace <external-workspace> --target <configured-game-root>
```

The generated installer follows `docs/installer-contract.md`: fixed files and player messages, Install/Restore/Verify operations, a game-root `parr0t-patch-status.txt` patch-ID marker, receipt-owned paths, declared original-file replacement backups, a root release checkpoint, and isolated custom error codes. A player using the interactive menu needs no agent approval file. Read-only verification needs no runtime approval; noninteractive agent/script install, update, and restore do. Never copy the workspace `AGENTS.md`, approval JSON, prompts, logs, or task state into the ZIP. Follow `docs/steam-packaging.md` for packaging, `docs/release-checkpoint.md` for continuation/reuse, and `docs/runtime-approval.md` for the automation external-effect gate.

Before deciding that a patched game root is a newly updated build, use `inspect-installed-state`. An intact receipt avoids redundant whole-game extraction or packaging. A receipt-owned mismatch is only an advisory `possible_game_update`, while a changed or missing preserved-original backup is `damaged_state` and must stop mutation.

For Steam discovery and launch integration, also read `docs/steam-faq.md` and `docs/wrapper-executable.md`. Resolve the exact App ID through existing library/app manifests and binding files, and ask when the result is not unique. Never edit Steam client configuration or require the client to close. Use an overlay or exact approved Parr0t wrapper bytes for the target architecture; verify the wrapper release signature and hash, preserve each declared original, and never generate an opaque per-game executable.
