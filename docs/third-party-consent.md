# Third-party consent contract

Parr0t denies new third-party code and services by default. Availability, open-source status, popularity, installed state, credentials, convenience, a broad request such as “fix” or “deploy”, and approval for a different component are not authorization.

## What requires exact consent

Before any of the following actions, obtain explicit user approval for the exact component and action:

- download or clone code, binaries, scripts, containers, images, plugins, or repositories;
- install, execute, vendor, copy, or modify third-party code;
- add or update a dependency, framework, package, submodule, build tool, or executable;
- call a newly introduced external service or API, or transmit user/project data to it.

Reading public documentation, package metadata, licenses, checksums, and source in a read-only browser is discovery, not approval. Do not download or execute the component during discovery.

An exact dependency version already declared and immutably locked by Parr0t becomes preapproved after complete validation passes without skips against those exact bytes, provided an evidence-backed review finds no known supply-chain attack history affecting that version, artifact, publisher, or source. The review must check authoritative project/package security notices and recognized advisory sources current at validation time. This exception covers the validated dependency closure and ordinary reuse only within the validated scope; it does not require a one-shot approval record.

The preapproval is content-bound, not name-bound. A version, source, publisher, digest, dependency closure, permissions, execution purpose, or data-scope change requires a new validation or explicit user consent. Incomplete, stale, skipped, or failed validation grants nothing. A later compromise report, yanked artifact for a security cause, provenance anomaly, or other credible supply-chain evidence immediately revokes inherited approval until the exact dependency is replaced and validated again or the user explicitly decides otherwise.

Host-provided tools already selected by the user and services explicitly selected in the current request remain authorized only within their existing scope. Adding, replacing, updating, newly executing, or broadening them requires a new decision.

## Required disclosure

Before asking for consent, prefer an existing first-party or project-declared path. If no adequate path exists, disclose:

1. exact component name and publisher;
2. canonical HTTPS source, exact version, immutable SHA-256 or Git commit, and license;
3. the exact proposed action and why it is necessary;
4. filesystem, process, network, credential, and data-transmission scope;
5. supply-chain and execution risks;
6. rollback and removal steps.

Ask for one scoped decision. Silence, an unanswered prompt, urgency, tool access, credentials, or an earlier broad instruction never grants consent. A refusal applies only to that proposed component; continue all unaffected work and do not substitute another third party without a separate approval.

## Core approval record

After the user explicitly approves and before the external action, record the exact decision:

```text
game-l10n approve-third-party \
  --workspace <workspace> \
  --action <download|clone|install|execute|vendor|add-dependency|add-submodule|use-service|send-data> \
  --name <exact-name> \
  --publisher <publisher> \
  --source <canonical-https-url> \
  --version <exact-version> \
  --digest <sha256:...|git:...|not-applicable-for-service-only> \
  --license <license> \
  --purpose <bounded-purpose> \
  --data-scope <data-sent-or-none> \
  --risk-disclosed \
  --user-confirmed
```

Immediately before the approved action, consume the one-shot record with `game-l10n consume-third-party-approval` and the same identity fields. A missing, expired, consumed, mutable, mismatched, or non-user-authorized record blocks the action. Service-only actions may use `not-applicable` for the digest; executable code requires an immutable SHA-256 or Git commit.

The approval record authorizes only the named action. It does not authorize installation, execution, publication, runtime mutation, credentials, additional components, later updates, or further data transmission unless those effects are separately named and approved.

## After use

Keep only artifacts the user approved or the repository contract requires. Remove transient downloads after their approved purpose, preserve license/attribution obligations, and report the exact component and action used. Never convert a temporary approval into a persistent dependency or submodule without a separate explicit decision.
