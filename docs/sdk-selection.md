# SDK discovery and reuse gate

This gate prevents an agent from rebuilding an engine adapter that Parr0t already has. It runs before extraction code is written and before any translation stage. The evidence and decision are stored only in the external workspace.

## Required order

1. Start from `main` and make the authenticated remote-tracking refs current when network access is available. A missing local checkout is not evidence that a branch does not exist. A Git checkout is one transport, not an activation requirement.
2. Initialize the external workspace. Initialization records all visible local and remote-tracking `sdk-*` refs without checking them out.
3. Run `game-l10n sdk-inventory --workspace <workspace> --refresh-remote` when authenticated Git access is available. This fetches only `sdk-*` tracking refs from `origin`; omit the flag for an explicitly offline run. Use `--remote <name>` when the authoritative remote has another name. When Parr0t or the selected SDK arrived as a ZIP/connector download, obtain the exact SDK branch snapshot through that authenticated source and register it instead:

```text
game-l10n sdk-inventory --workspace <workspace> --runtime-root <extracted-sdk-snapshot> --portable-branch sdk-<engine>-<version-line> --portable-commit <full-authenticated-commit-id>
```

The portable branch and full commit ID are provenance supplied by the authenticated source. The core independently fingerprints every runtime file. Do not derive either value from the folder name, invent it, or use a moving branch archive without recording the resolved commit.
4. Inspect each plausible candidate through its public documentation and the `public_contract_paths` reported by the inventory. Use `git show <inspect-ref>:<path>` or a separate read-only worktree; do not switch a dirty checkout and do not inspect private game data. For an exact selected SDK, inspect the headings and `Trigger` fields in `docs/engine-faq.md`; load a complete entry only when its trigger occurs. The runbook is an exception boundary, not general implementation guidance.
5. Record the target SDK identity from the engine and compatibility line actually observed in the private game workspace. For example, an exact editor/runtime build belongs to its declared public release line; never substitute the version of a convenient reference branch. Compare public format signatures, engine family, serialized layout, supported versions, extraction/application/packing behavior, integrity constraints, and required runtime hooks. Branch-name similarity alone is neither acceptance nor rejection evidence.
6. Record one categorical evaluation per candidate:

```text
game-l10n sdk-evaluate --workspace <workspace> --target-engine <engine> --target-version <version-line> --branch sdk-<engine>-<version-line> --result compatible --reason public_contract_match --evidence-path <inspected-public-path>
game-l10n sdk-evaluate --workspace <workspace> --target-engine <engine> --target-version <version-line> --branch sdk-<engine>-<version-line> --result partial --reason reusable_with_extension --evidence-path <inspected-public-path>
game-l10n sdk-evaluate --workspace <workspace> --target-engine <engine> --target-version <new-version-line> --branch sdk-<engine>-<older-version-line> --result reference --reason reusable_for_new_identity --evidence-path <inspected-public-path>
game-l10n sdk-evaluate --workspace <workspace> --target-engine <engine> --target-version <version-line> --branch <candidate> --result incompatible --reason <fixed-reason> --evidence-path <inspected-public-path>
```

7. Record exactly one decision:

```text
game-l10n sdk-select --workspace <workspace> --target-engine <engine> --target-version <version-line> --mode reuse --branch sdk-<engine>-<version-line>
game-l10n sdk-select --workspace <workspace> --target-engine <engine> --target-version <version-line> --mode extend --branch sdk-<engine>-<version-line>
game-l10n sdk-select --workspace <workspace> --target-engine <engine> --target-version <new-version-line> --mode derive-new --branch sdk-<engine>-<reference-version-line>
game-l10n sdk-select --workspace <workspace> --target-engine <engine> --target-version <version-line> --mode private-new
```

8. Activate the implementation that will actually execute the decision:

