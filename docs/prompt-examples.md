# Parr0t prompt examples

These prompts are written for Codex or Codex routed through an OpenCodex-compatible proxy. Replace angle-bracket placeholders with local paths or public format identifiers. Do not paste source text into the prompt.

## Start a new localization and patch job

```text
Read Parr0t's AGENTS.md, docs/agent-usage.md, and docs/sdk-selection.md first. Treat <source-root> as read-only and create a separate workspace at <external-workspace>. Ask me once whether Git is allowed. Before writing any unpacker or patcher, determine the actual target engine/version identity, inventory every visible sdk-* branch, inspect plausible public contracts, and bind compatibility decisions to that target. Reuse or extend only the exact target branch; when another identity contains reusable code, record it as a reference and derive the target branch instead of widening the reference branch. Use private-new only when nothing is reusable. Activate the exact selected implementation before extraction; generated pass-through tools are not an adapter. Then analyze the complete ordered source inventory with the appropriate narrative, functional, knowledge, or hybrid context, translate every target with configured fallbacks, run independent review and deterministic validation, and build a new recoverable patch. Keep all content and provider explanations in the private workspace and report only metadata to the top-level orchestrator.
```

## Use an existing SDK branch

```text
Determine <source-root>'s actual target engine/version identity, then inventory Parr0t SDK branches and evaluate them against its public format. If the exact target branch is compatible, record `sdk-select --mode reuse`; if that same identity is partial, record `--mode extend` and implement only the missing capability. If a different identity has reusable implementation, record `reference`, select `--mode derive-new`, and use it read-only without changing the promotion destination. Activate the selected implementation before extraction. Do not start a duplicate adapter or merge the reference branch wholesale. Read the applicable branch instructions, keep the source immutable, create or resume <external-workspace>, and complete context analysis, translation fallback, compact review, selective rewriting, packing, and validation. Do not copy any job data into the repository.
```

## Resume an interrupted workspace

```text
Read AGENTS.md and docs/agent-usage.md, then inspect Parr0t status for <external-workspace>. Resume from the recorded state and content hashes without repeating completed extraction, context, translation, or review stages. Re-run only invalidated targets and preserve the existing Git policy. Do not open private content in the top-level orchestration context.
```

## Use custom provider routing

```text
Run Parr0t with <provider-config>. Map providers to context analyzer, primary translator, fallback translator, compact judge, selective rewriter, and fallback judge roles without hard-coding vendor behavior. Finish the primary translation queue before fallback, route only refused or failed targets, rewrite only judge-flagged targets, and preserve exact source when every permitted route is exhausted.
```

## Validate an existing patch without retranslating

```text
Do not start a new translation pass. Read Parr0t instructions, inspect the recorded workspace state at <external-workspace>, verify that <patch-path> is recoverable and does not modify <source-root>, and run the selected SDK's structure, placeholder, markup, control-code, glossary, packing, and provenance checks. Report metadata and actionable issue codes only.
```

## Promote reusable tooling after success

```text
The patch in <external-workspace> has been confirmed successful. Prepare a sanitized sdk-<engine>-<version> profile containing only reusable extraction, application, packing, validation, synthetic fixtures, and neutral documentation. Remove every job-specific name, path, string, translation, log, count, hash, and history reference. Validate privacy and tests, keep the profile local, and stop. Do not mention or propose commit, push, a repository, a tag, or a release unless I explicitly request publication in a later instruction.
```

## Publish a per-game instance release

```text
The patch at <patch-path> has been confirmed successful and I explicitly request publishing this exact instance to <instance-repository>. If I also provide <translation-source-repository> and <game-folder>, use the translation repository, read-only current game folder, and prior instance checkpoint as three separate continuation inputs before any model call. Read the instance repository's existing README first and preserve the author's structure, tone, attribution, and unrelated sections. Package for the player's execution environment rather than the current agent/build host; use Windows when I have not specified another platform, and derive architecture and prerequisites from the supported game build. Follow docs/instance-release.md: keep the branch limited to README/legal notices, the canonical checkpoint and small release metadata, and strictly necessary minimal title/engine build glue. Do not commit archives, installers, generated payloads, game data, copied SDKs, or duplicate translation exports. Build once from clean referenced commits and upload the exact verified bytes only as GitHub Release assets. Set the visible GitHub Release title to `<Game Display Name> Patch <Patch Version> (Game Version <Game Version>)`, or the exact enumerated `Game Versions` form for multiple validated builds. Make the tag, release ID, referenced commits, checkpoint, manifest, asset name, embedded versions, README compatibility table, release notes, and installer checks agree. Report the published asset hashes after one read-only post-upload verification.
```

## Inspect a public reference implementation

```text
Inspect <public-reference-url> only to learn the public patch format and determine whether the method is reusable across independent titles. Do not import project identity, content, assets, filenames, or incident history. Keep provisional work private until a real patch is confirmed, then follow Parr0t's sanitized SDK promotion and publication approval flow.
```

## Build a minimal Steam installer

```text
Read Parr0t's AGENTS.md, docs/execution-budget.md, docs/installer-contract.md, and docs/steam-packaging.md. Use the completed payload at <payload-directory> and keep every title-specific value in a private Steam spec. Build one deterministic Steam-aware ZIP with the standard player-facing Install, Restore, and Verify operations, fixed messages/codes, and `parr0t-patch-status.txt`. If and only if a payload path must replace an original file, declare its target and backup path so first install preserves the original, update reuses it, and Restore puts it back. Put SDK/title-specific failures in the custom error namespace. Do not include agent harness files, approval records, prompts, logs, or task state; do not embed an older package, design a legacy migration layer, back up or hash the complete game, or use subagents for this packaging task. Run package validation and one synthetic lifecycle, then stop and report the artifact for user validation.
```

## Explicitly authorize one real installation action

```text
I have reviewed <patch-path>. Install this exact artifact into <configured-game-root> once. Record an `install` approval bound to this artifact and target, consume it immediately before installation, and do not launch the game, remove files, delete logs, or repeat the action. Report metadata only after the installer returns.
```

## Explicitly authorize one real launch

```text
The patch is already installed. Launch <exact-executable> once using <patch-path> as the approved artifact, solely for the runtime check already described. Record and consume a separate `launch` approval. Do not install, update, remove, clean generated data, modify saves, or launch again without another instruction.
```
