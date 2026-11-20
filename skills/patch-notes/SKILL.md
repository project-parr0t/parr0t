---
name: patch-notes
description: Draft, revise, or review player-facing patch notes and the change list in a release or Workshop changenote. Do not apply this skill to commit messages or README.md.
---

# Patch notes

Apply this skill **only to patch notes**, including the change list inside a release announcement or a Steam Workshop `changenote`. It does **not** govern commit messages, `README.md`, or other release metadata. This skill governs writing, not permission to commit, upload, or publish.

## Select what to say

1. List only what the exact release adds, removes, or changes. Compare its final contents with the prior published release or use the user's verified list. Omit anything unchanged, unfinished, or not in the release. If nothing changed, do not invent an entry.
2. State the change at the level users need. When a release adds support for an add-on, write “Added support for Example Add-On.” Do not append how the support was packaged, which alternatives were considered, or assurances unrelated to the support addition. Include a changed requirement or required player action only when it is itself a shipped change and readers need it to use the release.
3. Keep session material out: no agent or session opinions, justifications, speculation, task history, review commentary, internal coverage or missing-translation counts, or plans. Put necessary verification findings in the internal handoff, not the patch notes.

## Select the format

Use the first applicable option:

1. Follow the current user's specified format, including its language and layout.
2. Otherwise, reuse an applicable previously published format for the same product and channel. Match its heading, language, and list style; do not copy its old changes. A session draft is not a published precedent.
3. If neither applies, choose a simple format that fits the destination, such as a version heading and one bullet per change. Do not force a set number of bullets or add filler.

For example, if the channel's existing format is a product-and-version first line followed by `- ` entries:

```text
Example Mod 1.2.0
- Added support for Example Add-On.
```

Use the version of the exact package being released. Keep platform-required release metadata separate from the list of changes.
