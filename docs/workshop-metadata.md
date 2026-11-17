# Steam Workshop metadata contract

Use this contract only after the user explicitly authorizes the exact Workshop metadata mutation. Content publication, default metadata, localized metadata, Git publication, and game execution are separate actions.

## Determine item state from authoritative fields

Query `ISteamRemoteStorage/GetPublishedFileDetails` before mutation and bind the returned item ID, creator, creator app, consumer app, visibility, `banned`, and result code. A localized community page, generic warning banner, missing localized title, or anonymous rendering anomaly is not proof that an item was deleted or moderated. Report deletion or moderation only when an authoritative status field establishes it.

## Preserve complete locale records

Treat each managed Steam locale as a complete title-and-description record. Never publish a localized description without also setting a nonempty title. Bind every locale to one reviewed metadata manifest and one canonical description file; reject unknown or duplicate locale codes, path escape, over-limit UTF-8 values, and an item ID mismatch.

Treat `required_tags` as a non-destructive requirement set. Read the item's current tags, preserve them, add only missing tags using a case-insensitive comparison, and verify every required tag after submission. Never replace the current tag set with an incomplete manifest list. Use `--tags-only` when the authorized action must not rewrite localized titles or descriptions.

SteamCMD remains the required path for Workshop content and default-locale metadata. Its documented Workshop VDF does not provide localized-language selection. An unrecognized `language` field and a successful `workshop_build_item` command do not prove that localized metadata changed.

Localized title and description synchronization may use `scripts/sync_steam_workshop_metadata.py`, which calls Valve's native Steam API through Python's standard library. Apply mode requires:

- the exact manifest-bound published-file ID through `--approve-item`;
- the manifest-bound owner Steam ID, matched against the authenticated Steam client account;
- an online, authenticated Steam client session;
- the exact reviewed Steam API library digest through `--approve-steam-api-sha256`;
- a first-party Valve-signed Steam API binary or an exact locked dependency already approved under `docs/third-party-consent.md`;
- an explicit locale subset or every locale declared by the manifest.
- a reviewed `required_tags` list when tags are changed; empty or omitted lists do not mutate tags.

Do not acquire or execute a third-party Workshop publisher as a fallback. Do not extract browser or Steam client credentials, cookies, session tokens, or API keys. If the native API rejects an update, keep the update held. An authenticated community-page edit is a separate browser mutation and requires the user's explicit authorization and their own interactive login.

## Verify the public result

Command success is not publication proof. After mutation:

1. Query authoritative public item details again and require a successful result, the expected public item binding, and no ban.
2. Read every managed locale publicly and require the expected nonempty title and canonical description.
3. Read the item's tags through the native UGC query and require every manifest `required_tags` entry while permitting preserved pre-existing tags.
4. Record mismatched locales or tags and retry only those records through the same authorized path.
5. If public verification is blocked or inconsistent, hold completion and report the exact evidence; never reinterpret it as deletion, moderation, or success.
