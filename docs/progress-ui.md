# Processing progress UI

Long-running semantic commands automatically start a temporary loopback web UI. The operating system assigns an available random port by binding to port `0`; Parr0t does not reserve or reuse a fixed port. The server starts before semantic processing and closes in the command's `finally` path after success, failure, or interruption.

The startup message is written separately from the command's final machine-readable result and includes a tokenized local URL. The server binds only to `127.0.0.1`, rejects status requests without the per-run token, disables caching, and does not open a browser or modify the game.

The UI contract is `parr0t.processing-progress/v1`. It permits only:

- categorical operation, stage, and running/completed/failed status;
- total, completed, pending, translated, reviewed, fallback-pending, failed, source-preserved, cached, and attempt counts;
- completion percentage and elapsed time.

Source text, translated text, dialogue IDs, speaker or scene names, prompts, provider responses, refusal explanations, file contents, and game paths are forbidden. Progress reporting is observational: a UI failure cannot stop, retry, reroute, or alter localization work.
