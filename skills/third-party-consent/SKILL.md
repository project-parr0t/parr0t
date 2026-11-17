---
name: third-party-consent
description: Deny unapproved third-party code, tools, dependencies, frameworks, packages, plugins, binaries, repositories, containers, and services. Use before downloading, cloning, installing, executing, vendoring, adding, updating, or transmitting data to any third party not already immutably declared and authorized by the target repository or current user request.
---

# Third-party Consent

Apply `docs/third-party-consent.md`. New third-party use is deny-by-default.

## Stop before acquisition or use

Do not download, clone, install, execute, vendor, copy, add as a dependency or submodule, call as a service, or send data to a third party until the user explicitly approves the exact component and action. Installed or reachable does not mean approved. Open source, popularity, convenience, credentials, urgency, and broad instructions such as “fix”, “test”, “build”, “deploy”, or “publish” do not imply consent.

Ordinary use of dependencies already declared and immutably locked by the target repository is within the repository contract. A new dependency, newly executed helper, replacement, update, source change, permission increase, or new data recipient is not.

## Investigate without importing

Use first-party and already-declared project tools first. Public documentation, package metadata, license text, checksums, and source may be inspected read-only to determine suitability. Do not clone, download, install, execute, or copy code during that investigation.

## Ask for an exact decision

State, in the user's language:

- exact name, publisher, canonical source, version, immutable digest/commit, and license;
- why existing first-party or project-declared tools are insufficient;
- exact action and required filesystem, process, network, credential, and data scope;
- supply-chain/execution risks and rollback/removal plan.

Ask for approval of that exact proposal. No response means no approval. Approval of one component, version, action, or data scope never authorizes another.

## Record and consume consent

After explicit approval, create a short-lived one-shot record with `game-l10n approve-third-party --risk-disclosed --user-confirmed`. Immediately before the external effect, consume it with `game-l10n consume-third-party-approval` using the same action, identity, source, version, and digest. If validation fails, do not perform the action and do not substitute another third party.

Service/API actions may use `not-applicable` only where no executable artifact exists. Downloaded or executed code requires an immutable `sha256:` or `git:` pin.

## Finish cleanly

Use only the approved scope. Remove transient copies when the approved purpose ends unless retention was separately approved. Preserve required license notices. Report the exact component and action used; never hide it behind a generic description such as “framework” or “helper”.
