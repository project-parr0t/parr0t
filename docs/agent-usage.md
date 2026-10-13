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
2. Follow `docs/sdk-selection.md`. Inventory visible local and remote-tracking SDK branches, inspect plausible public contracts, record categorical evaluations, and select reuse, extension, or a private new adapter before writing format tooling. Reuse is the default and a partial match must be extended rather than duplicated.
3. Confirm that the source root is readable and the external workspace is writable.
4. Ask once at the beginning whether Git may be used for this job. Record the answer; do not infer consent.
5. Initialize or resume the external workspace and inspect `game-l10n status` before performing work.
6. Never initialize Git when the recorded choice is disabled.

## Execution lifecycle

```text
instructions and approvals
  -> format discovery and SDK selection
  -> read-only extraction into the external workspace
  -> complete ordered-script context analysis
  -> primary translation for every target
  -> fallback translation for refused or failed targets only
  -> compact independent judgment
  -> selective rewrite for flagged targets only
  -> fallback judgment for refused or failed reviews only
  -> deterministic integrity validation
  -> new patch and metadata-only completion report
```

Context analysis must finish before translation. It produces scene state, relationship changes, character voice rules, forms of address, glossary constraints, evidence identifiers, and supported allusion/wordplay/rhetorical cues from the complete ordered script and segment metadata. It must not invent a reference from weak resemblance. A worker receives one target segment at a time plus that read-only context. Segment isolation limits failure scope; it does not remove narrative, character, or text-function context.

The compact judge evaluates effect as well as denotational accuracy. It flags mechanical literalism, lost recognition cues, and unsuitable tone/rhythm for titles, quest names, slogans, dialogue, and UI text, while sending only target-relevant cues to keep input and output token cost bounded.

If every context-analysis route fails, stop before translation. If every permitted translation route fails for a target, preserve the exact source and mark it `source_preserved`. Never cancel unrelated targets because one target or batch was refused.

## Provider roles

Configure providers by role rather than by company name:

- context analyzer;
- primary translator;
- fallback translator;
- compact judge;
- selective rewriter;
- fallback judge.

The recommended hybrid uses native Codex workers for primary translation and compact judgment, with an independent API for refused translations, selective rewriting, and review fallback. It is only a preset. Native-only, API-only, command, compatible, and local providers may fill the same roles.

Provider refusal is a routing event, not a semantic verdict. Return a categorical status to the parent and keep provider prose inside the private data plane. The top-level orchestrator must not receive source text, candidate text, translated text, or prose refusal explanations.

## Patch and publication boundaries

Always create a new patch under the external workspace. Do not overwrite or install into the source root unless the user separately requests installation after reviewing a recoverable patch. “Build,” “fix,” “test,” “validate,” “inspect logs,” and access to the directory are not installation or launch permission.

Follow `docs/runtime-approval.md` for every real external effect. Record `install`, `update`, `remove`, and `launch` separately, bind the record to the exact artifact and target, and consume it once immediately before the effect. Default to user-executed runtime validation. Never launch a game or generated plugin merely to collect execution data, and never delete runtime data or logs without a separate request.

Reusable extraction, application, packing, or validation code may be promoted only after the user confirms that the patch succeeded. Promotion must copy only generic implementation, public-format knowledge, synthetic fixtures, and neutral documentation. It must not copy job data, filenames, paths, dialogue, translations, logs, counts, hashes, or incident history.

After sanitization, keep the SDK profile local and stop. A successful translation does not imply publication intent. Unless the user first issues an explicit publication command, do not ask whether to commit or push, do not suggest a repository or release, and do not surface publication metadata, gates, or next steps in progress or completion reports.

A public per-game instance is another, separate publication target whose workflow remains dormant and unmentioned until the user explicitly commands publication. Once activated, follow `docs/instance-release.md`: target the player's execution environment rather than the agent/build host and use Windows by default unless another platform is explicitly specified. Derive architecture and prerequisites from the supported game build; do not leak build-machine paths or require agent/developer tooling. Preserve the user's existing README, record every validated original game version/build, build the release once from a clean tagged commit, and upload the exact bytes that passed verification. The repository output, release manifest, release notes, asset name, embedded versions, and installer compatibility checks must identify the same release. Do not rebuild after verification or add release-only first-party files. Format the visible GitHub Release title as `<Game Display Name> Patch <Patch Version> (Game Version <Game Version>)`; use `Game Versions` followed by the exact enumerated versions when one release supports multiple builds.

## Codex operation

Codex discovers repository `AGENTS.md` instructions automatically. Keep the user prompt focused on the desired outcome and paths; do not paste the full policy into every request. Use `docs/prompt-examples.md` as the starting point and let the agent read the repository contract before acting.

The stable internal interface is:

```text
python -m pip install -e .
game-l10n init-workspace <external-workspace> --sdk-root <repository> --game-root <source-root>
game-l10n status --workspace <external-workspace>
game-l10n sdk-inventory --workspace <external-workspace> --refresh-remote
game-l10n sdk-evaluate --workspace <external-workspace> --branch <sdk-branch> --result <compatible|partial|incompatible> --reason <reason-code> --evidence-path <public-contract-path>
game-l10n sdk-select --workspace <external-workspace> --mode <reuse|extend|private-new> [--branch <sdk-branch>]
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

The final report to the parent contains only counts, categorical states, deterministic issue codes, artifact paths, and the next required approval. Private content remains in the external workspace.

## Execution economy

Read `docs/execution-budget.md` before a multi-stage job. Freeze the active deliverable, create the minimum working artifact, and validate only the layers that changed. Do not broaden an installer or packaging request into migration, backup, rollback, or compatibility infrastructure without a demonstrated requirement. A passed check remains valid while its input fingerprint is unchanged.

Keep simple or tightly coupled work in the current agent. Parallel workers are appropriate for independent translation or review shards, not for small scripts, status polling, repeated package inspection, or duplicate QA.

## Steam delivery packages

Steam support on `main` packages an already prepared payload; it does not replace an engine SDK. Keep the payload and package spec in the private workspace, then run:

```text
game-l10n steam-package --workspace <external-workspace> --payload <payload-directory> --spec <steam-spec.json> --output <patch.zip>
game-l10n verify-steam-package --workspace <external-workspace> --package <patch.zip>
```

The generated installer follows `docs/installer-contract.md`: fixed files and player messages, Install/Restore/Verify operations, a game-root `parr0t-patch-status.txt` patch-ID marker, receipt-owned paths, declared original-file replacement backups, and isolated custom error codes. A player using the interactive menu needs no agent approval file. Read-only verification needs no runtime approval; noninteractive agent/script install, update, and restore do. Never copy the workspace `AGENTS.md`, approval JSON, prompts, logs, or task state into the ZIP. Follow `docs/steam-packaging.md` for packaging and `docs/runtime-approval.md` for the automation external-effect gate.

For Steam discovery and launch integration, also read `docs/steam-faq.md` and `docs/wrapper-executable.md`. Resolve the exact App ID through existing library/app manifests and binding files, and ask when the result is not unique. Never edit Steam client configuration or require the client to close. Use an overlay or exact approved Parr0t wrapper bytes for the target architecture; verify the wrapper release signature and hash, preserve each declared original, and never generate an opaque per-game executable.
