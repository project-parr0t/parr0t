# FAQ

This file contains provenance-free troubleshooting for the engine-neutral Parr0t framework. It must not identify any localization job, game, source file, asset, dialogue, translation, workspace, model, or execution result.

Engine branches do not append implementation bugs or ordinary engine questions here. Under `docs/engine-faq-contract.md`, they keep only SDK-external manual exception procedures in their branch-local `docs/engine-faq.md`. A condition the SDK can resolve deterministically belongs in code and regression tests instead.

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

## An agent rebuilds an adapter even though a usable SDK branch exists

- **Symptom:** The first session starts writing a new unpacker or patcher without inspecting an existing `sdk-*` implementation, producing duplicate code and avoidable validation work.
- **General cause:** Branch discovery was advisory, only the current checkout was inspected, or a branch name was treated as the complete compatibility test.
- **Diagnosis:** Inspect the external workspace's SDK inventory, per-candidate commit-bound evaluations, and selected mode. A missing decision or an unevaluated plausible branch means implementation started too early.
- **Resolution:** Stop the duplicate implementation, refresh visible local and remote-tracking refs, inspect each plausible public contract, and record same-identity `reuse` or `extend` when possible. Record useful different-identity code as `reference` and select `derive-new`. Select `private-new` only after every candidate is categorically incompatible and none is a reusable reference.
- **Prevention:** Keep the `docs/sdk-selection.md` gate before extraction and let the CLI block data-plane stages and SDK promotion until both the current decision and its exact implementation activation are valid.

## Reusable SDK code is assigned to the wrong engine-version branch

- **Symptom:** A capability verified on one engine/version line is prepared for an older, newer, or otherwise different `sdk-*` branch merely because that branch supplied reusable code.
- **General cause:** Implementation reuse and compatibility ownership were treated as one decision. A reference implementation was mistaken for evidence that its branch should own the target runtime.
- **Diagnosis:** Compare the inspected runtime's target engine/version, the target recorded by `sdk-evaluate` and `sdk-select`, the selected mode, and the proposed promotion branch. A differing identity may be a `reference`, but it cannot be `compatible` or `partial` for the target.
- **Resolution:** Use `reuse` or `extend` only for the exact target identity. For useful code from another identity, record `reference`, select `derive-new`, create the target branch from `main`, and port only the neutral capability verified on the target line. Never merge the reference SDK branch wholesale.
- **Prevention:** Require explicit target engine/version arguments at evaluation and selection time, reject identity-mismatched extension, and verify that promotion matches the recorded target before exporting or publishing.

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
- **Resolution:** Keep text-bearing envelopes in the private workspace, normalize the parent-facing state to `fallback_required`, and continue the configured route without parent-side inspection. Preserve the exact source only when v2 route evidence proves every configured route was actually attempted and exhausted.
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
- **General cause:** Quality classification, strong judgment, and correction were combined, or the strongest judge ran universally despite a low defect rate.
- **Diagnosis:** Check whether accepted candidates are echoed, whether every target reaches the strong model, and whether a correction model runs for every target.
- **Resolution:** Run deterministic checks and complete low-cost compact triage first. Send only triage flags/failures, evidence-backed high-risk targets, and the deterministic sample to the strong judge; rewrite only its `rewrite` targets and confirm corrections.
- **Prevention:** Bind the triage route, sample rate, risk metadata, and per-task output limits in configuration; retain focused context and compare quality/cost on representative evaluations before changing roles.

## A bounded review task still makes one model call per record

- **Symptom:** Task files contain many IDs, yet prompt count and model/API calls grow almost one-for-one with records; input greatly exceeds output and elapsed time scales serially.
- **General cause:** Target-level failure isolation was misread as a requirement for target-level requests, or a job-local coordinator contract overrode the batch intent.
- **Diagnosis:** Compare task count, record count, prompt artifacts, and semantic call count. More than one semantic request for a multi-target native task violates its v5 detail contract.
- **Resolution:** Preserve completed shards, discard only the conflicting local execution instructions, and regenerate the remaining v5 manifest. Load shared context once, produce one ordered batched verdict set, and retain failures per target. If the provider truly accepts one target, repartition before dispatch.
- **Prevention:** Treat `parr0t.detail-contract.v1` as supplemental-only, reject free-form execution contracts, and enforce the bulk pilot/budget gate before later waves.

