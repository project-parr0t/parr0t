# Per-game instance release contract

This contract applies when an agent publishes a completed localization as a separate, game-specific repository. An instance repository is distinct from the Parr0t framework and engine SDK branches: it may contain the approved patch payload and title-specific compatibility data, but none of that data may flow back into Parr0t.

Publication is an external side effect. A successful patch, permission to use Git, SDK publication approval, and permission to publish a per-game release are separate decisions. Do not create a repository, tag, release, or upload until the user explicitly approves that exact instance publication.

## One immutable release unit

Treat the source commit, tag, release metadata, committed distribution output, and uploaded assets as one immutable release unit.

- Build once from a clean commit, then upload those exact bytes. Do not rebuild between verification and upload.
- Record the source commit, tag, release ID, package digest, manifest digest, and every released file digest in the instance repository.
- If a deployable archive or file is committed, the corresponding release asset must be byte-identical. If only build source is committed, the tagged source must contain the complete deterministic build recipe and the recorded output digest must match the uploaded asset.
- A release must not contain untracked first-party code, translation data, installers, manifests, or support files absent from the tagged source or its recorded build inputs.
- A third-party runtime may be release-only when redistribution or repository-size constraints justify it, but its exact name, version, source, license, and digest must be declared in the tagged source and release manifest. Never describe a first-party component as a third-party exception.
- Keep the release ID aligned across the tag, release title, package manifest, installer receipt, embedded component versions, and release notes.

After upload, download or query the published asset read-only and compare its digest with the verified local artifact. A mismatch invalidates the release; do not silently replace one side or edit history.

## Original game compatibility

Every release must describe the exact original game builds it supports. Record every build actually validated, rather than a vague claim such as “latest” or “all versions.” Use the identifiers available for the title, including as applicable:

- storefront and product/application identifier;
- platform and architecture;
- public game version;
- storefront build or depot identifier;
- engine/runtime version;
- executable or data-layout version;
- source-binding file digests needed to distinguish otherwise identical version labels.

When multiple original builds are supported, list each as a separate compatibility record and ensure the installer accepts every listed binding. An unknown or changed build must fail before mutation with a clear compatibility result; it must not guess, bypass binding checks, or install using the nearest known version. Add a build only after validating the patch against it, and remove or mark a build unsupported explicitly when compatibility is withdrawn.

The repository README, release notes, package manifest, and installer checks must agree on the supported-build set. A release is incomplete when any one of those surfaces omits or contradicts it.

## Preserve the user's README

Read the existing README before generating release documentation. Treat its authorship, structure, tone, branding, acknowledgements, contribution guidance, screenshots, and license notices as user-owned content.

- Make the smallest targeted edit needed to add or update installation, compatibility, release, checksum, and support information.
- Do not replace the README with a generated template, reorder unrelated sections, erase attribution, or rewrite the user's voice unless the user explicitly requests it.
- Preserve existing instructions that remain correct. If generated instructions conflict with them, stop before publication and present the conflict to the user instead of choosing silently.
- Keep instance-specific model/provider disclosure only when the user already included it or explicitly requests it; Parr0t does not require such disclosure.
- Show the README diff during the publication review together with the exact assets and compatibility records.

## Required release gate

Before an agent publishes an instance release, it must verify all of the following:

1. The user has confirmed the patch itself succeeds.
2. The user has separately approved publication of this exact instance repository and release.
3. The source tree is clean, the intended commit is fixed, and the tag/release ID is unused.
4. The complete supported-build matrix is recorded and matches installer binding checks.
5. The committed distribution output or deterministic build recipe produces the exact staged assets.
6. Package validation, required engine/adapter checks, license checks, and one proportional synthetic lifecycle pass.
7. The existing README is preserved except for reviewed, release-specific edits.
8. Release notes identify the release ID, source commit, supported builds, changes, installation/update caveats, and asset checksums.
9. The uploaded assets are the same bytes that passed verification, and their published digests are checked once after upload.

If any gate fails, keep the staged release in the private workspace and report the mismatch. Do not publish a best-effort or partially documented release.
