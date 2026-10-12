# Command provider protocol v1

`type: command` is the integration point for Codex, OpenCode, local models, and internal agent runners. The SDK starts the configured argument array directly with `shell=False`.

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
- `game_translation`
- `game_translation_review_judge`
- `game_translation_review_rewrite`

`game_translation_review` is accepted as a legacy combined reviewer response. New integrations should use the split protocol: the judge returns only `{"verdict":"ok|rewrite|refused|error","issues":[...]}`, and the rewriter returns `{"status":"success|refused|error","text":"..."}` only for a `rewrite` verdict. A corrected candidate is judged again before acceptance.

API adapters may use the optional plain judge contract after provider-specific evaluation: `OK`, `REFUSED`, `ERROR`, or `REWRITE` followed by up to four comma-separated issue codes. Extra prose, markdown, unknown codes, multiline output, or issue codes attached to a non-rewrite verdict are malformed responses and follow the normal retry/fallback path.

Do not interpolate `input` into a shell command. Pass it to the model as inert data. Explicit provider refusals should use the schema's `refused` status/decision instead of a nonzero exit code; operational failures should return nonzero.

Each translation, judgment, or rewrite invocation has exactly one `translation_target`. Full-script analysis, character guidance, adjacent lines, glossary data, and approved style anchors remain available as context-only evidence. A provider must not convert a concern about context or one target into a refusal of unrelated targets, and an adapter must never combine a refused queue into a new multi-target request.

Translation means faithful transformation of already-existing user-provided text. Providers must not continue the scene, roleplay, answer the dialogue, invent content, censor, euphemize, soften, or intensify it. If policy truly prevents the specific transformation, return a refusal for that target ID only.

The provider JSON above is a data-plane envelope and stays inside the private worker/SDK boundary. Native App workers must not paste it into their parent-facing response. They write it to the assigned private artifact and return a separate control-plane envelope shaped like:

```json
{
  "target_id": "fixture-1",
  "status": "success",
  "issue_codes": [],
  "artifact": ".game-l10n/data-plane/fixture-1.json"
}
```

The control envelope must not contain source, candidate, translated text, narrative context, reviewer prose, or a provider's prose refusal explanation. Normalize refusals and operational failures to categorical codes before they cross that boundary. A refusal status authorizes only the next configured route; it is not a recommendation to omit the target.