## An MV plugin or script command disappears after translation

- **Symptom:** Visible text changes correctly, but the surrounding plugin command or JavaScript call is missing or malformed.
- **General cause:** The patcher replaced the entire JSON string even though only one span inside that string was translatable.
- **Diagnosis:** Compare the manifest mapping with the baseline and confirm that plugin and script targets record a parent path plus an exact span. Script literals must also record their quote style.
- **Resolution:** Replace only the recorded span. Encode script text as a JavaScript literal with the original quote style, then reparse the rebuilt JSON.
- **Prevention:** Use synthetic round-trip tests for plugin payloads, single- and double-quoted literals, apostrophes, backslashes, tabs, and newlines.

## Applying a later MV target reports a false baseline mismatch

- **Symptom:** The baseline precondition passes for an early target but fails for another valid target in the same JSON document.
- **General cause:** Preconditions were checked against the mutable output document after earlier assignments had already changed it.
- **Diagnosis:** Check whether the patcher keeps separate immutable baseline and mutable output objects for every file.
- **Resolution:** Validate all source values, mirror values, and spans against the immutable baseline. Accumulate assignments and publish output only after every precondition succeeds.
- **Prevention:** Test multiple targets in one parent string and conflicting assignments to one JSON path.

## An MV dialogue block no longer aligns with event commands

- **Symptom:** A rebuilt event has missing, merged, or shifted text lines even though the translation itself is readable.
- **General cause:** A multi-line target was split into a different number of lines than its recorded Show Text or Scrolling Text command paths.
- **Diagnosis:** Compare the candidate newline count with the manifest path count before writing any JSON.
- **Resolution:** Reprocess the target with the required line count or preserve its exact source. Never redistribute lines heuristically during patch assembly.
- **Prevention:** Extract consecutive text commands as one target and test one- through four-line synthetic blocks.

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
- **Resolution:** Return to the smallest design: immutable source, explicit payload, one status marker and receipt, owned-path collision checks, one package validation, and a clear Restore operation. Preserve only original files explicitly declared for replacement. Move optional migration work to a separately approved task.
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

## A published asset differs from the staged release or the branch contains artifacts

- **Symptom:** A downloaded asset differs from the verified staged bytes, a deployable archive or generated payload is committed to the instance branch, an asset contains an undocumented first-party file, or compatibility is not listed consistently in the README and manifest.
- **General cause:** The package was rebuilt or edited after verification, the instance branch was treated as an artifact store, release metadata was updated independently, or compatibility was described as “latest” without exact build bindings.
- **Diagnosis:** Compare the tag, instance/translation-source/SDK commits, checkpoint, release ID, package/manifest/file digests, tracked-file allowlist, compatibility records, release notes, and installer binding checks as one immutable release unit.
- **Resolution:** Stop distribution, keep the mismatched assets unchanged for diagnosis, rebuild one staged release from the intended clean commit, and request publication approval for the corrected release. Do not silently replace history or broaden compatibility claims.
- **Prevention:** Follow `docs/instance-release.md`, keep distributable bytes only in GitHub Releases, restrict the branch to its lean allowlist, require `game-l10n instance-release-gate` to return `ready` for the exact local commit and asset before any external publication, upload those exact bytes, perform one read-only post-upload digest check, enumerate every validated original game build, and preserve the user's README through minimal reviewed edits.

## Packaging succeeds and the agent skips the instance release contract

