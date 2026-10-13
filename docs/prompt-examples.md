# Parr0t prompt examples

These prompts are written for Codex or Codex routed through an OpenCodex-compatible proxy. Replace angle-bracket placeholders with local paths or public format identifiers. Do not paste source text into the prompt.

## Start a new localization and patch job

```text
Read Parr0t's AGENTS.md and docs/agent-usage.md first. Treat <source-root> as read-only and create a separate workspace at <external-workspace>. Ask me once whether Git is allowed, then discover the format, analyze the complete ordered script and character voices, translate every target with configured fallbacks, run independent review and deterministic validation, and build a new recoverable patch. Keep all content and provider explanations in the private workspace and report only metadata to the top-level orchestrator.
```

## Use an existing SDK branch

```text
Use the Parr0t branch sdk-<engine>-<version> only after verifying that its public format contract matches <source-root>. Read the branch instructions, keep the source immutable, create or resume <external-workspace>, and complete context analysis, translation fallback, compact review, selective rewriting, packing, and validation. Do not copy any job data into the repository.
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
The patch in <external-workspace> has been confirmed successful. Prepare a sanitized sdk-<engine>-<version> profile containing only reusable extraction, application, packing, validation, synthetic fixtures, and neutral documentation. Remove every job-specific name, path, string, translation, log, count, hash, and history reference. Validate privacy and tests, then ask me separately before any commit or push.
```

## Inspect a public reference implementation

```text
Inspect <public-reference-url> only to learn the public patch format and determine whether the method is reusable across independent titles. Do not import project identity, content, assets, filenames, or incident history. Keep provisional work private until a real patch is confirmed, then follow Parr0t's sanitized SDK promotion and publication approval flow.
```
