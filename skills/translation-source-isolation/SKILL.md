---
name: translation-source-isolation
description: Enforce Parr0t's default-deny content-network and direct-model provenance contract for translation, rewrite, localization context, triage, strong review, dialogue review, and terminology review workers.
---

# Translation source isolation

Apply this skill to every Parr0t semantic localization task. Candidate wording and semantic judgments come only from the exact selected model using the immutable bound local task and context.

## Default deny

Without an exact `parr0t.content-source-grant.v1` bound into the task's `parr0t.content-source-policy.v1`, do not use Google Translate, browser translation, web search, translation APIs, external machine-translation libraries or endpoints, remote dictionaries, connectors, or any other network content source. Provider transport may invoke only the selected model; it is not a content source.

Installed tools, credentials, configuration examples, provider availability, previous sessions, and a broad request to translate or validate never grant access. Inventory-only local inspection may use the bound local files and tools but must not produce candidate wording.

## Exact authorization

External content is allowed only when the current task names the immutable grant path and matching scope ID. The grant must record either explicit user confirmation or an exact immutable validation-session contract, and must match the semantic task kind, route scope, and every external source. Do not reuse a grant implicitly or widen it on retry, resume, fallback, split, or relay replacement.

## Provenance and taint

Every model-authored result carries `parr0t.content-provenance.v1` with `mode: direct_model`, the exact model/route, the bound policy digest, and the exact external sources actually used. An undeclared source, mismatched model or policy, missing scope, or non-direct provenance taints the result. Reject it with the core provenance error; never silently accept, rewrite, or relabel it.

## Harness enforcement

Use the host's actual tool allowlist when present. Semantic workers receive no browser/web-search tools and no arbitrary process bridge under the guarded OMP transport. Only exact local core protocol commands remain available; an exact authorized external source may be read only through its bound target. Prompt-only isolation must be reported as prompt-only and never described as host enforcement.
