# Windows installer contract

This contract defines the public, player-facing installer produced by Parr0t. It is a distribution interface, not an agent harness. An SDK adapter may implement title- or engine-specific patch construction, but must not invent a different package layout, operation name, core error code, or player message for each job.

## Audience boundary

- A player who double-clicks `Install-Translation-Patch.bat` authorizes the selected operation by choosing it in the menu and accepting the final target/action confirmation. No Parr0t workspace, `AGENTS.md`, approval JSON, API key, development tool, or agent terminology may be required or shipped.
- An agent or script that invokes a mutating PowerShell mode noninteractively must supply the matching one-shot runtime approval. This is an automation boundary only; it must never be presented as a prerequisite for ordinary interactive installation.
- Read-only package validation and installed-state verification require no runtime approval.

## Fixed package layout

Every standard ZIP has exactly these framework-owned root entries plus the manifest-listed payload:

```text
Install-Translation-Patch.bat
Install-Translation-Patch.ps1
parr0t-patch-manifest.json
README.txt
payload/**
```

Do not add `AGENTS.md`, prompts, runtime approvals, `.game-l10n`, task state, logs, source game data, translation work files, SDK documentation, or build-host paths. Title-specific values belong only in the generated manifest, README, payload, and explicitly approved custom error registry.

## Required operations

The basic user menu and noninteractive interface support exactly three public operations:

1. **Install** — first install or update of the same patch ID.
2. **Restore** — remove newly added files and restore every explicitly declared original replacement from its verified backup.
3. **Verify installed state** — read the status marker, receipt, owned-file presence, and hashes without changing the game.

The bundled Korean labels for the three operations and the progress/failure states are normative for this contract and are validated against the shared template. A failure always shows what happened, a public code, a concrete next action, and whether any game file may have changed. Raw exceptions and internal codes appear only with the explicit diagnostic switch.

## Patch identity and status file

The game directory contains one framework-owned marker named `parr0t-patch-status.txt`:

```text
contract=parr0t.patch-status/v1
patch_id=<stable-patch-id>
release_id=<installed-release-id>
```

This marker is the first authority for whether a Parr0t patch is installed and which patch ID owns the state. Detailed owned paths and hashes remain in `.parr0t/<patch-id>.json`. Install/update writes both records; Restore removes both. A missing, malformed, or disagreeing pair is reported as damaged installed state. A different `patch_id` stops before mutation and instructs the player to restore that patch with its matching installer.

`parr0t-patch-status.txt` and `.parr0t/` are reserved and cannot be payload paths.

## Declared original-file replacement

An overlay payload normally refuses every unowned collision. If an adapter must replace an original file, such as putting a launcher wrapper at the original executable path, it must declare that exception in the private package spec:

```json
{
  "replace_files": [
    {
      "path": "fixture-launcher.exe",
      "backup_path": "fixture-launcher.parr0t-original.exe"
    }
  ]
}
```

The replacement target must exist in the payload and in the game before first install. The backup path must differ, must not be a payload path, and must not already exist. When `backup_path` is omitted, the generator uses `.parr0t/backups/<patch-id>/<path>`.

Before the first mutation, the installer copies the original to the declared backup and records the original path, backup path, size, and hash in the receipt. Verify checks both the installed replacement and original backup. Update is allowed only when the patch ID and replacement set are unchanged and both installed and backup hashes still match; it reuses the first-install backup rather than backing up the wrapper as though it were the original. Restore replaces each target through a temporary file, removes the verified backup, removes overlay-owned files, and then removes the receipt and status marker. Any disagreement stops before mutation with `PARR0T-E012`.

Do not declare an ordinary new mod file as a replacement, synthesize an original, silently adopt an existing backup, or change the replacement/backup paths during an update. Restore the prior release first when the replacement set must change.

Executable wrappers additionally follow `docs/wrapper-executable.md`. They use approved architecture-specific wrapper bytes and the adjacent `<name>.parr0t-original.exe` convention; per-game ad hoc binaries and `.lnk` substitutions are not accepted.

## Core result codes

Core codes and meanings are fixed by `parr0t.windows-installer/v1`:

| Code | Meaning |
| --- | --- |
| `PARR0T-S001` | Installation completed |
| `PARR0T-S002` | Update completed |
| `PARR0T-S003` | Restore completed |
| `PARR0T-S004` | Installed state verified |
| `PARR0T-E001` | Package is damaged or invalid |
| `PARR0T-E002` | Game directory is invalid |
| `PARR0T-E005` | Noninteractive mutation lacks automation approval |
| `PARR0T-E006` | Automation approval does not match the operation |
| `PARR0T-E007` | An unowned file collision prevents installation |
| `PARR0T-E008` | This patch is not installed |
| `PARR0T-E009` | Status, receipt, or installed files are damaged or changed |
| `PARR0T-E010` | The status file belongs to another patch ID |
| `PARR0T-E011` | Menu input is invalid or the user cancelled |
| `PARR0T-E012` | Declared original replacement or backup state does not match |
| `PARR0T-E099` | Unexpected installer failure |

Agents must use these meanings and the shared installer generator; they must not allocate ad hoc core codes or expose internal exception strings as the player interface.

## SDK- and title-specific errors

Specific adapters may need additional compatibility or dependency failures. Put them in the package spec's `custom_errors` list instead of changing a core meaning:

```json
{
  "internal_code": "E_CUSTOM_FIXTURE_BINDING",
  "public_code": "PARR0T-FIXTURE-E001",
  "message": "This patch does not match the installed build.",
  "resolution": "Use the patch release that lists this build as supported."
}
```

Rules:

- Internal names use `E_CUSTOM_<NAME>`.
- Public names use `PARR0T-<SCOPE>-E###`; they cannot reuse `PARR0T-E###`.
- The scope is a stable SDK or release identifier, not a private workspace path or job history.
- Message and resolution are short, single-line, player-facing text. They must say what the player can do and must not mention agent policy.
- Custom entries are data in the fingerprinted manifest. They cannot override core failures and may be emitted only for a corresponding deterministic adapter check.

Changing this layout, operation set, marker format, or core registry requires a new installer contract version and matching tests. A per-game task is not permission to redesign the contract.
