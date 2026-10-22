# Branch update and verification contract

This contract governs SDK maintenance against one intended core revision. It separates the need to update Git history, the presence of current core behavior, and validation of a proposed combined tree. It does not authorize publication, SDK ownership changes, or a real game action.

## Entry and fixed inputs

Use `scripts/update_branches.py` for update decisions and `scripts/check_branches.py` for branch health checks. Refresh authoritative remote refs at most once before planning when requested and available; neither command fetches implicitly. Pin the main and SDK commit IDs in one inventory. Reuse that inventory in validation rather than enumerating branches or resolving the same refs again.

The updater is the only core-propagation route. Do not answer a hold by running a manual `merge`, inventing a resolution commit, or combining local and remote SDK tips. Repair the exact categorical prerequisite and compute a fresh plan. This rule applies even when an ordinary Git merge appears easy: only the updater binds the resulting tree to complete validation evidence and records that binding in the merge commit.

Keep cache, snapshots, logs, plans, and results in an external state directory. Planning leaves branch refs and checked-out files unchanged. Git may create unreachable merge-tree objects as internal metadata. A stored plan is a report only: never load it as permission or evidence for a later apply operation.

```text
python scripts/update_branches.py --repo <repository> --main-ref origin/main --state-dir <external-state>
```

Default planning includes the inventoried origin SDK refs. Repeat `--branch <local-sdk-branch>` for selected local targets. Network failures do not authorize using guessed refs or creating a replacement SDK.

SDK branches declare Python validation dependencies in `requirements.txt`, `requirements-*.txt`, or `*-requirements.txt`, including under `profiles/`. Such a branch requires `--provision-dependencies`; ambient packages never satisfy its evidence because a different version can appear compatible. The exact files install into a content-addressed directory below the external state directory. This is an explicit network/package-install action. Provisioning never writes into the repository, user Python installation, or another SDK's environment. Evidence includes every installed dependency byte; later mutation is a hold. A failed or timed-out installation is cached for the same dependency and Python identity; use `--force` only after its cause changes.

An operator may provide prebuilt external wheels through standard `PIP_FIND_LINKS` when the host cannot build a transitive source distribution. The resulting installed bytes, not the wheelhouse path or ambient package set, bind the validation proof. Never add downloaded packages or wheel caches to an SDK branch.

An SDK that needs external compiler or library files declares their environment-variable names in one `profiles/sdk-validation.json` using schema `parr0t.sdk-validation.v1` and `required_environment_files`. The checker requires absolute existing files, binds their content hashes into evidence, and rejects a change during tests. Missing files or an invalid manifest hold only that SDK with a categorical code; the checker never guesses a path, downloads a native tool, or converts the hold into a merge conflict. Use `--environment-key` for other unversioned tools or editable dependencies.

## Update decisions

| Action | Required evidence | Work to skip or perform |
| --- | --- | --- |
| `skip_up_to_date` | Intended main is already an ancestor; no conflicting core customization | No merge-tree construction, snapshot, environment probe, or test suite for this update decision |
| `skip_compatible` | Core-owned files, including modes and deletions, equal intended main; complete current validation passes | No history-only merge; reuse passing content-bound evidence |
| `update_ready` | Core changes remain, or exact ancestry is required; Git merge tree equals the tested combined snapshot; all checks pass without skips | Eligible for a separately requested local apply |
| `manual_core_reconciliation` | SDK overrides a core-owned path with different content | Preserve the branch; stop automatic update for this target before expensive tests |
| `manual_merge_review` | Git reports a merge conflict or its tree differs from the snapshot | Preserve the branch; do not invent a resolution |
| `validation_held` | A required check fails, is missing, or contains skipped tests | Preserve the branch and report the incomplete evidence |
| `apply_held` | A checked-out target is dirty or has an operation in progress, refs changed, signing failed, or the exact update was rejected | Preserve existing refs and user files; report the exact categorical cause |

