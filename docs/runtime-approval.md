# Runtime authorization boundary

Parr0t denies real game installation and execution by default. Read access to a game directory authorizes discovery and extraction only. Building, fixing, packing, validating, inspecting logs, or asking whether a patch works does not authorize a write to the game directory or execution of the game or generated code.

## Separate states

```text
artifact_built
  -> offline_validated
  -> awaiting_install_approval
  -> installed
  -> awaiting_launch_approval
  -> runtime_validated
```

Each arrow into a real external effect performed by an agent or noninteractive script requires the user's explicit instruction for that action. `install`, `update`, `remove`, and `launch` are four different automation permissions. Installation does not imply launch; launch does not imply cleanup, log deletion, save changes, or patch mutation. Repeated exact actions are permitted only when explicitly included in bounded workflow reuse. This record is not a player-facing license prompt: a player running the packaged interactive menu authorizes Install or Restore through its final target/action confirmation.

## Approval record

After the user requests an exact action, optionally including bounded repeated use, the agent records it without executing anything:

```text
game-l10n approve-runtime --workspace <external-workspace> --action <install|update|remove|launch> --artifact <artifact-inside-workspace> --target <configured-game-root> --user-confirmed
```

For `launch`, also pass `--executable <exact-executable-inside-game-root>`. The command produces a private approval JSON under `.game-l10n/runtime-approvals/` and reports `recorded_not_executed`.

The record is bound to:

- one action;
- the exact artifact path and content digest;
- the exact configured game root;
- the exact executable for launch;
- an explicit expiry, 30 minutes by default (`--ttl-minutes <positive-minutes>` may record a suitable approved duration without an arbitrary one-day maximum);
- one use by default, or repeated exact use only with explicit user authorization recorded through `--workflow-reuse`.

A changed build, different path, different action, different executable, expiry, or consumed one-shot record invalidates the approval. The matching SDK operation validates it immediately before the external effect and consumes one-shot approvals. A failed action does not justify broadening or minting consent: an unchanged, unexpired reusable record can authorize the requested retry; a consumed one-shot record needs new explicit authorization.

## Steam packages

The generated Steam installer permits read-only `Verify` without an approval. Interactive player operation needs only the installer's final confirmation. For noninteractive automation, `Install` determines whether the operation is a first install or update and requires the matching approval; public `Restore` requires the internal `remove` approval. Pass the record explicitly:

```text
powershell -File <extracted-package>/Install-Translation-Patch.ps1 -Mode Install -GameRoot <configured-game-root> -ApprovalFile <approval-json>
powershell -File <extracted-package>/Install-Translation-Patch.ps1 -Mode Restore -GameRoot <configured-game-root> -ApprovalFile <approval-json>
```

The installer recomputes its manifest/payload fingerprint, checks the exact target, action, expiry, and use policy, consumes a one-shot record, and only then mutates receipt-owned paths. A reusable record remains limited to its exact approved action and unchanged artifact. Its agent-harness approval JSON stays in the private workspace and is never included in the player ZIP. See `docs/installer-contract.md`.

Approval JSON accepts UTF-8 with or without a BOM, including consumed receipts rewritten by Windows PowerShell.

## Engine SDK contract

An engine branch that adds real install or launch behavior must call `require_runtime_approval(..., consume=True)` immediately before the write or process start. It must not provide a second unguarded path, invoke the installer or executable directly, or translate another approval type into the requested action. Offline builders and validators remain inside the external workspace and do not need runtime approval.

The default runtime-validation handoff is still user execution: give the user the recoverable artifact and request only authorized metadata or diagnostic evidence. An agent may run the real game only after an explicit launch instruction and exact launch approval. Source/log exposure still follows control-plane recovery rules.

This SDK guard cannot cryptographically prove that a chat message came from a human, and a shell-capable agent could bypass a library through operating-system commands. Repository and workspace instructions therefore prohibit direct copies, installers, process launches, or cleanup outside the guard. Optional host confirmation can add assurance; an explicit current user request and bound approval do not require a second mandatory interactive dialog. Framework lifecycle consent never grants game installation or execution permission.
