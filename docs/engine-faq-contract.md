# Engine exception runbook contract

An engine SDK FAQ is not a list of common questions, bug history, or implementation notes. It is a compact runbook for a later agent that encounters an engine-specific condition the SDK cannot resolve safely and deterministically by itself.

`main` owns this contract only. Every `sdk-<engine>-<version>` branch owns its own `docs/engine-faq.md`. Engine entries never belong in the framework-level `docs/faq.md` and never flow back into `main`.

## Classification before documentation

Classify every material engine problem observed while building or validating an adapter:

- `automated_fix`: the SDK can detect and resolve or reject the condition deterministically. Fix the adapter and add a regression test; do not add an engine FAQ entry.
- `engine_runbook`: safe continuation requires external tooling, user-supplied material, an engine/version choice, runtime evidence, manual judgment, or an authorization the SDK cannot infer. Add or update a neutral engine runbook entry.
- `private_only`: the condition is title-specific or cannot be useful without private provenance. Keep it only in the external workspace.
- `unresolved`: the cause or safe procedure is not established. Keep it in the failure queue; do not publish speculation as guidance.

An empty engine runbook is valid. Quality is measured by correct classification, not entry count.

## Admission test

An `engine_runbook` entry is eligible only when all of the following are true:

1. Its trigger can be recognized without private game text or job history.
2. The SDK cannot safely choose or perform the resolution from public engine evidence alone.
3. A future agent can name the exact external input, tool, evidence, or user decision required.
4. The procedure has a safe stop point and a deterministic resume verification.
5. The guidance can be written without a real title, source filename, asset path, character, dialogue, translation, provider identity, run count, hash, or incident narrative.

If a later SDK revision can automate the condition, move the behavior into code and regression tests, then remove or reduce the runbook entry.

## Branch file and schema

Each engine SDK branch contains `docs/engine-faq.md` with this marker and title:

```markdown
<!-- parr0t-engine-faq:v1 -->
# Engine exception runbook
```

When no manual exception is known, the file contains:

```markdown
_No SDK-external engine exceptions are currently documented._
```

Every entry uses a level-two heading and all of these fields:

```markdown
## Neutral condition name

- **Trigger:** The public signal or deterministic error that selects this entry.
- **Automation boundary:** Why the SDK cannot safely decide or perform the next action.
- **Stop condition:** The point beyond which the agent must not continue automatically.
- **Required input or tool:** The external material, tool, runtime evidence, authorization, or user decision required.
- **Procedure:** A bounded, provenance-free manual or external procedure.
- **Resume verification:** The deterministic check that permits the SDK workflow to continue.
- **Do not:** Unsafe guesses, substitutions, mutations, or workarounds that remain prohibited.
```

Run `python scripts/check_engine_faq.py docs/engine-faq.md` on the SDK branch before publication. The checker validates the marker and required fields; it does not require any entries.

## Use from another session

After selecting an SDK branch, inspect the engine runbook headings and `Trigger` fields. Load the full matching entry only when its trigger is present. Do not apply an entry by engine-name resemblance alone, and do not read unrelated entries into the data plane. If no trigger matches, continue with the SDK protocol or classify the new condition rather than improvising from an unrelated workaround.
