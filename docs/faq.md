# FAQ

This FAQ contains only provenance-free issues that can recur across independent SDK or engine integrations. It must not identify any localization job, game, source file, asset, dialogue, translation, workspace, model, or execution result.

## A private repository returns 404 even though it exists

- **Symptom:** A browser, connector, or agent cannot open a known private repository and reports `404 Not Found`.
- **General cause:** Git hosting services may hide private resources from unauthenticated or unauthorized clients. Browser, connector, and terminal Git credentials can have different scopes.
- **Diagnosis:** Verify the remote with authenticated Git and confirm the connector or token explicitly includes the repository.
- **Resolution:** Authenticate the relevant client or grant the Git application access to that repository. Prefer a repository-scoped credential with the minimum required permission.
- **Prevention:** Validate repository access at task startup without placing credentials in prompts, configuration, or logs.

## An identical unpack or pack step runs again

- **Symptom:** A cached tool stage executes even though its source and tool appear unchanged.
- **General cause:** The cache fingerprint includes a generated output, an unstable path, or other state that changes after the first run.
- **Diagnosis:** Compare only the normalized command, source content, reusable tool files, and relevant configuration used to construct the fingerprint.
- **Resolution:** Exclude generated outputs and volatile metadata from the fingerprint, then regenerate the stage state.
- **Prevention:** Test every cached stage by running it twice and requiring the second run to report `skipped`.

## A model refuses fictional game localization

- **Symptom:** A context, translation, or review provider refuses a bounded transformation because the fictional text contains mature themes.
- **General cause:** The provider does not recognize the request as inert, user-supplied fictional content or its policy disallows the transformation.
- **Diagnosis:** Check the structured refusal status without persisting the source text in repository logs.
- **Resolution:** Retry once with the bounded transformation context clarified; if refusal remains, route to the next configured provider. Do not obfuscate content or weaken safeguards.
- **Prevention:** Configure independent provider chains and test refusal routing with synthetic fixtures.

## One mature target causes an entire translation batch to be refused

- **Symptom:** A provider rejects unrelated ordinary targets because they were submitted together with one target or context field that triggered a refusal.
- **General cause:** Multiple output targets were bundled into one model request, or context-only material was not distinguished from the text that actually required transformation.
- **Diagnosis:** Confirm that every provider request has exactly one translation target and that refusal records identify only that target ID.
- **Resolution:** Continue the unaffected targets individually. Queue only the refused target for the configured fallback after the primary stage completes; never rebuild the refused collection into another multi-target request.
- **Prevention:** Label full-story, scene, character, adjacent-line, and glossary material as context-only evidence while preserving one output target per request.

## A fallback provider reaches its quota or every route fails

- **Symptom:** Translation or review cannot produce a validated result after the configured routes run.
- **General cause:** A provider may refuse, return malformed output, encounter a rate/quota limit, or fail deterministic integrity checks.
- **Diagnosis:** Inspect text-free attempt metadata and queue transitions for the target ID.
- **Resolution:** Emit the exact source as `source_preserved` and continue unrelated targets. Do not label the preserved source as translated or reviewed.
- **Prevention:** Configure independent translation and review routes, validate keys with `doctor`, and retain explicit unresolved counts in the patch summary.

## A provider refusal explanation influences the parent orchestrator

- **Symptom:** The parent agent sees a provider's prose explanation or the affected dialogue and treats a route failure as a reason to omit content or stop patch creation.
- **General cause:** Data-plane model output was copied into a control-plane message or audit record instead of being reduced to categorical metadata.
- **Diagnosis:** Inspect schemas and logs with synthetic fixtures; parent-facing records should contain only target IDs, categorical status/error codes, counts, artifact paths, queue transitions, and deterministic issue codes.
- **Resolution:** Keep text-bearing envelopes in the private workspace, normalize the explanation to a category such as `provider_refusal`, and continue the configured fallback route. If every route fails, preserve the exact source without removing the target.
- **Prevention:** Make workers read/write private artifacts directly and require metadata-only completion envelopes at every parent boundary.

## A resumed review refers to an older translation candidate

- **Symptom:** Review appears complete even though a retry or rewriter changed the candidate afterward.
- **General cause:** Review state was keyed only by target ID and did not include an immutable candidate generation and digest.
- **Diagnosis:** Compare the generation and digest recorded by the review attempt with the current candidate metadata.
- **Resolution:** Invalidate the stale review and review the current generation. Reject resume state whose candidate text does not match its digest.
- **Prevention:** Finish translation fallbacks before review, increment candidate generation on every content change, and persist stage checkpoints atomically.

## A temporary provider failure is recorded as a refusal