- **Symptom:** A valid installer or ZIP is treated as ready to publish; the agent uses a generic GitHub Release workflow, commits the archive to the branch, replaces the existing README, or discovers the checkpoint and lean-branch rules only after a user correction.
- **General cause:** Package correctness and publication correctness were collapsed into one state, while the instance contract existed only as prose and was read too late.
- **Diagnosis:** Check whether `parr0t-instance-release.json` exists in the committed lean tree and whether `.game-l10n/instance-release-gate.json` reports `ready` for the current instance commit and exact asset digest. A package-validator pass is not equivalent evidence.
- **Resolution:** Stop before push/tag/release/upload. Restore the user's README, remove deployable artifacts and unexplained files from the branch, add the canonical checkpoint and release metadata, commit locally, and run the instance release gate. Publish nothing until it passes.
- **Prevention:** On an explicit instance-publication request, read `docs/instance-release.md` before editing the target repository. Use the dedicated gate rather than a remembered checklist or generic `gh release create` sequence.

## An agent proposes a repository or release during local work

- **Symptom:** A translation, package build, or local validation report unexpectedly asks whether to commit/push, names a public or private destination repository, or presents tags, GitHub Releases, uploads, or publication gates as the next step.
- **General cause:** Successful local completion or permission to use Git was incorrectly treated as publication intent, or generated workspace metadata contained an automatic publication prompt.
- **Diagnosis:** Check the active user request, generated profile metadata, status output, and completion message. If the user did not first command publication of the exact target, every publication-related prompt or state transition is out of scope.
- **Resolution:** Keep all artifacts local, remove the unsolicited publication step from the active plan and user-facing status, and continue or stop at the requested local deliverable. Do not convert the correction into a new request for publication approval.
- **Prevention:** Keep publication workflows dormant and silent until activated by a user-originated command. Generated profiles and ordinary status output must not advertise publication; a successful patch, local Git choice, or SDK promotion never activates it.

## A GitHub Release title omits the compatible game version

- **Symptom:** The visible release title contains only a patch version or uses a vague label such as `latest`, leaving players unable to tell which original game version it supports.
- **General cause:** Patch identity and original-game compatibility were maintained as separate metadata and the title was not validated against the supported-build matrix.
- **Diagnosis:** Compare the visible title with the README, release notes, manifest, asset metadata, and installer binding checks. The exact supported game-version set must agree everywhere.
- **Resolution:** Before publication, set the title to `<Game Display Name> Patch <Patch Version> (Game Version <Game Version>)`; use the exact enumerated `Game Versions` form only for multiple validated builds. If the game version is unknown, keep the release private and unresolved.
- **Prevention:** Make the release-title format a required gate and reject bare patch versions, unverified ranges, and ambiguous compatibility labels.

## A release works on the build agent but not on the player's machine

- **Symptom:** The package depends on an absolute workspace path, developer tool, environment variable, host architecture, or command that exists only on the machine that built it.
- **General cause:** The build environment was treated as the deployment target instead of producing a self-contained package for the player's operating system and supported game build.
- **Diagnosis:** Inspect the archive, scripts, manifest, prerequisites, and README for build-host paths or tools; test extraction and entry points from a clean target-environment fixture with spaces and non-ASCII characters in its path.
- **Resolution:** Rebuild the staged package with relative paths, runtime discovery, declared target architecture, and only bundled or explicitly documented redistributable prerequisites. Keep the broken release unchanged until a corrected release receives publication approval.
- **Prevention:** Default unspecified public distributions to Windows, derive architecture and runtime requirements from the game build rather than the agent host, and validate the exact packaged interface as an end user would receive it.

## An interactive installer asks the player for an agent approval or prints internal exceptions

- **Symptom:** A player-facing batch installer stops for a runtime-approval JSON, mentions workspace policy, or exposes raw internal `E_*` exceptions instead of actionable guidance.
- **General cause:** The noninteractive agent automation guard was applied to the interactive distribution interface, or installer messages and codes were improvised per task.
- **Diagnosis:** Run the exact packaged batch file in a synthetic game directory. Its menu choice and final target/action confirmation must authorize the interactive mutation, while noninteractive PowerShell mutation without approval must fail with a public `PARR0T-E###` code. Inspect the ZIP for harness and workspace artifacts.
- **Resolution:** Rebuild with the shared installer contract. Keep runtime approval only on noninteractive mutation, map failures to the fixed player messages, and expose internal details only in diagnostic mode.
- **Prevention:** Validate the fixed archive entries, result-code registry, Korean menu, one interactive lifecycle, and one denied noninteractive mutation before release.

