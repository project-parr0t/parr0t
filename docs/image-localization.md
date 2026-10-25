# Optional UI-image localization contract

Parr0t is a text-localization system by default. Image inspection, OCR, image editing, and image-translation planning are out of scope unless the user explicitly requests image translation. Access to images, an attached screenshot, or evidence that a game uses textures does not grant that request.

## Entry gate

After an explicit request, record it without opening an image:

```text
game-l10n image-localization-policy --workspace <workspace> --mode requested --user-requested
```

This enables only the bounded discovery route. It does not prove that any image is a translation target and does not authorize installation, launch, publication, or recursive OCR. A later explicit revocation uses `--mode disabled --user-requested`; existing private artifacts are preserved.

Status routes the optional work only after the ordinary text result is complete:

```text
verify_requested_ui_image_scope
  -> run_requested_ui_image_localization (only when confirmed targets exist)
  -> deterministic image-result verification
  -> normal patch packing
```

## Definite-target rule

The isolated `image_ui_inventory` worker starts from engine UI bindings, atlas/UI metadata, or image assets specifically identified by the user. It must not recursively OCR every image or treat a filename, folder, texture format, or OCR guess as proof.

An asset is eligible only when all of these are true:

1. a supported engine UI/atlas binding or user-identified UI-asset binding is recorded with evidence;
2. at least one bounded text region is visually confirmed as actual text;
3. the region is player-visible UI, not scenery, decoration, concept art, or an uncertain texture;
4. the source asset path, digest, format, dimensions, region bounds, and evidence are bound in `parr0t.image-ui-inventory.v1`.

OCR may assist only after the user-request gate and only inside bounded UI candidates. Every OCR-derived region must receive visual confirmation. Uncertain candidates stay out of the image target set and remain counted as uncertain. A complete empty inventory is a valid `no_targets` result and returns to the text-only path.

## Localization and review

The isolated `image_localizer` receives only the verified inventory. It must not scan or OCR unlisted images. It translates the confirmed regions into `project.target_language`, preserves layout intent and style, and writes new assets under the private workspace. It never edits the game root.

Each `parr0t.image-ui-results.v1` row binds the original asset and every confirmed region to one output file. Before acceptance, the selected SDK must provide deterministic evidence that:

- format and dimensions remain valid;
- alpha behavior is preserved or proved inapplicable;
- atlas layout, rotation, region placement, and animation references are preserved or proved inapplicable;
- pixels outside declared text regions are unchanged;
- all declared text regions are translated;
- the asset received independent visual review, or the user explicitly accepted it as unreviewed.

`reviewed` and `user_accepted_unreviewed` are distinct. A worker-supplied claim is not user acceptance. If the user explicitly accepts exact unreviewed assets, record that bounded decision after scope verification:

```text
game-l10n accept-unreviewed-images --workspace <workspace> --asset-id <verified-id> --user-requested
```

Repeat `--asset-id` for each accepted asset. The core binds the approval to the current request revision, inventory digest, and exact IDs; stale, invented, or out-of-scope evidence fails verification. Failed or uncertain images are excluded from the completed image set and are never relabeled as reviewed. The result verifier stores only counts, digests, and categorical status in the parent-visible attestation.

## Application and delivery

The SDK maps verified outputs to their exact asset bindings. An image-result attestation is not runtime evidence: without a separately authorized or user-executed game observation, report `REAL_RUNTIME_NOT_EXECUTED` and do not claim that every screen rendered correctly.

Image files join the same package replacement set and update/restore contract as other payloads. If adding images changes the replacement set, the player must restore the prior release before a fresh install. Validate the real ZIP's install, update, verify, and restore lifecycle in a disposable synthetic target; do not install it into the supplied game during automated checks.
