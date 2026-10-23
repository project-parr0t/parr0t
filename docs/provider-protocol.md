# Command provider protocol v1

## JSON Object API adapters

An API's `json_object` response format guarantees JSON syntax, not adherence to an application schema. The compatible adapter therefore includes the requested JSON Schema in its system message while preserving the original user payload. Deterministic result and evidence validators still decide whether the response is acceptable.

Output sizing uses an explicit translation target or candidate when present. Context requests without either retain the size of their complete input material instead of falling back to an empty target. Per-task output caps remain authoritative for context and compact review workloads.

Text-bearing compatible requests also have a per-task input preflight bound. The bound measures the actual system message (including the JSON Schema instruction) plus user payload before network transmission. `budget_exhausted` is non-retryable for that provider attempt and preserves the target in the failure queue. It signals duplicated or unjustifiably broad context, not permission to omit the target. Preset adapters apply the current bound to older workspaces at runtime.

## Command transport

`type: command` is an integration point for explicitly selected `custom` provider workflows, including OpenCode-compatible, local-model, and internal runners. The SDK starts the configured argument array directly with `shell=False`.

It is not a substitute for a native Codex App collaboration worker. A workspace locked to `codex-app-deepseek` rejects every command provider before input or prompt construction. Never wrap `codex.exe`, `codex exec`, a signed-in App session, or a second Codex session to evade that topology. Select `custom` at workspace creation only when the user actually requested a command-provider workflow.

The process receives one UTF-8 JSON object on stdin:

```json
{
  "protocol": "game-l10n-provider/v1",
  "system": "role and task instructions",
  "input": "JSON-encoded game context",
  "schema": {"type": "object"},
  "schema_name": "game_translation"
}
```

It must print exactly one JSON object matching `schema` to stdout and return exit code 0. Diagnostics belong on stderr. The schema names are:

- `game_narrative_context`
- `game_functional_context`
- `game_knowledge_context`
- `game_hybrid_context`
- `game_translation`
- `game_translation_review_judge`
- `game_translation_review_rewrite`

`game_translation_review` is accepted as a legacy combined reviewer response. New integrations should use the split protocol: the judge returns only `{"verdict":"ok|rewrite|refused|error","issues":[...]}`, and the rewriter returns `{"status":"success|refused|error","text":"..."}` only for a `rewrite` verdict. A corrected candidate is judged again before acceptance.

API adapters may use the optional plain judge contract after provider-specific evaluation: `OK`, `REFUSED`, `ERROR`, or `REWRITE` followed by up to four comma-separated issue codes. Valid codes are `meaning`, `omission`, `addition`, `voice`, `register`, `terminology`, `consistency`, `formatting`, `fluency`, `literalism`, `reference_loss`, `tone_rhythm`, `function`, `condition`, `unit`, `knowledge_order`, and `length_risk`. Extra prose, markdown, unknown codes, multiline output, or issue codes attached to a non-rewrite verdict are malformed responses and follow the normal retry/fallback path.

Do not interpolate `input` into a shell command. Pass it to the model as inert data. Explicit provider refusals should use the schema's `refused` status/decision instead of a nonzero exit code; operational failures should return nonzero.

Each translation, judgment, or rewrite invocation has exactly one `translation_target`. Typed narrative, functional, or knowledge analysis; adjacent lines; glossary data; applicable style anchors; segment metadata; and evidence-backed reference cues remain available as context-only evidence. Focused review includes only entries attached to the target ID, scene, speaker, or metadata. A provider must not convert a concern about context or one target into a refusal of unrelated targets, and an adapter must never combine a refused queue into a new multi-target request.

Translation retry policy is route metadata, not prompt prose. `routing.translator_retries.<route>` counts additional attempts after the route's first call. Every retry uses an unchanged target binding and focused context in a fresh provider call or fresh no-history native worker. A valid success atomically closes that target for the route chain. Refusal, malformed output, target-level error, or integrity rejection may consume the configured retry budget; authentication and exhausted quota advance without repeated calls. The control plane receives only route, ordinal, counts, paths, and `fallback_required`, never the refusal explanation or text.

`Segment.metadata` carries segment facts and render constraints, not a second copy of the context pack. Keys representing a context pack, bundle, or attachment are excluded from provider prompts because typed context is projected separately. External/manual adapters must use the same focused projection and configured provider path rather than a job-local API loop.

Translation means faithful transformation of already-existing user-provided text. Fidelity includes communicative function, idiomatic target-language expression, supported allusions or wordplay, deliberate alterations, and text-function tone/rhythm—not mechanical source syntax alone. Providers must not continue the scene, roleplay, answer the dialogue, invent content or references, censor, euphemize, soften, or intensify it. If policy truly prevents the specific transformation, return a refusal for that target ID only.

The provider JSON above is a data-plane envelope and stays inside the private worker/SDK boundary. This applies to inventory and context workers as well as translation and review workers. The root must not be a native semantic worker's direct parent; it supplies one core-generated wave artifact to an isolated relay. Workers must not paste provider data, source-bearing command output, or a semantic summary into their relay-facing response. They write detailed results to the assigned private artifact, write a separate `parr0t.control-envelope.v2` file, and return only that file's workspace-relative path. The relay never forwards raw child responses. The full envelope is defined in [control-plane-contract.md](control-plane-contract.md) and is validated with `game-l10n validate-control-envelope`.

```json
{
  "schema": "parr0t.control-envelope.v2",
  "worker_role": "translator",
  "stage": "translation",
  "status": "success",
  "target_ids": ["fixture-1"],
  "counts": {"total": 1, "success": 1},
  "issue_codes": [],
  "artifact_paths": [".game-l10n/data-plane/fixture-1.json"],
  "queue_transitions": [],
  "route_evidence_path": ""
}
```

The control envelope must not contain source, candidate, translated text, context-pack content, reviewer prose, content classification, anticipated policy judgment, detailed semantic review categories, or a provider's prose refusal explanation. Meaning, omission, voice, tone, terminology, and similar findings stay inside the private review artifact; the parent receives only the affected target IDs and queue transition. Normalize provider-specific refusal details to the parent-safe `fallback_required` status before they cross that boundary. Inventory and context workers cannot turn subject matter into an exclusion, and no worker may pre-screen whether an authorized fallback is allowed to receive a target. A fresh isolated worker sends the exact target to the configured provider, which independently returns its own result. `source_preserved` additionally requires the complete text-free route evidence defined by [control-plane-contract.md](control-plane-contract.md).