## Updating a launcher wrapper loses the real original or Restore puts back the wrapper

- **Symptom:** An update backs up the already installed wrapper as though it were the original, or Restore deletes/reinstalls the wrong executable.
- **General cause:** An original-file replacement was treated as an ordinary overlay collision and was not bound to a stable backup in the first-install receipt.
- **Diagnosis:** Compare `parr0t-patch-status.txt`, the receipt's patch ID and original records, the current replacement set, and the sizes/hashes of both installed targets and backups. Do not move files while diagnosing.
- **Resolution:** Stop before mutation on any mismatch. Use the original matching installer to Restore, then install a release whose declared replacement and backup paths are correct. Never adopt an unknown existing backup.
- **Prevention:** Declare every original replacement in the package spec, preserve it only on first install, require the same replacement set on update, verify both copies, and exercise install/update/verify/restore with a synthetic replacement file.

## A report says zero missing entries but visible text is still untranslated

- **Symptom:** Result validation reports no missing IDs, yet runtime screens, descriptions, tooltips, or dynamically assembled labels still contain source-language text.
- **General cause:** Completeness was measured against the extractor's own manifest, making the denominator circular, or a source class was excluded to avoid overcapture.
- **Diagnosis:** Compare two independently produced inventories and inspect their declared user-visible exclusions. Keep result alignment separate from runtime observations.
- **Resolution:** Repair the engine SDK extraction method until both inventories agree without user-visible exclusions, regenerate context and results for the changed fingerprint, and record runtime coverage only from a user-executed or separately authorized observation.
- **Prevention:** Require `game-l10n verify-inventory`, use the terminology in `docs/coverage-gates.md`, and never restate `manifest_missing_count=0` as zero in-game omissions.

## A partial context file is mistaken for completed analysis

- **Symptom:** Translation starts after a context worker was interrupted, unavailable, or at capacity because an output file happens to exist.
- **General cause:** Artifact existence was accepted instead of a successful per-part route and evidence-coverage attestation.
- **Diagnosis:** Check the context-state fingerprint, execution status, completed-part count, and domain coverage rather than the context-pack path alone.
- **Resolution:** Route the incomplete part through the next configured analyzer. If every route fails, stop without writing a successful context state or starting translation.
- **Prevention:** Use the CLI context gate, write output and state atomically, and reject partial or stale attestations.

## A transient provider failure leaves a patch partly usable

- **Symptom:** Capacity, timeout, quota, rate-limit, authentication, or malformed-output failure preserves source text and the workflow proceeds as if the patch were complete.
- **General cause:** Operational failure was collapsed into refusal or final source preservation, and indispensable targets were not identified.
- **Diagnosis:** Inspect the metadata-only failure queue for categorical failure codes and `blocking` state; do not expose source text or provider prose to the control plane.
- **Resolution:** Keep operational failures queued until the user explicitly asks to resume. If any queued target is marked `metadata.blocking_on_failure=true`, retain all results as a private held checkpoint and emit no new patch.
- **Prevention:** Mark required targets during extraction, preserve the failure queue across turns, bind it to the inventory fingerprint, and resume only through `game-l10n resume-failures`.

## A prior release is available but the agent translates everything again

- **Symptom:** Supplying a released package with the current game folder does not shorten the next translation or a small wording correction triggers a full translation and review pass.
- **General cause:** The distribution lacks a validated continuation checkpoint, or reuse was decided from filenames and target IDs without comparing source, location, dependency, context, review-policy, SDK, and game-build bindings.
- **Diagnosis:** Validate `parr0t-release-checkpoint.json`, compare its manifest digest and IDs, regenerate both current inventories, and count exact reusable, invalidated, new, and unresolved targets without exposing text to the control plane.
- **Resolution:** Reuse only matching `reviewed` targets. Translate and review new, changed, `source_preserved`, or otherwise invalidated targets; for a user-requested correction, add the selected stable IDs and their transitive reverse dependencies. Run a full semantic review only when the user requested one or a global binding changed.
- **Prevention:** Include the checkpoint in every distribution, keep stable SDK-defined IDs and dependency fingerprints, and follow `docs/release-checkpoint.md` before making model calls.