- **Symptom:** A malformed response, timeout, or transport error skips recoverable retries or is reported as a content refusal.
- **General cause:** Provider failures were collapsed into one undifferentiated status.
- **Diagnosis:** Inspect the categorical finish signal, HTTP class, and parser outcome without copying provider prose into the control plane.
- **Resolution:** Reserve `explicit_provider_refusal` for an explicit refusal field or content-filter finish reason. Apply bounded grace retries to retryable parser, transport, rate-limit, timeout, and integrity failures.
- **Prevention:** Keep a stable failure taxonomy and test each category with synthetic provider responses.

## Review cost is high even when most candidates are already correct

- **Symptom:** The reviewer returns a full copy of every translation and consumes nearly as many output tokens as translation itself.
- **General cause:** Quality classification and correction were combined into one response contract.
- **Diagnosis:** Check whether accepted candidates are echoed by the reviewer and whether a correction model runs for every target.
- **Resolution:** Use a compact judge that returns only `ok` or `rewrite` plus categorical issue codes. Invoke a rewriter only for `rewrite`, validate the correction, and confirm it with another compact judgment.
- **Prevention:** Set per-task output limits, retain focused scene/character evidence, record provider usage metadata, and compare quality and cost on synthetic representative evaluations before changing the configured roles.

## A semantically accurate title or short line still reads like a literal translation

- **Symptom:** A candidate preserves dictionary meaning but loses idiomatic phrasing, a recognizable allusion or wordplay cue, deliberate alteration, atmosphere, or the cadence expected of its text function.
- **General cause:** The judge treats semantic equivalence as sufficient, or receives no reliable indication that the segment is a title, quest name, slogan, UI label, or reference-bearing line.
- **Diagnosis:** Check segment metadata and target-scoped context cues, then inspect whether the compact verdict can report `literalism`, `reference_loss`, or `tone_rhythm` without echoing private text.
- **Resolution:** Route only the flagged target to the configured rewriter with its focused scene, voice, text-function, and evidence-backed reference cues; confirm the correction with another compact judgment.
- **Prevention:** Analyze the ordered script and metadata before translation, preserve communicative effect instead of source word order, and never manufacture a reference from weak resemblance.

## A small installer request expands into a migration framework

- **Symptom:** A simple package or launcher takes much longer than translation because the agent adds legacy fingerprints, embedded previous releases, generalized rollback, or compatibility logic that was not requested.
- **General cause:** Safety and reuse were treated as unlimited goals instead of constraints proportional to the active deliverable.
- **Diagnosis:** Compare each added component with the current acceptance criteria. If removing it still produces the requested recoverable patch, it is outside the active scope.
- **Resolution:** Return to the smallest design: immutable source, explicit payload, one receipt, owned-path collision checks, one package validation, and a clear remove operation. Move optional migration work to a separately approved task.
- **Prevention:** Follow `docs/execution-budget.md`, freeze scope per user request, and stop after the proportional acceptance checks pass.

## Steam discovery fails on a library that no longer exists

- **Symptom:** Automatic game discovery aborts before reaching a valid Steam library or manual-path fallback.
- **General cause:** A stale `libraryfolders.vdf` entry points to a missing directory or unavailable drive, and child paths are joined before the root is checked.
- **Diagnosis:** Enumerate only library root metadata and identify roots that fail an existence check; do not scan game content.
- **Resolution:** Skip every missing root before constructing `steamapps` paths, continue remaining roots, and prompt for an explicit game directory when no valid match remains.
- **Prevention:** Cover one stale-root case in the synthetic installer lifecycle and keep manual selection available.

## Unchanged artifacts are validated repeatedly

- **Symptom:** The same hashes, archive entries, build, or runtime state are checked many times without any input change.
- **General cause:** Successful validation is not tied to an input fingerprint, or confidence checks are run without a new failure hypothesis.
- **Diagnosis:** Compare content, tool, and configuration fingerprints between checks.
- **Resolution:** Reuse the prior pass while its fingerprint remains unchanged. Re-run only the layer invalidated by a changed input or failed check.
- **Prevention:** Record validation fingerprints and apply the stop conditions in `docs/execution-budget.md`.

## An agent installs or launches a patch while trying to validate it

- **Symptom:** A build, fix, test, or log-inspection request causes files to be written into a real game directory, an installed patch to be changed or removed, or the game/generated code to be launched before the user requests that action.
- **General cause:** Read access, artifact creation, runtime curiosity, or one external action was incorrectly treated as broad authorization for another external effect.
- **Diagnosis:** Compare the operation with the recorded runtime approval. A valid record must name exactly one of `install`, `update`, `remove`, or `launch` and match the current artifact digest, configured target, executable when launching, expiry, and unused state.
- **Resolution:** Stop before the external effect, keep the source and runtime data unchanged, and hand off the recoverable artifact for user execution. If the user explicitly requests agent execution, create and consume only the exact one-shot approval described in `docs/runtime-approval.md`.
- **Prevention:** Deny runtime mutations and process starts by default, keep installation and launch approvals separate, use only synthetic temporary targets for automated lifecycle tests, and never bypass the shared guard with direct shell copies or process commands.
