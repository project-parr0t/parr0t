# Core and engine SDK branch contract

The `main` branch is the engine-neutral control framework. It owns narrative context gates, provider routing, refusal and error taxonomy, candidate generations, review state, deterministic validation hooks, checkpointing, privacy boundaries, and source-preservation behavior.

An `sdk-<engine>-<version>` branch is a concrete data-plane implementation. It inherits `main` and adds only the public engine contract needed to discover text, unpack data, apply a validated translation manifest, repack a patch, and verify the rebuilt output.

## Dependency direction

The allowed dependency direction is:

```text
main framework  ->  engine SDK branch  ->  private per-game workspace
```

The reverse direction is forbidden. `main` must not contain an engine name, engine file layout, engine command, engine-specific warning, engine fixture, or exception added solely for one SDK branch. A private workspace must never be copied into either branch.

Distribution-layer packaging that does not inspect or interpret engine assets may live on `main`. It must consume an already prepared payload, remain independent of the payload's engine, and keep every title-specific identifier and file in the private workspace. Steam discovery and receipt-based installation are distribution concerns; text extraction, runtime hooks, asset rebuilding, and engine payload construction remain SDK-branch concerns.

Engine SDK branches may use every public interface from `main`. When `main` changes, each engine branch should merge or rebase the new core and run both the core suite and its own adapter suite. A core change must not import the adapter package or special-case its paths.

## Ownership

`main` owns:

- orchestration stages and hard barriers;
- provider protocols and categorical failures;
- context, glossary, voice, integrity, and privacy contracts;
- translation and review fallback policy;
- source immutability and patch-output requirements;
- engine-neutral delivery packaging and receipt contracts;
- sanitized SDK promotion and branch lifecycle.

An engine SDK branch owns:

- unpacking and text-target discovery;
- engine-specific manifests and protected fields;
- translation application, mirror updates, span handling, and escaping;
- engine payload assembly, packing, and round-trip verification;
- engine-specific documentation, FAQ entries, ignore rules, and synthetic fixtures.

## Branch acceptance gate

Before an engine SDK branch is published:

1. Merge or rebase the intended `main` revision.
2. Confirm the diff from `main` contains only that engine adapter and its neutral support files.
3. Run the core test suite, the adapter test suite, and the privacy scan.
4. Confirm no private game data, names, paths, text, hashes, counts, logs, or run history appears in the tree or branch-only commits.
5. Keep engine-only FAQ entries on the engine branch. Keep only framework-generic FAQ entries on `main`.

The framework may be installed without an engine adapter, but it cannot unpack or patch an engine by itself. Once an engine SDK branch supplies its adapter, the same central context, translation, fallback, review, and source-preservation flow becomes executable without duplicating orchestration logic.
