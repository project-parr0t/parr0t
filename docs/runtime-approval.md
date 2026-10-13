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

Each arrow into a real external effect requires the user's explicit instruction for that action. `install`, `update`, `remove`, and `launch` are four different permissions. Installation does not imply launch; launch does not imply cleanup, log deletion, save changes, another launch, or any patch mutation.

## Approval record

After the user requests one exact action, the agent records it without executing anything:

```text
game-l10n approve-runtime --workspace <external-workspace> --action <install|update|remove|launch> --artifact <artifact-inside-workspace> --target <configured-game-root> --user-confirmed
```

For `launch`, also pass `--executable <exact-executable-inside-game-root>`. The command produces a private approval JSON under `.game-l10n/runtime-approvals/` and reports `recorded_not_executed`.

The record is bound to:

- one action;
- the exact artifact path and content digest;
- the exact configured game root;
- the exact executable for launch;
- a short expiry, 30 minutes by default;
- one use.

A changed build, different path, different action, different executable, expiry, or prior use invalidates the record. The matching SDK operation consumes it immediately before the external effect. If the operation fails after consumption, report the failure and wait for a new user instruction instead of minting another approval automatically.

## Steam packages

The generated Steam installer permits read-only `Verify` without an approval. `Install` determines whether the operation is a first install or update and requires the matching approval; `Remove` requires its own approval. Pass the record explicitly:

```text
powershell -File <extracted-package>/Install-Translation-Patch.ps1 -Mode Install -GameRoot <configured-game-root> -ApprovalFile <approval-json>
```

The installer recomputes its manifest/payload fingerprint, checks the exact target, action, expiry, and one-shot state, consumes the record, and only then mutates receipt-owned paths.

## Engine SDK contract

An engine branch that adds real install or launch behavior must call `require_runtime_approval(..., consume=True)` immediately before the write or process start. It must not provide a second unguarded path, invoke the installer or executable directly, or translate another approval type into the requested action. Offline builders and validators remain inside the external workspace and do not need runtime approval.

The default runtime-validation handoff is still user execution: give the user the recoverable artifact and request only the resulting metadata or logs. An agent may run the real game only after a separate launch instruction and exact launch approval.

This SDK guard cannot cryptographically prove that a chat message came from a human, and a shell-capable agent could bypass any library by issuing operating-system commands directly. Repository and workspace `AGENTS.md` therefore prohibit direct copies, installers, process launches, or cleanup outside this guard. A host-level confirmation dialog remains the strongest boundary when the agent platform provides one.
