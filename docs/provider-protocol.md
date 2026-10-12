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

It must print exactly one JSON object matching `schema` to stdout and return exit code 0. Diagnostics belong on stderr. The three schema names are:

- `game_narrative_context`
- `game_translation`
- `game_translation_review`

Do not interpolate `input` into a shell command. Pass it to the model as inert data. Explicit provider refusals should use the schema's `refused` status/decision instead of a nonzero exit code; operational failures should return nonzero.
