# Steam Workshop metadata contract

This contract governs Steam Workshop publication and verification. Use it only for the exact action the user authorizes. Content publication, default metadata, localized metadata, Git publication, and game execution are separate actions.

## Determine item state from authoritative fields

Use Valve's native, authenticated Steam client API (`ISteamUGC`) for Workshop preflight and readback. Bind the exact item ID, creator, creator app, consumer app, visibility, ban flag, and result code. A Community-page warning or a failed web request is not evidence of deletion or moderation. Report either only when the authoritative client API fields establish it.

## Preserve complete locale records

Treat each managed Steam locale as a complete title-and-description record. Never publish a localized description without also setting a nonempty title. Bind every locale to one reviewed metadata manifest and one canonical description file; reject unknown or duplicate locale codes, path escape, over-limit UTF-8 values, and an item ID mismatch.

Treat `required_tags` as a non-destructive requirement set. Read the item's current tags, preserve them, add only missing tags using a case-insensitive comparison, and verify every required tag after submission. Never replace the current tag set with an incomplete manifest list. Use `--tags-only` when the authorized action must not rewrite localized titles or descriptions.

SteamCMD remains the required path for Workshop content and default-locale metadata. Its documented Workshop VDF does not provide localized-language selection. An unrecognized `language` field and a successful `workshop_build_item` command do not prove that localized metadata changed.

Follow `skills/patch-notes/SKILL.md` when drafting or revising the VDF `changenote`. It does not govern the item title or description.

Localized title and description synchronization may use `scripts/sync_steam_workshop_metadata.py`, which calls Valve's native Steam API through Python's standard library. Apply mode requires:

- the exact manifest-bound published-file ID through `--approve-item`;
- the manifest-bound owner Steam ID, matched against the authenticated Steam client account;
- an online, authenticated Steam client session;
- the exact reviewed Steam API library digest through `--approve-steam-api-sha256`;
- a first-party Valve-signed Steam API binary or an exact locked dependency already approved under `docs/third-party-consent.md`;
- an explicit locale subset or every locale declared by the manifest.
- a reviewed `required_tags` list when tags are changed; empty or omitted lists do not mutate tags.

Do not acquire or execute a third-party Workshop publisher as a fallback. Do not extract browser or Steam client credentials, cookies, session tokens, or API keys. If the native API rejects an update, keep the update held. An authenticated community-page edit is a separate browser mutation and requires the user's explicit authorization and their own interactive login.

## Verify through the Steam client API only

SteamCMD is for authorized uploads, not verification. Its success acknowledgement is not proof that the release is available. After SteamCMD exits, verify through the Valve client API only, using the reviewed Valve library and an online client signed into the expected account. SteamCMD login can disconnect the desktop client; if it is offline or unauthenticated, hold verification and ask the user to reconnect. Do not run SteamCMD again to verify, or fall back to the Steam Web API, `steamcommunity.com`, browser automation, scraping, or any other HTTP probe.

1. Query the exact Workshop item through `ISteamUGC` and require the expected identity, visibility, and ban state. Reuse that record for other checks it supports, including required tags.
2. For content, confirm the client has the current Workshop install with no update pending, and compare the files at the path returned by the client API with the staged package. A stale local copy or the upload acknowledgement alone does not pass. If a field such as changenote text cannot be read through the client API, report it as independently unverified rather than fetching a web page.
3. For a localized release, read back only English (`english`) and Korean (`koreana`) once each. If both match the expected title and description and all requested submissions succeeded, this passes the locale verification gate for the release; it does not mean other locales were read back. For a partial metadata update, read back only the selected languages among those two. Content-only uploads do not trigger locale readback.
4. Keep at most one network-bearing UGC request in flight per workflow. Wait for it to complete, then wait at least one second before submitting another; apply any stricter Steam limit. This is a local ceiling, not a Valve quota. Pumping callbacks for a pending request does not issue a new request.
5. On a rate-limit response, such as `k_EResultRateLimitExceeded` or HTTP `429` reported by existing logs, stop the batch immediately. On an access-denied response, offline session, or timeout, stop and report the exact failure. Do not retry automatically, fan out by locale, or switch clients, accounts, IPs, or HTTP paths. Respect any reported cooldown; a later, explicitly authorized attempt may query only outstanding records. Leave verification pending until the required checks pass.
