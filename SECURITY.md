# Security and data handling

- Game assets, extracted strings, translations, model prompts, caches, and audit logs are private work artifacts and must remain outside this repository.
- API keys are read from named environment variables. The SDK does not accept literal keys in its generated configuration.
- Local command providers are executed without a shell and receive game text through stdin JSON.
- The audit log records provider/status/reason metadata but excludes source, translated text, and prompts.
- Engine profile export only accepts a small source/document extension allowlist, rejects data-like paths and filenames, and records SHA-256 hashes.
- `publish-sdk` requires two recorded approvals, a clean repository, an existing `main`, and an already initialized Git repository.

Run `python scripts/check_repo_privacy.py` before every commit.
