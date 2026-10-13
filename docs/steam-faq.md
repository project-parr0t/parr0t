# Steam packaging and installation FAQ

This FAQ defines the Steam-specific boundary for agents and adapters. It supplements the engine-neutral installer contract; it does not authorize a game task, installation, client shutdown, launch, or public release by itself.

## What may a standard installer inspect?

Read-only discovery may inspect only the metadata needed to identify a target:

- Steam install paths in the current-machine/current-user registry;
- `steamapps/libraryfolders.vdf` for registered library roots;
- `steamapps/appmanifest_<app-id>.acf` for the matching application and `installdir`;
- manifest-declared binding or required files inside the resulting game root;
- `parr0t-patch-status.txt`, the matching receipt, and receipt-owned files for installed-state verification.

Skip missing library roots and continue. Do not recursively inventory unrelated libraries, games, saves, Workshop content, screenshots, cloud data, or every user account. The App ID and expected install directory come from the private title spec, not a guess based on folder names.

## How should an agent find the installed game?

Use this order:

1. Resolve each registered Steam root from the registry.
2. Parse `libraryfolders.vdf` and keep only existing library directories.
3. In each library, require `appmanifest_<app-id>.acf` and read its `installdir`.
4. Construct `steamapps/common/<installdir>` and validate every manifest-declared required/binding file.
5. Accept an automatic result only when exactly one validated directory remains.
6. Otherwise ask the player to select or paste the game directory, then apply the same binding checks. Never choose the nearest-looking folder.

The store page, the user's stated title, or existing public project metadata may help an agent identify an App ID, but the installed app manifest and binding checks remain the local authority.

## What may a standard installer change?

After a player confirms the displayed target/action, the standard interactive installer may:

- create or update manifest-listed overlay files inside the validated game root;
- replace only original files explicitly listed in `replace_files`, after preserving and hashing their declared backups;
- write the game-root status marker and matching receipt;
- Restore only receipt-owned overlays and recorded originals.

A noninteractive agent/script needs the matching runtime approval for the same mutation. Neither route authorizes launching the game, changing saves, deleting logs, stopping Steam, editing Workshop content, or modifying another account.

In most cases, prefer a normal overlay. If Steam must start a wrapper at the original executable path, prefer a declared original-file replacement governed by `docs/installer-contract.md`. This keeps discovery, update, verification, and Restore under one game-root receipt without touching Steam's per-account configuration.

## May an installer edit Steam launch options?

No. A Parr0t package must not read or write `localconfig.vdf`, inject Steam launch options, or change other per-account Steam client configuration. This prohibition applies to the standard installer and every SDK/title adapter. It prevents client-cache races, account ambiguity, unrelated-setting damage, and any fabricated requirement to shut down Steam.

Steam Play-button integration uses a reversible executable wrapper inside the game root instead. Follow `docs/wrapper-executable.md`; do not generate a fresh opaque binary for each title:

1. Identify every supported main executable explicitly in the private title spec and validate it against the supported build. Do not select executables by a broad glob.
2. Select the exact approved Parr0t wrapper release for the target architecture. A filesystem shortcut (`.lnk`) is not an executable replacement and is not used for this purpose.
3. Put the launcher payload at the original executable path and declare that path plus the preserved-original path in `replace_files`.
4. On first install, preserve and hash the real original. On update, reuse that same backup. On Restore, put that exact original back.
5. The wrapper invokes only the adjacent preserved original, prevents self-recursion, preserves working-directory behavior, forwards command-line arguments, inherits the environment, and returns the child exit code.
6. Validate wrapper selection, argument forwarding, exit-code forwarding, update, and Restore in a synthetic copy. Do not install into or launch the real game merely to test the wrapper.

Multiple supported main executables use multiple explicit replacement records. If a build cannot be wrapped without changing Steam client configuration, report that limitation; do not fall back to launch-option editing.

## How can a player or antivirus trust the wrapper EXE?

Do not create a new wrapper binary per game. Use the same minimal, public, versioned binary for each architecture, Authenticode-signed by one consistent verified publisher. Renaming those exact bytes to a declared main-executable name preserves the signature and file hash, so both users and Windows reputation systems see the same reviewed artifact instead of a never-before-seen build.

The release must link the exact source and reproducible build recipe, publish unsigned and signed SHA-256 values, identify the signer and timestamp, and explain the wrapper's limited behavior. Initial SmartScreen warnings can still occur for a new signed publisher/file; never promise otherwise and never tell players to disable protection. Follow the complete generation, signing, reputation, player-verification, and false-positive procedure in `docs/wrapper-executable.md`.

## Must Steam be closed for a patch?

No. Steam must remain outside the installer's process-control scope. The installer must not require, request, close, kill, or restart the Steam client. Read-only Steam metadata discovery works while the client is open, and Parr0t does not edit the client-cached configuration that previously motivated a shutdown requirement.

The target game itself must be closed before replacing a loaded executable or another locked game file. Ask the player to close it and retry. A title adapter may close only the validated target game process after a separate, explicit player confirmation; it must match the executable inside the selected game root and must never broaden that action to Steam, helper services, unrelated games, or similarly named processes. The generic installer does not terminate processes automatically.

## What if Steam or the game updates after patch installation?

Run Verify first. The status marker identifies the patch; the receipt distinguishes overlay files, installed replacements, and preserved originals.

- If an owned overlay changed or disappeared, report damaged installed state and offer reinstall only after confirmation.
- If a replaced target or its original backup changed, stop with the original-replacement state error. Do not rotate the backup or treat the new file as the original.
- If the supported game binding changed, use a scoped adapter error and require a compatible patch release.
- If the adapter must change its replacement set or integration method, Restore the prior release before a fresh install.

Steam's file verification may remove overlays or restore Steam-managed originals. That is not permission for an agent to reinstall automatically; the player chooses the next action.

## Which actions remain forbidden without another request?

- launching the game or generated code to “check” the patch;
- requiring, closing, killing, or restarting Steam;
- reading or editing `localconfig.vdf`, Steam launch options, or other per-account client configuration;
- modifying all Steam accounts when one account is ambiguous;
- writing to a guessed game directory or unsupported build;
- disabling updates, cloud sync, integrity checks, or platform security;
- copying agent harness files, approvals, logs, account identifiers, or user configuration into the release.

For general installer messages, codes, status, replacement, and Restore rules, see `docs/installer-contract.md`. For noninteractive agent execution, see `docs/runtime-approval.md`.
