# Image inspection and localization

Image work has two distinct scopes. Text localization does not imply image translation, but an explicit request to inspect specified or inventory-bound images permits bounded visual inspection without granting translation. An explicit request for player-visible image text may include UI, CG, scenery, backgrounds, or other non-UI assets; do not narrow it to UI-only targets. The request and policy must identify the actual scope.

## Request and inspection

Record image policy before image access:

```text
game-l10n image-localization-policy --workspace <workspace> --mode inspect --scope "<inspection purpose>" --user-requested
game-l10n image-localization-policy --workspace <workspace> --mode requested --scope "<player-visible image-text scope>" --user-requested
```

`inspect` permits only the requested bounded visual inspection. It does not authorize OCR-based translation, text edits, game mutation, installation, launch, or publication. Image translation requires the separate `requested` mode. Fonts, glyph atlases, texture atlases, dimensions, and rendering metadata may be inspected when needed to diagnose rendering or coverage; inspection alone is not translation approval.

Use the validated game inventory to bind exact local assets and unchanged SHA-256 digests. Provide only those paths as `--inspection-asset` or through the core-generated `bound_local_assets` task payload. The worker must not browse game roots or inspect unbound files. Offline image processing is permitted only through explicitly approved, exact local executable/argument bindings; it must remain offline and confined to bound assets and private output. A new third-party tool, service, executable, or data transfer still requires exact actual user consent under `skills/third-party-consent/SKILL.md`. Do not install or launch the game.

For automatic loose-asset inventory, Core discovers and hash-binds image/font candidates within the authorized scope and supplies a private candidate manifest; callers need not manually enumerate them. Unexamined packaged, linked, unreadable, or unsupported sources remain explicit deterministic SDK/extractor repair work, never proof of `no_targets`. Native sealing verifies the report against that exact discovery. Direct revalidation uses `verify-image-ui-inventory --candidate-discovery <core-generated-workspace-json>` and must not erase bound discovery evidence or turn incomplete scope into completion by omitting it.

An unchanged opaque-source retry does not launch another image inventory worker. Core applies the hash-bound additive extractor requirements without dropping existing formats, capabilities, metadata, or unrelated policy, then uses the existing isolated extractor-preparation flow. Pending image repair does not stop independent text work. Only an exact readiness-attested candidate manifest with complete original-source accounting and unchanged hashes admits the narrow `inputs/image-assets` cache as a read-only image source; the workspace as a whole never becomes a game root.

## Scope discovery and translation

Inspection and image translation may run in parallel with ordinary text localization when their inputs and task scopes are independently bound; do not impose a text-before-image scheduling barrier. The isolated image inventory begins from engine UI/atlas metadata, identified assets, or the requested player-visible asset inventory. It must not recursively OCR every image or treat filenames, folders, texture format, or OCR guesses as proof of visible text.

For translation, include an asset only when its exact source path and digest, format, dimensions, and evidence are bound in `parr0t.image-ui-inventory.v1`; each translated region must be visually confirmed as text and player-visible within the user's requested scope. UI bindings and atlas regions can establish scope; non-UI CG/scenery may be included when the user requested that player-visible scope and visibility/text are confirmed. Ambiguous or unexamined candidates remain counted as uncertain/unexamined; they cannot silently disappear behind `no_targets`. `no_targets` is valid only for a complete examination of the requested scope with no confirmed targets.

OCR can assist only within authorized, bound candidates; visually confirm every OCR-derived region. Manual transcription and metadata inspection are also valid. Keep uncertain candidates out of the translated target set while reporting them honestly.

The isolated `image_localizer` receives only verified inventory entries and exact bound local assets. It translates confirmed regions into `project.target_language`, preserves source lineage, placeholders, layout intent, and style, and writes new assets under the private workspace. It never edits the game root.

## Verification and delivery

Each `parr0t.image-ui-results.v1` row binds the original asset and every confirmed region to one output file. Deterministic checks must establish:

- format and dimensions remain valid;
- alpha behavior is preserved or proved inapplicable;
- atlas layout, rotation, region placement, and animation references are preserved or proved inapplicable;
- pixels outside declared text regions are unchanged;
- each declared region has a result with source lineage and preserved placeholders/control data.

Independent visual review is reported distinctly from user-accepted unreviewed work. A worker cannot claim user acceptance. If the user explicitly accepts exact unreviewed assets, record that bounded decision after scope verification:

```text
game-l10n accept-unreviewed-images --workspace <workspace> --asset-id <verified-id> --user-requested
```

Repeat `--asset-id` for each accepted asset. The core binds approval to the current request revision, inventory digest, and exact IDs; stale, invented, or out-of-scope evidence fails verification. Failed or uncertain images are excluded from the completed image set and are never relabeled reviewed. Parent-visible attestation stores counts, digests, and categorical status only. Report unreviewed and incomplete scope truthfully.

The SDK maps verified outputs to exact asset bindings. An image-result attestation is not runtime evidence: without separately authorized or user-executed game observation, report `REAL_RUNTIME_NOT_EXECUTED` and do not claim every screen rendered correctly. Image files join the package replacement set and update/restore contract like other payloads. If adding images changes the replacement set, restore the prior release before a fresh install.
