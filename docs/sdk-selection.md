# SDK discovery and reuse gate

This gate prevents an agent from rebuilding an engine adapter that Parr0t already has. It runs before extraction code is written and before any translation stage. The evidence and decision are stored only in the external workspace.

## Required order

1. Start from `main` and make the authenticated remote-tracking refs current when network access is available. A missing local checkout is not evidence that a branch does not exist.
2. Initialize the external workspace. Initialization records all visible local and remote-tracking `sdk-*` refs without checking them out.
3. Run `game-l10n sdk-inventory --workspace <workspace> --refresh-remote` when authenticated network access is available. This fetches only `sdk-*` tracking refs from `origin`; omit the flag for an explicitly offline run. Use `--remote <name>` when the authoritative remote has another name.
4. Inspect each plausible candidate through its public documentation and the `public_contract_paths` reported by the inventory. Use `git show <inspect-ref>:<path>` or a separate read-only worktree; do not switch a dirty checkout and do not inspect private game data.
5. Compare public format signatures, engine family, serialized layout, supported versions, extraction/application/packing behavior, integrity constraints, and required runtime hooks. Branch-name similarity alone is neither acceptance nor rejection evidence.
6. Record one categorical evaluation per candidate:

```text
game-l10n sdk-evaluate --workspace <workspace> --branch sdk-<engine>-<version> --result compatible --reason public_contract_match --evidence-path <inspected-public-path>
game-l10n sdk-evaluate --workspace <workspace> --branch sdk-<engine>-<version> --result partial --reason reusable_with_extension --evidence-path <inspected-public-path>
game-l10n sdk-evaluate --workspace <workspace> --branch sdk-<engine>-<version> --result incompatible --reason <fixed-reason> --evidence-path <inspected-public-path>
```

7. Record exactly one decision:

```text
game-l10n sdk-select --workspace <workspace> --mode reuse --branch sdk-<engine>-<version>
game-l10n sdk-select --workspace <workspace> --mode extend --branch sdk-<engine>-<version>
game-l10n sdk-select --workspace <workspace> --mode private-new
```

Extraction, context analysis, translation, and new SDK promotion remain blocked until the decision is valid for the current inventory fingerprint.

## Decision rules

- `reuse` is the default when the public contract matches. Use the branch implementation directly from an isolated checkout or worktree; do not copy it into a new adapter.
- `extend` applies when the existing adapter is a sound base but one bounded public capability is missing. Implement only that delta privately, validate against the existing adapter suite, and target the same SDK branch after success and publication approval.
- `private-new` applies only when every inventoried candidate has an `incompatible` evaluation. The CLI rejects this decision while any candidate is unevaluated, compatible, or extensible.
- Every ordinary evaluation names at least one path from the candidate's reported `public_contract_paths`. This records what public contract evidence was inspected without placing target-game data in the repository. Only `uninspectable_contract` may omit it.
- `uninspectable_contract` is not a shortcut around authentication or stale refs. It keeps `private-new` blocked. First restore normal authenticated read access and refresh the inventory; if that is impossible, stop and report the limitation instead of silently rebuilding the adapter.

## Invalidation

The inventory binds every evaluation to the candidate commit. Refreshing the inventory preserves an evaluation only when that branch still resolves to the same commit and clears the selection decision. Any ref update therefore requires a new selection before data-plane work resumes.

The gate does not prove target compatibility by itself. It makes the agent expose and record its public-format comparison before implementation, while the selected SDK's own discovery and deterministic validators remain authoritative.