## A game update introduces a locale key that the patch does not contain

- **Symptom:** A newly updated game displays no text or crashes while resolving a localized key absent from an older patch.
- **General cause:** The engine requires a complete locale shape or replaces locale data wholesale, and the patch has no verified lookup fallback for unknown keys.
- **Diagnosis:** In a synthetic SDK fixture, add an unknown key with the same type and container shape as the engine resource and observe whether lookup reaches the base locale, source text, a placeholder, or an invalid state.
- **Resolution:** When the SDK supports it safely, preserve missing keys through a verified base-locale, exact-source, or nonempty-placeholder fallback while retaining placeholders and data shape. If no safe mechanism exists, record the checkpoint fallback as `unsupported` with a neutral reason and retain strict build binding instead of claiming protection.
- **Prevention:** Declare and test the fallback mode in the engine SDK, bind it into each release checkpoint, and never treat fallback as permission to install on an unvalidated game build.

## A translated UI value repeats its label or describes the wrong state

- **Symptom:** The rendered UI shows a label twice, joins two translated fragments incorrectly, or uses a literal open/close-style word where the control means enabled/disabled.
- **General cause:** Extraction represented a render-time value or state slot as a standalone sentence, so translation and review saw the dictionary meaning but not the surrounding label, composition pattern, or semantic state.
- **Diagnosis:** Inspect the static resource binding or render call site without launching the game. Identify each independently localized slot, its related stable IDs, render pattern, semantic state, fixed target affixes, and any hard display limit.
- **Resolution:** Add the evidence-backed fields from `docs/segment-metadata.md`, invalidate only the affected IDs and composition dependents, and translate/review the slot payload again. Reject an explicitly repeated fixed affix deterministically.
- **Prevention:** Make UI slot metadata part of both independent inventories and compact review. Keep labels and values separate when the runtime does, and do not infer composition from wording alone.

## An installed patch is mistaken for an upstream game update

- **Symptom:** An agent sees files differing from a clean source or prior extraction and unnecessarily repeats unpacking, translation, or packaging even though the known patch is still intact.
- **General cause:** The game root was compared with an unpatched baseline without first reading `parr0t-patch-status.txt` and its matching receipt, or any mismatch was treated as proof of a game update.
- **Diagnosis:** Run `game-l10n inspect-installed-state` against the exact configured game root. It hashes only receipt-owned installed files and preserved original backups and performs no mutation.
- **Resolution:** Reuse the recorded state when it reports `installed_intact`. Investigate `possible_game_update` as an advisory external-change signal before attributing its cause; stop all mutation on `damaged_state` until preserved originals are recovered.
- **Prevention:** Use receipt-scoped inspection before whole-game comparison or re-extraction, and never classify a game update from a patched-file mismatch alone.

## A one-line correction consumes a full translation run

- **Symptom:** Fixing an exact typo or user-specified wording takes many minutes, invokes a long-running discovery worker, reloads full context, checks the complete game tree, or creates several one-off scripts and reports.
- **General cause:** The correction was treated as a new localization job instead of a target-ID delta against a verified result and release checkpoint. Broad safety checks were repeated even though their inputs did not change.
- **Diagnosis:** Compare elapsed tool time with agent reasoning/wait time, count model/context calls, list reread artifacts, and identify whether unchanged targets, source files, installation state, or package layers were revalidated without an invalidating input.
- **Resolution:** If the user supplied the exact final wording, run `game-l10n apply-user-correction`, then repack only the owning payload and verify the package once. If reverse dependencies are reported, review only those IDs. Use one narrow static lookup only when the stable mapping is ambiguous.
- **Prevention:** Preserve the full reviewed result and release checkpoint, keep corrections in the current agent, prohibit job-specific correction scripts, and enforce the zero-model exact-correction budget in `docs/minimal-corrections.md`.