`skip_up_to_date` is a synchronization decision, not a claim that SDK tests or runtime validation passed. A health audit may still be requested separately. A passing simulation of latest core plus SDK additions is `update_ready` when the original branch lacks core changes. It must not be described as already updated or used to skip required core fixes. Earlier reports that called such simulations `up_to_date_compatible` are not sufficient update evidence.

`--require-ancestry` disables the history-only exception for equivalent core content. Use it when the SDK promotion contract requires the exact intended main revision to be an ancestor. A compatible-maintenance result cannot waive the promotion commit gate.

## Validation reuse and budget

Use the existing branch checker and its cache; do not add a second test runner or verifier. Bind evidence to the effective source tree, authoritative privacy/FAQ validators, checker, Python/platform/package metadata, relevant environment paths, and explicit environment key. Keep branch identity and current commit separately from content identity: empty commits do not require the same tests again. A changed adapter invalidates its evidence; a changed core invalidates affected combined trees. Validate the pinned main snapshot with the complete core suite once. For every non-diverged SDK overlay, run only its branch-owned adapter tests against that exact main snapshot; those tests own core/adapter integration coverage. Do not repeat the complete unchanged core suite per SDK. A core-diverged branch remains manual review and may receive a complete diagnostic audit without authorizing automatic application.

Known core overrides and already synchronized branches need no broad suite merely to decide whether an update should happen. Batch remaining independent validation with the existing bounded concurrency. Require complete core and adapter tests, privacy checks, and engine FAQ validation before applying an update. A partial cached result stays partial; an exit code alone is not a compatibility proof. Change `--environment-key` for modified unversioned tools or editable dependencies whose metadata is unchanged.

After an applied merge, the source tree is exactly the validated tree. Do not rerun unchanged suites merely because the merge commit ID is new. A subsequent update invocation detects main ancestry and performs zero validation runs for that branch. Preserve completed validation evidence for later health audits.

## Explicit local application

An agent may run `--apply` only after the user requests the local SDK merge/update action and its named targets. Implementation or inspection authorization alone does not authorize applying the current plan. No push, tag, release, or remote write is included.

```text
python scripts/update_branches.py --repo <repository> --main-ref origin/main --state-dir <external-state> --branch <local-sdk-branch> --apply
```

`--apply` requires explicit local `sdk-*` names. It computes a fresh plan, validates immutable inputs, and accepts only `update_ready` rows. Remote-tracking refs are never update destinations. An unoccupied ref advances through a compare-and-swap transaction. A target already checked out in exactly one clean linked worktree advances there with an exact fast-forward to the preconstructed merge commit; the updater rechecks its branch, old tip, operation state, cleanliness, resulting tree, and final cleanliness. A dirty worktree or operation in progress is held. There is no automatic checkout, stash, reset, rebase, conflict resolution, deletion, or force push.

Construct a merge commit with the original SDK and intended main as parents, preserve the configured signing requirement, and record `Parr0t-Core`, `Parr0t-Validated-Tree`, and `Parr0t-Validation-Key` trailers. Then either atomically verify the expected main ref and compare-and-swap an unoccupied SDK ref, or fast-forward its exact clean linked worktree. A signing, ref, cleanliness, or fast-forward failure must never trigger an unsigned fallback, manual merge, or overwrite of a newer tip. Other targets may continue independently. A rejected update may leave an unreachable commit; do not delete repository objects as cleanup.

Use one caller per state directory. Do not concurrently change worktree topology or edit update targets while applying. The updater rechecks occupancy immediately before application; Git ref compare-and-swap protects unoccupied refs, while the checked-out path uses Git's clean fast-forward protections. Arbitrary external worktree operations still require serialization by the caller. This command does not lock or control other Git clients.

## Report

Record the pinned revisions, per-branch decision, exact merge tree, evidence reuse/execution counts, changed local refs, and held causes. Distinguish planning, validation, and actual application. Report remaining core customizations or skipped tests without labeling the whole update successful. Store only reusable code and this contract in the repository; retain execution evidence externally.