```text
# Bind the configured SDK checkout/worktree for reuse/extend.
game-l10n sdk-activate --workspace <workspace>

# Or explicitly bind an inventoried Git checkout or registered portable snapshot.
game-l10n sdk-activate --workspace <workspace> --runtime-root <selected-sdk-runtime>

# For derive-new, run from main after replacing both generated workspace tools,
# and bind the exact clean reference checkout as well.
game-l10n sdk-activate --workspace <workspace> --reference-checkout <reference-worktree>

# For private-new, run from main after replacing both generated workspace tools.
game-l10n sdk-activate --workspace <workspace>
```

`reuse` requires either a clean runtime at the exact inventoried Git commit or an exact content match to the registered portable snapshot. `extend` requires provable ancestry; an unchanged portable snapshot may still be reused, but modifying it cannot establish ancestry and therefore needs a Git worktree for the extension. `derive-new` binds both the new workspace adapter and the exact Git or content-bound reference. `private-new` binds the new workspace adapter. Generated pass-through tools never count as an adapter. The activation record fingerprints the runtime and, when applicable, the workspace tool manifest; changing, moving, or replacing either invalidates it.

A separate control-plane core may activate and validate the selected SDK without running from its checkout. For `reuse`/`extend`, activation defaults to the configured `sdk_root`; an explicit `--runtime-root` overrides it. Later checks validate the recorded SDK path and its complete binding, not the control-plane core's checkout. Changed SDK content, commit, worktree state, workspace tools, or references still invalidate activation; an explicit mismatching validation runtime is rejected.

The repository choice in the private game workspace is unrelated. `disabled` prohibits initializing or publishing that game workspace; it never blocks downloading, registering, or activating an SDK. A top-level agent must not turn that choice into a terminal blocker.

Extraction, context analysis, translation, packing, and SDK promotion remain blocked until both selection and activation are valid. For `reuse`/`extend`, use the selected branch's engine commands; the generic workspace `run-tool` command is reserved for a bound `derive-new` or `private-new` adapter.

## Extractor preparation before either inventory

Exact selection and activation precede preparation. An `extend`, `derive-new`,
`private-new`, `partial` evaluation, declared `extractor_requirements`, or an
existing preparation profile/evidence requires fresh extractor proof before
**either** primary or independent full inventory can be dispatched. Activation
identifies code; it does not prove missing extractors have been implemented.
Status reports `prepare_and_validate_extractors`, not inventory Repair.

Prepare the isolated implementation worker using the existing `source_probe`
role, exact configured model, permissions, transport and retry policy:

```text
game-l10n prepare-data-plane-task --workspace <workspace> --worker-role source_probe --purpose extractor_preparation
```

Its immutable task has `purpose: extractor_preparation`, stage `discovery`, a
separate default relay directory `.game-l10n/data-plane/extractor_preparation`,
and output `.game-l10n/extractor-preparation.json`. Dispatch only the core-bound
relay and include its generated worker prompt verbatim. The worker implements
the missing extraction delta in the external workspace, stages deterministic
synthetic/bounded **format** fixtures, and checks actual extraction/round-trip
outcomes. It must not perform a third full source scan, either inventory, or a
whole-game dry run. No-op/no-throw or self-echo checks are not format proof.
For text discovery, the exercised format fixtures must also distinguish display-bearing fields and actual display dependencies from control/editor/diagnostic storage and unclassified raw records. Assert the resulting eligible targets, relevant context and unchanged excluded bytes through the real extractor/application path. An unresolved actual display consumer reports pending SDK capability instead of promoting its entire raw container into translation. Preparation must not prove eligibility by copying the expected inventory, relabeling one discovered list as two methods, or merely attaching source/evidence hashes.
The parent never reads private source, fixtures, tasks, or evidence; it receives
validated categorical status, counts, and paths only.

Preparation receipts live at
`.game-l10n/data-plane/extractor_preparation/receipt.json`, independently of the
source-discovery receipt under `source_probe`. Status and native launch repair
use that same purpose-specific lifecycle. An ended failed or stale successful
preparation can be prepared again as a fresh attempt with the current requirements;
an operationally blocked native launch instead resumes its bound task after host
repair. Neither path overwrites discovery evidence or changes SDK selection.

