# Security and data handling

- Game assets, extracted strings, translations, model prompts, caches, and audit logs are private work artifacts and must remain outside this repository.
- API keys are read from named environment variables. The SDK does not accept literal keys in its generated configuration.
- Local command providers are executed without a shell and receive game text through stdin JSON.
- The audit log records only target IDs, provider names, categorical status/reason codes, deterministic issue codes, and timing. It excludes source, candidates, translated text, prompts, provider prose explanations, and reviewer prose.
- Engine profile export only accepts a small source/document extension allowlist, rejects data-like paths and filenames, and records SHA-256 hashes.
- `publish-sdk` requires two recorded approvals, a clean repository, an existing `main`, and an already initialized Git repository.
- The repository is provenance-free: no real project names, source filenames, asset paths, dialogue, translations, job metadata, private exclusions, or execution reports may appear in code, docs, fixtures, commits, or retained branch history. Only public engine-format identifiers and clearly synthetic fixtures are allowed.
- FAQ entries describe only engine/SDK-generic symptoms and remedies. Job-specific incidents remain in the external private workspace and are never summarized in this repository.
- Provider presets keep credentials in environment variables. Selecting a preset plus supplying its keys authorizes only the configured per-target context/translation/review calls; audit records exclude prompts, source, candidates, and translated text.
- Configured game roots are immutable inputs. Translation, rebuilt data, archives, and patches must be separate outputs in the external workspace; exhausted routes preserve the source explicitly instead of modifying originals.
- The top-level agent is a control-plane observer. Text-bearing data-plane files stay in the private workspace and workers return metadata-only envelopes. DeepSeek-compatible requests use deterministic low-creativity settings, but the SDK does not claim to disable or bypass provider safeguards.

Run `python scripts/check_repo_privacy.py` before every commit.
