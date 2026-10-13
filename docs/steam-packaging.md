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
    "required_files": ["fixture-game.exe"]
  }
}
```

`patch_id` is the stable status/receipt identity. `release_id` changes when a new package replaces the previous release. `checkpoint_file` is required, is resolved beside or below the private spec, and must satisfy `docs/release-checkpoint.md` with matching patch/release IDs. The builder canonicalizes it and places it at ZIP root as `parr0t-release-checkpoint.json`; it is release metadata and is not copied into the game. `required_files` is optional and validates a manually selected game root. `replace_files` is optional and is reserved for payload paths that must replace and later restore an original game file; omit `backup_path` to use the private `.parr0t/backups/<patch-id>/...` default. `custom_errors` is optional and uses the isolated `PARR0T-<SCOPE>-E###` namespace. Real identifiers and payload paths stay in the private workspace and generated ZIP; they must not be committed to Parr0t.

## Build and verify

```text
game-l10n steam-package --workspace <external-workspace> --payload <payload-directory> --spec <steam-spec.json> --output <patch.zip>
game-l10n verify-steam-package --workspace <external-workspace> --package <patch.zip>
```

The ZIP is deterministic for identical inputs. It contains only the fixed files, release checkpoint, and payload defined by `docs/installer-contract.md`; agent harness files and workspace state are forbidden. The package validator checks the exact entry set, installer contract, result-code registry, sizes, artifact fingerprint, checkpoint schema/binding/digest, and payload digests.

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
- backs up only explicitly declared original replacements and binds their paths, sizes, and hashes to the receipt;
- reuses the verified original backup on update instead of mistaking the installed wrapper for the original;
- removes or restores only paths listed by its prior receipt;
- writes `parr0t-patch-status.txt` as the primary patch-ID marker and a matching detailed receipt;
- supports the public operations Install, Restore, and Verify installed state.

For an ordinary player, run the batch file, select the operation, review the target/action confirmation, and continue. `Verify` is read-only. When an agent invokes the PowerShell modes noninteractively, the user must explicitly request the exact action and the agent records it with `game-l10n approve-runtime`; pass the resulting file with `-ApprovalFile`. A first `Install` requires `install`, an existing status/receipt makes the same mode require `update`, and public `Restore` consumes the internal `remove` approval. Package creation and verification do not authorize automation. See `docs/runtime-approval.md` and `docs/installer-contract.md`.

It deliberately does not embed the previous package, back up the complete game, fingerprint every Steam-managed file, or implement arbitrary legacy migration. If a title needs a special migration, keep it in the private workspace until it proves reusable and receives separate publication approval.

Steam per-account launch options and `localconfig.vdf` are forbidden targets. Prefer an overlay or declared wrapper replacement and never require or terminate Steam. A wrapper package must use the exact approved binary, signature, hashes, adjacent original naming, and source/build evidence defined by `docs/wrapper-executable.md`.

## Acceptance checks

For an unchanged payload, one package verification is sufficient. When installer logic changes, run one synthetic install/update/verify/restore lifecycle in a temporary directory, including one declared original replacement when that layer changed. A real agent-driven game install, update, restore, or launch is a separately approved step and must not be inferred or repeated after unrelated documentation or packaging changes.
