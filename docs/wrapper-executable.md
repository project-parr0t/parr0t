# Trusted launcher-wrapper contract

A Windows executable wrapper is necessary when Steam must continue to start the original executable path and a filesystem shortcut cannot serve as that executable. The safe answer is not to generate a different opaque EXE for every game. Parr0t uses one minimal, public, versioned wrapper implementation and releases one stable binary per supported architecture. Per-game packages copy and rename those exact bytes.

## Stable-binary design

The wrapper derives its preserved-original path from its own filename:

```text
<directory>/<name>.exe
<directory>/<name>.parr0t-original.exe
```

Renaming a signed wrapper does not change its bytes. The same architecture/version binary can therefore wrap multiple explicitly declared executables while retaining one file hash and one publisher signature. Do not compile game names, paths, App IDs, commands, URLs, or private data into it. Do not rebuild it merely because a game package changed.

The wrapper must:

- resolve its own canonical path and the adjacent preserved-original path;
- reject a missing original, a directory escape, a reparse/symlink target where unsupported, or a target that resolves back to itself;
- invoke the original directly with the Windows process API, never through a shell, script interpreter, URL handler, or Steam client setting;
- preserve the caller's working directory and inherited environment;
- forward arguments according to the documented Windows quoting rules and return the original process exit code;
- avoid elevation, network access, registry writes, self-update, self-modification, DLL injection, process hollowing, persistence, telemetry, advertising, and unrelated file access;
- show only a short deterministic error directing the player to Verify or Restore when the original cannot be launched;
- contain no packer, obfuscator, encrypted payload, runtime download, or dynamically generated code.

The wrapper does not patch memory, bypass DRM, disable platform security, or launch an unsupported build. If direct wrapping is incompatible with a title, report the limitation instead of adding stealth behavior.

## Build once, reuse exact bytes

Maintain wrapper source and its build recipe in Parr0t. Build only when that source changes:

1. Pin the supported compiler/toolset version and target architecture (`x86`, `x64`, or `arm64`).
2. Build in a clean, network-isolated job with no game data and no secrets except the later signing boundary.
3. Enable deterministic/reproducible PE output, fixed optimization/security flags, and source-path mapping. Microsoft documents `IMAGE_DEBUG_TYPE_REPRO` as the PE marker for deterministic output.
4. Build the same unsigned source twice in independent clean jobs and require identical bytes.
5. Run the bounded synthetic suite: canonical-path rejection, recursion rejection, missing-original error, Unicode/spaces, argument round trips, working directory, environment inheritance, child exit code, multiple renamed copies, and install/update/Verify/Restore.
6. Inspect imports and behavior to confirm there is no network, shell, registry, elevation, injection, or Steam-client control surface.
7. Publish the verified unsigned hash, then sign the exact verified file. Never rebuild between verification and signing.

The signed file will differ from the reproducible unsigned file because the signature and timestamp are appended. Record both hashes and bind them to the same source tag and build record.

## Signing and Windows reputation

For public direct download, Authenticode-sign every released wrapper with one consistent, identity-validated publisher certificate that chains to the Microsoft Trusted Root Program. Keep the private key in the signing service, HSM, or hardware token; never export it to an agent workspace or repository. Use SHA-256 for the file and timestamp digests, verify the signature after signing, and record the signer identity, certificate thumbprint, timestamp, and signed SHA-256.

Microsoft currently recommends Artifact Signing for eligible non-Store publishers and a conventional OV certificate when that service is unavailable. A self-signed certificate is for controlled development only. EV no longer provides an automatic SmartScreen bypass. Most importantly, even a correctly signed new binary can initially show an “unrecognized app” warning: SmartScreen considers both publisher reputation and the exact file hash. Reusing the same signed wrapper bytes and consistent publisher identity is therefore materially better than producing a fresh per-game binary, but it cannot guarantee that no warning will appear.

References:

- [SmartScreen reputation for Windows app developers](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation)
- [Code signing options for Windows app developers](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options)
- [SignTool](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool)
- [PE deterministic build marker](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format)

## What the player can verify

Every instance release that contains a wrapper must provide, before download:

- the Parr0t wrapper contract/version and source tag;
- links to the exact public source and pinned build recipe;
- target architecture and minimum Windows version;
- unsigned reproducible SHA-256 and released signed SHA-256;
- verified publisher name, certificate thumbprint, and signing timestamp, or an explicit `unsigned` label;
- the package SHA-256 and the wrapper's payload hash from `parr0t-patch-manifest.json`;
- a plain-language statement that the wrapper only starts the adjacent preserved original and forwards arguments/exit status;
- instructions to inspect the file's Digital Signatures tab and compare the published hash.

Do not tell users to disable Defender, SmartScreen, Smart App Control, or another antivirus. Do not present a multi-engine scan result as proof of safety. If Defender reports a believed false positive, pause the release and submit the exact generic wrapper binary—not private game data—to the [Microsoft Security Intelligence software-developer submission portal](https://www.microsoft.com/en-us/wdsi/filesubmission). Record the submission outcome before resuming that artifact's distribution.

## Package and release gate

An agent may use a wrapper only when all of these are true:

1. The binary exactly matches an approved Parr0t wrapper release for the required architecture.
2. Its Authenticode result and signed hash match the published wrapper record; an explicitly approved unsigned development build is confined to synthetic testing and is not a public release.
3. The payload places those exact bytes at each declared main-executable path and `replace_files` uses the adjacent `.parr0t-original.exe` path.
4. The package manifest, receipt, status marker, instance source tag, release notes, and downloadable asset agree on the wrapper version and hashes.
5. The wrapper behavior and installer lifecycle pass once in a synthetic copy; the agent does not install or launch the real game without the separate requested action.
6. The distribution contains no ad hoc wrapper compiled in a game workspace, no downloaded third-party launcher, and no binary that differs from the reviewed release.

Until a wrapper implementation has completed this release gate, an installer may package ordinary overlays and other declared replacements, but it must not claim to provide a trusted Parr0t Steam launcher wrapper.