Source-format discovery records `extractor_requirements` in `game-l10n.json`:
`required_capabilities` and `required_formats` are nonempty unique categorical
code lists, and `source_metadata` is a nonempty metadata object. Required
categories come from observed source/format metadata, not from whichever
extractors happen to exist. Existing requirements are bound into the prepared
task and cannot be narrowed by that worker. If previously absent, the worker
establishes them from bounded metadata before checking. A new requirement or
metadata change invalidates readiness, not the completed inventory history.

The SDK preparation helper stages `tools/extractor-preparation.json` with this
typed profile (all paths are workspace-relative):

```json
{
  "schema": "parr0t.extractor-preparation-profile.v1",
  "required_capabilities": ["ordered-strings", "unicode"],
  "required_formats": ["synthetic-format"],
  "source_metadata": {"engine": "synthetic", "version": "1"},
  "extractor_paths": ["tools/check.py", "tools/extractor.py"],
  "checks": [{
    "id": "synthetic-round-trip",
    "format": "synthetic-format",
    "capabilities": ["ordered-strings", "unicode"],
    "runner": "tools/check.py",
    "arguments": [],
    "cases": [{
      "id": "unicode-case",
      "input_path": "fixtures/input.bin",
      "expected_path": "fixtures/expected.json",
      "actual_path": ".game-l10n/preparation-output/actual.json"
    }]
  }]
}
```

Profile requirements/metadata must exactly match configuration; checks must
cover every required format and capability. Each runner is an implemented
`.py` file listed in `extractor_paths`. Inputs and fixed expected outputs exist
before execution and are content-bound. Actual outputs are distinct, unique
paths, removed by Core before each run. Keep generated outputs outside `tools/`
because the entire workspace tool manifest is also bound. Category/case/check
codes use `[a-z0-9][a-z0-9._-]*`. Every declared extractor, runner, fixture,
expected result, profile, selected SDK decision, activated runtime, tool
manifest and source/format requirement is bound into readiness.

Keep approved third-party dependency trees outside `tools/`, for example under
`.game-l10n/extractor-dependencies/`. The exportable tool manifest accepts source
files, not dependency binaries such as `.pyd`/`.dll`. Bind the actual dependency
location explicitly in runner arguments; staging metadata and preparation
profiles must not retain the old location after an authorized move. Isolated
`python -I` runners also need an explicit existing Core `src` dependency path
when they import `game_l10n`; ambient `PYTHONPATH` is not a substitute. These
paths do not authorize acquisition or execution of unapproved dependencies.

A standalone synthetic runner invocation is a smoke check, not an inventory
readiness receipt. After a profile/tool-path repair, obtain fresh readiness
through the bound Core preparation check; do not relabel earlier receipts.

After implementation, reactivate the **same** SDK selection if its bound tools
changed, then run inside that isolated worker:

```text
game-l10n check-extractor-preparation --workspace <workspace> --relay <bound-relay-path>
```

Core marks preparation pending before attempting checks and invokes each local
runner as the current Python `-I -B <runner> <arguments>` followed by
`--preparation-request <absolute-json-path> --preparation-result <absolute-json-path>`.
No dependencies are acquired and no ambient packages are added. A runner may
explicitly bind already-authorized dependency paths as part of its implemented
bytes and activated runtime. There are no new model calls or usage/time caps.

The request schema is `parr0t.extractor-preparation-request.v1`, with `task_id`,
`binding_fingerprint`, the complete declared `check`, and a content-derived
`request_id`. The runner writes a result with schema
`parr0t.extractor-preparation-check.v1`, that exact `request_id`, and `cases`
equal to the declared case descriptors. Core, not the runner's prose/status,
checks zero process exit, actual output bytes equal to every fixed expected
output, request/task binding, and unchanged implementation/fixture/activation
bytes. The resulting `parr0t.extractor-preparation.v1` attestation is admitted
only with its immutable prepared relay/task, current complete binding, and
unchanged request/result/output digests. Worker success is sealed through the
normal `seal-data-plane-task --worker-ended` control-envelope flow; the envelope
must bind the preparation attestation. Missing, failing, malformed, forged,
unbound, or stale proof remains pending and does not authorize either inventory.

