# SDK discovery and reuse gate

This gate prevents an agent from rebuilding an engine adapter that Parr0t already has. It runs before extraction code is written and before any translation stage. The evidence and decision are stored only in the external workspace.

## Required order

1. Start from `main` and make the authenticated remote-tracking refs current when network access is available. A missing local checkout is not evidence that a branch does not exist.
2. Initialize the external workspace. Initialization records all visible local and remote-tracking `sdk-*` refs without checking them out.
3. Run `game-l10n sdk-inventory --workspace <workspace> --refresh-remote` when authenticated network access is available. This fetches only `sdk-*` tracking refs from `origin`; omit the flag for an explicitly offline run. Use `--remote <name>` when the authoritative remote has another name.
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
# Run this from the exact selected SDK checkout for reuse/extend.
game-l10n sdk-activate --workspace <workspace>

# For derive-new, run from main after replacing both generated workspace tools,
# and bind the exact clean reference checkout as well.
game-l10n sdk-activate --workspace <workspace> --reference-checkout <reference-worktree>

# For private-new, run from main after replacing both generated workspace tools.
game-l10n sdk-activate --workspace <workspace>
```

`reuse` requires a clean runtime at the exact inventoried commit. `extend` requires a runtime descended from the selected same-identity commit. `derive-new` binds both the new workspace adapter and the exact reference commit. `private-new` binds the new workspace adapter. Generated pass-through tools never count as an adapter. The activation record fingerprints the executing checkout and, when applicable, the workspace tool manifest; changing either invalidates it.

Extraction, context analysis, translation, packing, and SDK promotion remain blocked until both selection and activation are valid. For `reuse`/`extend`, use the selected branch's engine commands; the generic workspace `run-tool` command is reserved for a bound `derive-new` or `private-new` adapter.

## Two independent decisions

SDK selection has two axes that must never be collapsed:

1. **Implementation reuse:** whether code or a public format strategy from an existing SDK is useful.
2. **Compatibility ownership:** which `sdk-<engine>-<version>` branch may publicly claim the capability.

Reusing implementation does not transfer compatibility ownership. A tool proven on a new engine/version line must not be added to an older-version branch merely because that branch supplied useful code. Conversely, selecting a new target branch does not authorize ignoring a reusable SDK reference and rebuilding from scratch.

## Decision rules

- `reuse` is the default when the target branch identity and public contract both match. Use the branch implementation directly from an isolated checkout or worktree; do not copy it into a new adapter.
- `extend` applies only when the existing branch has the same target engine/version identity and one bounded public capability is missing. Implement only that delta privately, validate against the existing adapter suite, and target that same branch after success and publication approval.
- `derive-new` applies when a different SDK identity contains reusable implementation or format knowledge but cannot own the target compatibility claim. Reuse that branch as a read-only implementation reference, validate only capabilities actually exercised on the target line, and prepare the new `sdk-<target-engine>-<target-version>` profile. Publication still creates the target branch from `main`; never merge the reference branch wholesale or broaden its version contract.
- `private-new` applies only when every inventoried candidate is incompatible and none is a reusable reference. It means a new target SDK identity, not permission to ignore related branches.
- `compatible` and `partial` are invalid evaluations for a branch whose identity differs from the recorded target. Use `reference` when implementation can be reused across identities, or `incompatible` when it cannot.
- If the exact target branch already exists, `derive-new` and `private-new` are invalid. Evaluate that target branch and either reuse or extend it.
- Every ordinary evaluation names at least one path from the candidate's reported `public_contract_paths`. This records what public contract evidence was inspected without placing target-game data in the repository. Only `uninspectable_contract` may omit it.
- `uninspectable_contract` is not a shortcut around authentication or stale refs. It keeps `private-new` blocked. First restore normal authenticated read access and refresh the inventory; if that is impossible, stop and report the limitation instead of silently rebuilding the adapter.

## Invalidation

The inventory binds every evaluation to the candidate commit and the recorded target engine/version identity. Refreshing the inventory preserves an evaluation only when that branch still resolves to the same commit and clears the selection decision. A ref update or target-identity change therefore requires a new matching evaluation and selection before data-plane work resumes.

The gate does not prove target compatibility by itself. It makes the agent expose and record its public-format comparison before implementation, while the selected SDK's own discovery and deterministic validators remain authoritative.
