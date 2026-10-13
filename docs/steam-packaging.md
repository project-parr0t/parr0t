# Steam delivery packaging

Parr0t can wrap an already prepared patch payload in a minimal Steam-aware installer. This feature belongs to the engine-neutral delivery layer: it does not extract text, interpret assets, compile runtime hooks, or build an engine payload.

## Private workspace inputs

Place the completed files under a payload directory in the external workspace. Create a JSON spec beside it:

```json
{
  "patch_id": "fixture-localization",
  "release_id": "r1",
  "display_name": "Fixture localization patch",
  "steam": {
    "app_id": "123456",
    "install_dir": "Fixture Game",
    "required_files": ["fixture-game.exe"]
  }
}
```

`patch_id` is the stable receipt identity. `release_id` changes when a new package replaces the previous release. `required_files` is optional and validates a manually selected game root. Real identifiers and payload paths stay in the private workspace and generated ZIP; they must not be committed to Parr0t.

## Build and verify

```text
game-l10n steam-package --workspace <external-workspace> --payload <payload-directory> --spec <steam-spec.json> --output <patch.zip>
game-l10n verify-steam-package --workspace <external-workspace> --package <patch.zip>
```

The ZIP is deterministic for identical inputs. It contains a payload manifest, a PowerShell installer, a double-click batch launcher, a short README, and the payload. The package validator checks the exact entry set, sizes, artifact fingerprint, and payload digests.

## Installer behavior

The installer:

- reads Steam registry roots and `libraryfolders.vdf`;
- skips missing or stale library roots before joining child paths;
- requires the matching app manifest for automatic discovery;
- asks for a game directory when automatic discovery fails;
- verifies package payload hashes once before mutation;
- denies install, update, and remove without an exact unexpired one-shot runtime approval;
- refuses collisions with files not owned by its receipt;
- removes only paths listed by its prior receipt when updating;
- writes one receipt with the stable patch ID, release ID, owned paths, and hashes;
- supports install/update, remove, and installed-file verification.

`Verify` is read-only and needs no runtime approval. Before a real mutation, the user must explicitly request the exact action and the agent records it with `game-l10n approve-runtime`; pass the resulting file with `-ApprovalFile`. A first install requires `install`, an existing receipt makes the same installer mode require `update`, and removal requires `remove`. Package creation and verification do not authorize any of them. See `docs/runtime-approval.md`.

It deliberately does not embed the previous package, back up the complete game, fingerprint every Steam-managed file, or implement arbitrary legacy migration. If a title needs a special migration, keep it in the private workspace until it proves reusable and receives separate publication approval.

## Acceptance checks

For an unchanged payload, one package verification is sufficient. When installer logic changes, run one synthetic install/update/verify/remove lifecycle in a temporary directory. A real game install, update, removal, or launch is a separately approved step and must not be inferred or repeated after unrelated documentation or packaging changes.