`require_extractor_preparation(work_dir, config, activation)` is the deterministic
SDK/Core admission API. It raises `E_EXTRACTOR_PREPARATION_PENDING` on missing or
invalid required proof, otherwise returns only safe status/counts/path metadata.
Both SDK inventory commands and Core inventory task admission use it after
activation. `run-tool --stage unpack` likewise cannot bypass required proof.

Preparation proves extraction readiness only. It never writes inventory
attestations or sets static/runtime coverage complete. Two independent full
inventories and later reconciliation remain separate mandatory coverage gates.
Conservative compatibility preserves jobs that already have both inventories:
status continues their existing reconciliation/downstream path without
rewriting inventories, successful receipts, or checkpoints solely because an
older job lacks preparation evidence. Every **new** inventory dispatch still
requires current readiness for a deficient SDK, including after changed tools,
selection, requirements, metadata, fixtures, or output bytes.


## Two independent decisions

SDK selection has two axes that must never be collapsed:

1. **Implementation reuse:** whether code or a public format strategy from an existing SDK is useful.
2. **Compatibility ownership:** which `sdk-<engine>-<version>` branch may publicly claim the capability.

Reusing implementation does not transfer compatibility ownership. A tool proven on a new engine/version line must not be added to an older-version branch merely because that branch supplied useful code. Conversely, selecting a new target branch does not authorize ignoring a reusable SDK reference and rebuilding from scratch.

## Decision rules

- `reuse` is the default when the target branch identity and public contract both match. Use the branch implementation directly from an isolated checkout/worktree or registered portable snapshot; do not copy it into a new adapter.
- `extend` applies only when the existing branch has the same target engine/version identity and one bounded public capability is missing. Implement only that delta privately, validate against the existing adapter suite, and target that same branch after success and publication approval.
- `derive-new` applies when a different SDK identity contains reusable implementation or format knowledge but cannot own the target compatibility claim. Reuse that branch as a read-only implementation reference, validate only capabilities actually exercised on the target line, and prepare the new `sdk-<target-engine>-<target-version>` profile. Publication still creates the target branch from `main`; never merge the reference branch wholesale or broaden its version contract.
- `private-new` applies only when every inventoried candidate is incompatible and none is a reusable reference. It means a new target SDK identity, not permission to ignore related branches.
- `compatible` and `partial` are invalid evaluations for a branch whose identity differs from the recorded target. Use `reference` when implementation can be reused across identities, or `incompatible` when it cannot.
- If the exact target branch already exists, `derive-new` and `private-new` are invalid. Evaluate that target branch and either reuse or extend it.
- Every ordinary evaluation names at least one path from the candidate's reported `public_contract_paths`. This records what public contract evidence was inspected without placing target-game data in the repository. Only `uninspectable_contract` may omit it.
- `uninspectable_contract` is not a shortcut around authentication or stale refs. It keeps `private-new` blocked. First restore normal authenticated read access and refresh the inventory; if that is impossible, stop and report the limitation instead of silently rebuilding the adapter.

## Invalidation

The inventory binds every evaluation to the candidate commit, complete content fingerprint, and recorded target engine/version identity. Refreshing the inventory preserves an evaluation only when both commit and content fingerprint still match and clears the selection decision. A ref update, snapshot change, or target-identity change therefore requires a new matching evaluation and selection before data-plane work resumes.

Refresh inventory before preparing a `source_probe` task, not inside its bound discovery attempt. A non-success discovery receipt in the current control generation keeps status on discovery even if that attempt already wrote a selected SDK into configuration. Seal a successful continuation before advancing to implementation activation; receipts from earlier control generations do not block the current generation.

The gate does not prove target compatibility by itself. It makes the agent expose and record its public-format comparison before implementation, while the selected SDK's own discovery and deterministic validators remain authoritative.
