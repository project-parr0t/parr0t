# Launcher-neutral filesystem package

`parr0t.filesystem.patch/v1` is the standard Windows distribution format for a game that should not use Steam discovery. It shares the fixed `parr0t.windows-installer/v2` lifecycle with Steam packages but never searches or changes a launcher, store account, launch option, shortcut, or client process.

## Build

Keep the completed payload, package spec, checkpoint, and ZIP in the private game workspace. A minimal spec is:

```json
{
  "patch_id": "synthetic-localization",
  "release_id": "r1",
  "display_name": "Synthetic localization patch",
  "checkpoint_file": "parr0t-release-checkpoint.json",
  "target": {
    "required_files": ["synthetic-game.exe"],
    "source_bindings": []
  },
  "replace_files": [],
  "custom_errors": []
}
```

Build and verify with:

```text
game-l10n filesystem-package --workspace <workspace> --payload <payload> --spec <spec.json> --output <patch.zip>
game-l10n verify-filesystem-package --workspace <workspace> --package <patch.zip>
```

The generated ZIP layout is fixed:

```text
Install-Translation-Patch.bat
Install-Translation-Patch.ps1
parr0t-patch-manifest.json
parr0t-release-checkpoint.json
README.txt
payload/...
```

## Runtime behavior

The player selects Install/Update, Restore, or Verify installed state. At startup the installer captures its current working directory. `-GameRoot` accepts an absolute path or a path relative to that captured directory. When `-GameRoot` is omitted, the installer first treats the captured current directory as the game folder; if its target markers do not match, the installer asks for an absolute or relative path or accepts a folder dragged into the window. `target.required_files` performs a presence check. Optional `target.source_bindings` use the same path/size/SHA-256/custom-error structure and original-backup rules as Steam source bindings.

The installer:

- manages only manifest and receipt-owned files;
- identifies ownership through `parr0t-patch-status.txt` and `.parr0t/<patch-id>.json`;
- refuses an unowned collision;
- preserves every declared original replacement once and restores it;
- requires the same one-shot runtime approval for noninteractive mutation;
- never launches the game or modifies a store/launcher.

Building or validating the ZIP is read-only with respect to the game. Installing, updating, or restoring it remains a separately authorized runtime action. An engine SDK may construct the payload, but it may not invent another public installer shape or per-game lifecycle.
