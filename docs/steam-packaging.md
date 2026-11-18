# Steam delivery packaging

Parr0t can wrap an already prepared patch payload in a minimal Steam-aware installer. This feature belongs to the engine-neutral delivery layer: it does not extract text, interpret assets, compile runtime hooks, or build an engine payload.

## Private workspace inputs

Place the completed files under a payload directory in the external workspace. Create a JSON spec beside it:

```json
{
  "patch_id": "fixture-localization",
  "release_id": "r1",
  "display_name": "Fixture localization patch",
  "checkpoint_file": "release-checkpoint.json",
  "replace_files": [
    {
      "path": "fixture-launcher.exe",
      "backup_path": "fixture-launcher.parr0t-original.exe"
    }
  ],
  "custom_errors": [
    {
      "internal_code": "E_CUSTOM_FIXTURE_BINDING",
      "public_code": "PARR0T-FIXTURE-E001",
      "message": "This patch does not match the installed build.",
      "resolution": "Use the release that lists this build as supported."
    }
  ],
  "steam": {
    "app_id": "123456",
    "install_dir": "Fixture Game",
    "required_files": ["fixture-game.exe"],
    "source_bindings": [
      {
        "path": "fixture-launcher.exe",
        "bytes": 18,
        "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "error_code": "E_CUSTOM_FIXTURE_BINDING"
      }
    ]
  }
}
```

`patch_id` is the stable status/receipt identity. `release_id` changes when a new package replaces the previous release. Published packages supply `checkpoint_file`, resolved beside or below the private spec, and must satisfy `docs/release-checkpoint.md` with matching patch/release IDs. Local-only packages instead may supply `local_checkpoint` when workspace configuration sets `release_checkpoint.required_for_distribution` to `false`; its generated checkpoint is canonicalized into the ZIP as `parr0t-release-checkpoint.json`. The checkpoint is package metadata and is not copied into the game. `required_files` is optional and validates a manually selected game root. `source_bindings` is optional and binds supported original byte counts and hashes to registered custom errors; omission is equivalent to `[]` for v4 compatibility. `replace_files` is optional and is reserved for payload paths that must replace and later restore an original game file; omit `backup_path` to use the private `.parr0t/backups/<patch-id>/...` default. `custom_errors` is optional and uses the isolated `PARR0T-<SCOPE>-E###` namespace. Real identifiers and payload paths stay in the private workspace and generated ZIP; they must not be committed to Parr0t.

For local-only packaging, set workspace configuration `release_checkpoint.required_for_distribution` to `false` and omit `checkpoint_file`. The spec may instead contain:

```json
{
  "local_checkpoint": {
    "inventory": "inventory.json",
    "results": "translation-results.json",
    "ownership": {
      "owner": "Authorized owner",
      "license": "License identifier"
    }
  }
}
```

The inventory/results paths must resolve to existing workspace files; ownership requires explicit nonempty values. The packager validates these inputs and generates the local integrity checkpoint and evidence into a private immutable spec. `local_checkpoint` and `checkpoint_file` are mutually exclusive. This local-only mode does not qualify the package for publication.

## Build and verify

```text
game-l10n steam-package --workspace <external-workspace> --payload <payload-directory> --spec <steam-spec.json> --output <patch.zip>
game-l10n verify-steam-package --workspace <external-workspace> --package <patch.zip>
game-l10n inspect-installed-state --workspace <external-workspace> --target <configured-game-root>
```

The ZIP is deterministic for identical inputs. It contains only the fixed files, release checkpoint, and payload defined by `docs/installer-contract.md`; agent harness files and workspace state are forbidden. The package validator checks the exact entry set, installer contract, result-code registry, sizes, artifact fingerprint, checkpoint schema/binding/digest, and payload digests.

`inspect-installed-state` is a separate read-only diagnostic. It hashes only receipt-owned installed files and preserved original backups. It reports `not_installed`, `installed_intact`, `possible_game_update`, or `damaged_state`; it neither installs nor repairs anything. `possible_game_update` means recorded patched bytes changed or disappeared and may also reflect a user or another tool, so revalidate before attributing the cause. A missing or changed preserved-original backup is `damaged_state` and blocks further mutation until recovery is resolved.

## Installer behavior

The installer:

- reads Steam registry roots and `libraryfolders.vdf`;
- skips missing or stale library roots before joining child paths;
- requires the matching app manifest for automatic discovery;
- asks for a game directory when automatic discovery fails;
- verifies package payload hashes once before mutation;
- accepts a player's interactive menu selection and final confirmation without an agent approval file;
- denies noninteractive agent/script install, update, and restore without an exact unexpired one-shot runtime approval;
- refuses collisions with files not owned by its receipt;
- rejects an unsupported original build through the source binding's registered custom error before mutation;
- backs up only explicitly declared original replacements and binds their paths, sizes, and hashes to the receipt;
- checks a replaced file's source binding against its first-install backup during update and Verify, while installed patch hashes remain receipt-owned checks;
- reuses the verified original backup on update instead of mistaking the installed wrapper for the original;
- removes or restores only paths listed by its prior receipt;
- writes `parr0t-patch-status.txt` as the primary patch-ID marker and a matching detailed receipt;
- supports the public operations Install, Restore, and Verify installed state.

For an ordinary player, run the batch file, select the operation, review the target/action confirmation, and continue. `Verify` is read-only. When an agent invokes the PowerShell modes noninteractively, the user must explicitly request the exact action and the agent records it with `game-l10n approve-runtime`; pass the resulting file with `-ApprovalFile`. A first `Install` requires `install`, an existing status/receipt makes the same mode require `update`, and public `Restore` consumes the internal `remove` approval. Package creation and verification do not authorize automation. See `docs/runtime-approval.md` and `docs/installer-contract.md`.

It deliberately does not embed the previous package, back up the complete game, fingerprint every Steam-managed file, or implement arbitrary legacy migration. If a title needs a special migration, keep it in the private workspace until it proves reusable and receives separate publication approval.

Steam per-account launch options and `localconfig.vdf` are forbidden targets. Prefer an overlay or declared wrapper replacement and never require or terminate Steam. A wrapper package must use the exact approved binary, signature, hashes, adjacent original naming, and source/build evidence defined by `docs/wrapper-executable.md`.

## Acceptance checks

For an unchanged payload, one package verification is sufficient. When installer logic changes, run one synthetic install/update/verify/restore lifecycle in a temporary directory, including one declared original replacement when that layer changed. A real agent-driven game install, update, restore, or launch is a separately approved step and must not be inferred or repeated after unrelated documentation or packaging changes.