## Native worker token use grows much faster than target count

- **Symptom:** A large translation or review pass has reasonable unique input and output, but aggregate input is orders of magnitude larger, cached input dominates usage, the root makes hundreds of model calls, controller agents spawn more agents, or a worker remains alive across several shards.
- **General cause:** Long root/control sessions and nested model controllers replay instructions, tool definitions, conversation history, command results, and prior progress on every call. Cache hits reduce repeated computation but do not turn the repeated input into zero usage.
- **Diagnosis:** Compare root and descendant session counts, maximum agent depth, model-call counts, aggregate input, cached input, output, progress messages, and work completed without reading game text. Any model controller, depth above one, follow-up shard, repeated contract read with the same digest, or continued turn beyond the manifest ceiling violates the execution contract.
- **Resolution:** Regenerate `prepare-native-pass` v4 tasks. Have the root dispatch them directly to fresh no-history workers, write task-named results to a private wave directory, import the complete wave once with `import-native-wave`, and merge with the same manifest. Checkpoint when a root/worker ceiling is reached. Do not repair a long session with per-shard root turns, summaries, compaction, recovery controllers, or more nested agents.
- **Prevention:** Keep the default 64-ID/24,000-estimated-material-token bounds and preferred eight-record minimum, never reuse completed workers, prohibit worker delegation, and treat a high cached-input ratio as a stop signal rather than evidence that a long session is efficient.

## A handful of residual review items takes hours to finish

- **Symptom:** The complete pass is done and only a few corrections remain, but the job creates controllers, ownership or lease records, liveness checks, proof bundles, repeated context supplements, or dozens of status waits instead of finishing the targets.
- **General cause:** Bulk-work reliability machinery was recursively rebuilt for the residual tail. Capacity failures then restarted a long-lived root turn, so metadata and conversation history grew while the remaining target count did not.
- **Diagnosis:** Compare the frozen residual count with worker dispatches and state changes. More than one task for at most 32 targets, any job-local orchestration layer, repeated identical progress snapshots, or a capacity restart of the root turn violates the residual convergence contract.
- **Resolution:** Keep every passed result, generate one `prepare-native-pass` v4 residual task, run it in one fresh no-history worker, and aggregate once. On capacity or startup failure, preserve that exact task in resumable operational state and return; do not create another controller or automatic continuation.
- **Prevention:** Keep `residual_direct_records=32`, `job_local_controller_allowed=false`, `automatic_capacity_restart_allowed=false`, `max_unchanged_progress_observations=1`, and `max_control_actions_without_progress=2`. Report only at wave boundaries or blockers. Semantic quality checks and the existing two-round review limit still apply; only orchestration churn is removed.

## A short API fallback target consumes tens of thousands of input tokens

- **Symptom:** Source strings are short, but each compatible-provider request is extremely large, many targets reuse nearly identical input, or provider input usage dwarfs output by several orders of magnitude.
- **General cause:** A complete context pack or exchange artifact was nested into every segment's metadata, target-relevant projection was bypassed by a job-local request loop, or target-specific text appeared before the reusable prompt prefix.
- **Diagnosis:** Use aggregate character and provider-usage telemetry without opening game text. Compare request count, average combined input size, cached-input/cache-miss counts, and the configured per-task bound; check structurally whether metadata contains a context pack, bundle, or attachment.
- **Resolution:** Stop the job-local loop, retain completed successes, and resume only pending IDs through the configured adapter. Move shared analysis back to the typed `ContextPack`, let the prompt builder project relevant evidence, and keep `budget_exhausted` targets queued until duplicated context is removed or a larger bound is explicitly justified.
- **Prevention:** Keep the preset 24,000-character text-request preflight, never embed full context containers in `Segment.metadata`, preserve stable-field-first prompt order, and retain provider cache telemetry. Retry counts need not be reduced; every retry is still subject to the same per-request bound.
