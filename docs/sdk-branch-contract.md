# Core and engine SDK branch contract

The `main` branch is the engine-neutral control framework. It owns typed narrative, functional, knowledge, and hybrid context gates; provider routing; refusal and error taxonomy; candidate generations; review state; deterministic validation hooks; checkpointing; privacy boundaries; and source-preservation behavior.

An `sdk-<engine>-<version>` branch is a concrete data-plane implementation. It inherits `main` and adds only the public engine contract needed to discover text, unpack data, apply a validated translation manifest, repack a patch, and verify the rebuilt output.

The branch name is a compatibility claim, not a statement of code ancestry. `<version>` identifies the public engine/runtime compatibility line actually exercised and validated. Code taken from or inspired by another SDK branch does not authorize publishing the capability under that reference branch's version.

## Checkpoint-only framework-version compatibility

Parr0t supports earlier framework versions only through validated checkpoint data.
Core and SDK execution always use current code for the selected engine/version.
An engine compatibility line is not permission to retain an older Parr0t framework.

Preserve checkpoint schemas, completed target identities, artifact hashes, selected
language, authorization and actual model provenance through explicit data migration.
Treat recorded runtime paths, argv and tool transports as historical evidence only.
Do not restore old checkouts, recreate deleted paths, add aliases/shims, or execute
an older framework to resume a job. After observed worker termination, close the
old attempt and compile a fresh task/relay and isolation binding with current Core.
Unknown or active workers still require real host-owner termination evidence;
checkpoint migration must never fabricate completion or silently relabel launches.


## Dependency direction

The allowed dependency direction is:

```text
main framework  ->  engine SDK branch  ->  private per-game workspace
```

The reverse direction is forbidden. `main` must not contain an engine name, engine file layout, engine command, engine-specific warning, engine fixture, or exception added solely for one SDK branch. A private workspace must never be copied into either branch.

Distribution-layer packaging that does not inspect or interpret engine assets may live on `main`. It must consume an already prepared payload, remain independent of the payload's engine, and keep every title-specific identifier and file in the private workspace. Steam discovery and receipt-based installation are distribution concerns; text extraction, runtime hooks, asset rebuilding, and engine payload construction remain SDK-branch concerns.

Engine SDK branches may use every public interface from `main`. When `main` changes, check compatibility before changing each branch. Follow `docs/branch-update-contract.md`: equivalent core content may skip a history-only update, while missing core changes require a validated update. Reconcile divergent core ownership separately. A core change must not import the adapter package or special-case its paths.

## Ownership

`main` owns:

- orchestration stages and hard barriers;
- provider protocols and categorical failures;
- context, glossary, voice, integrity, and privacy contracts;
- translation and review fallback policy;
- source immutability and patch-output requirements;
- the deny-by-default runtime approval guard for real install, update, remove, and launch actions;
- engine-neutral delivery packaging and receipt contracts;
- sanitized SDK promotion and branch lifecycle.

An engine SDK branch owns:

- unpacking and text-target discovery;
- engine-specific manifests and protected fields;
- translation application, mirror updates, span handling, and escaping;
- engine payload assembly, packing, and round-trip verification;
- engine-specific documentation, manual exception runbooks, ignore rules, and synthetic fixtures.

After its deterministic pack and round-trip verification succeeds, an engine branch records the artifact through the core `record_verified_pack_artifact` API. This binds the engine output to the exact completed translation result without routing engine logic into `main`. An unverified output, a generic pass-through copy, or a pack built from another result must not create core pack state.

## Display eligibility before semantic work

Every SDK separates its private raw-occurrence census from its active translation inventory. A string, a source-language match, an engine/resource container, two equal inventories, or an evidence digest alone is not a display-use proof. This separation is mandatory; the additional reachability pruning in `docs/engine-static-scope.md` is optional.

The SDK must derive each active target from a supported display-bearing format role or a source-bound consumer/dependency trace. Account separately for display targets, actual unresolved display dependencies, proven non-display occurrences, and raw unclassified occurrences. Resolve unsupported roles from cached structural evidence through the isolated SDK preparation/reconciliation workflow, not a human per-string whitelist or a semantic context pass over every raw literal. An unresolved actual display consumer keeps affected scope pending; it must not disappear behind a complete claim.

Control keys, indices, script operands and shared control storage are not active replacement targets. Preserve their bytes and use distinct display-only projections when the same value also reaches a display. `display_projection_required=true` is a repair requirement, not permission to translate control storage. New inventories emit the existing `coverage.display_scope` and source-bound per-target evidence; no parallel eligibility schema is needed.

Context is evidence for already eligible targets, not an inventory filter. Attach only the relevant speaker, branch, state, composition and explicitly bound semantic facts. Keep full command trees, editor records, unrelated notes, diagnostics and parse/provenance caches outside model inputs. Useful annotations may be retained when bound to the target; do not indiscriminately remove all notes or classify their wording in the text-free parent.

Primary and independent methods independently discover supported physical target locations and their eligibility. They may share lossless format readers, public role specifications and identity normalization, but not a discovered segment list, copied classifications, or counterpart output. Two traversals of one already selected list do not establish independent extraction.

Put repeatable cached reconciliation in the SDK's deterministic entry point,
not in scripts generated or substituted by individual relays. Validate each
method's cache schema and evidence binding separately. Apply each supported
consumer rule across the matching unresolved occurrences in one cached pass.
Distinguish an unsupported consumer from missing or conflicting evidence in
aggregate diagnostics. Reuse locked outcomes; do not infer an exclusion
from a missing rule or classify by equal wording.

SDK acceptance fixtures must exercise the actual extractor-to-consumer boundary: visible text survives; control/editor/unused records do not become targets; unsupported actual display dependencies remain pending; relevant context survives without unrelated records; excluded source bytes remain unchanged on apply/repack; and an omission in one discovery method produces an inventory disagreement. Use synthetic format fixtures, not source-text assertions, output echoes, or mere equal counts. A core test pass, a clean privacy scan and an `up_to_date` branch state do not establish these engine-specific properties.

Existing checkpoint-bound raw history is never silently deleted to fix eligibility. Use the bound scope-correction contract for retired occurrences and regenerate affected context/review bindings without renaming successful historical identities.


## Selection before implementation

The agent must complete `docs/sdk-selection.md` before it writes provisional format tooling. A compatible SDK branch with the same engine/version identity is reused directly. A partial same-identity match is extended with the smallest missing capability. A different identity with reusable implementation is recorded as a reference and selected through `derive-new`; it is not extended. A new private adapter without a reference is allowed only after every inventoried candidate is evaluated as incompatible. Neither the current checkout nor the absence of a local branch is evidence that no reusable SDK exists.

The decision is bound to candidate commits, complete content fingerprints, and an explicit target engine/version identity in the external workspace. A separate activation record binds the actual SDK runtime—Git checkout/worktree or authenticated portable snapshot—and, for a new adapter, its workspace tool manifest. Data-plane work must validate both records; merely selecting a branch while continuing from `main` or the generated pass-through tools is invalid. Either record must be refreshed when its bound refs, runtime content/path, tools, or target identity change. The private game workspace Git policy is unrelated to this binding. This state is job control metadata and must never be promoted into an SDK branch.

## Promotion ownership

Promotion exports only behavior actually used and verified after the user confirms success. Its destination is determined by the recorded target identity, never by the checkout that supplied reference code:

- `reuse` publishes nothing new.
- `extend` may add the verified delta only to the same-identity selected branch.
- `derive-new` creates `sdk-<target-engine>-<target-version>` from `main` and ports only the verified, provenance-free implementation needed for that target. The reference branch remains unchanged and must not be merged wholesale.
- `private-new` creates the recorded target branch from `main` without pretending that an incompatible branch owns the new support.

A promotion commit owns engine-specific implementation only. It must be a separate non-merge commit after the intended `main` revision is merged, and it must not modify any path that exists on `main`. Do not use promotion to bundle a generally useful helper, control-plane rule, provider change, common test, root documentation edit, or cleanup. If engine work exposes a genuine core need, stop promotion, make that a separately authorized `main` change, verify it there, merge the resulting core commit into the SDK branch, and then create a clean engine-only promotion commit. Never stage or commit both scopes together.

An exact engine build observed in a private workspace may be reduced to its declared public compatibility line for the branch name, but the line must be derived from that observed build before evaluation begins. After it is recorded, the exact `sdk-<engine>-<version>` identity controls ownership; an older, newer, or merely similar branch cannot absorb the result.

For cached reconciliation, the promotion candidate may contain reusable
cache readers, public-format consumer rules, and synthetic regression
fixtures. Keep rule instances bound to private files or ranges, their
witnesses, scan caches, and diagnostic counts in the external workspace.
A successful cache-only run proves only that exercised capability; it does
not satisfy the confirmed patch-success gate.

## Branch audit and update route

Use `docs/branch-update-contract.md` for all-branch health checks, content-bound validation reuse, update decisions, and explicit local application. Its separate states distinguish current core content from a successful simulation of a future update. A simulation alone cannot skip a core update. SDK selection, ownership, and the promotion acceptance gate below remain mandatory.

## Promotion preparation and fixed base

Treat generalizing private tooling, adding synthetic fixtures, and fixing adapter behavior as SDK implementation. Promotion preparation begins after those capabilities and their acceptance criteria are known. Read this contract and the engine FAQ contract together, and consolidate required fixes before implementation rather than distributing new requirements over repeated review rounds.

Before implementation, resolve the intended local `main` commit and target SDK branch once. Record that full base hash and the planned file list outside the repository. Run the existing scope checker early, before creating files or running broad regression:

```text
game-l10n verify-sdk-promotion --sdk-root <repository> --branch <sdk-branch> --working-tree --planned-path <new-sdk-file>
```

Repeat `--planned-path` for each intended file. Preflight requires the target branch checkout and checks committed, staged, unstaged, untracked, and planned paths against `main`. It does not create files, stage, commit, fetch, or change branches. Its `scope_ready` result attests path ownership only; it is not test, privacy, or publication approval. Split a rejected core path into a separately authorized core change immediately.

Use the returned `base` hash as `--expected-main <full-hash>` in subsequent preflight and final commit checks. This pins the local reference; it neither refreshes nor attests the remote. Do not silently adopt a moving upstream during finalization. When an intended base update is necessary, explicitly reconcile its contract/code changes, record the new hash, and invalidate only validation evidence affected by changed inputs.

Check local Git identity, signing configuration/tool availability, and configured destination early without exposing credentials or creating trial commits. Authentication, signing, or network failures have separate causes: preserve the working tree, diagnose the failed prerequisite once, and use the retry limits in `docs/execution-budget.md`. Network publication and other external actions still require their own exact user request.

## Branch acceptance gate

Before an engine SDK branch is published:

1. Merge or rebase the intended `main` revision.
2. Confirm the branch name equals the recorded promotion target and that every promoted capability was exercised on that target compatibility line.
3. Confirm the diff from `main` contains only that engine adapter and its engine-owned support files. For `derive-new`, inspect the diff rather than merging the reference branch. Run `game-l10n verify-sdk-promotion --sdk-root <repository> --branch <sdk-branch> --commit <promotion-commit> --expected-main <full-hash>`; any `main`-owned path is a hard failure and must be split into a separate core change.
4. Run the core test suite, the adapter test suite, and the privacy scan.
5. Confirm no private game data, names, paths, text, hashes, counts, logs, or run history appears in the tree or branch-only commits.
6. Classify every material problem observed during adapter work under `docs/engine-faq-contract.md`. Put deterministic fixes in code and regression tests, keep title-specific or unresolved material private, and place only SDK-external manual exceptions in the branch's `docs/engine-faq.md`. Run `python scripts/check_engine_faq.py docs/engine-faq.md`; zero entries is valid.
7. Keep framework-level troubleshooting in `main`'s `docs/faq.md`. Never merge an engine runbook into `main`.
8. Call the core runtime approval guard immediately before any real installation write or process launch. Do not add an unguarded direct execution path in an SDK branch.

Complete the acceptance gate on the fixed code and base before creating the final archive, patch export, or report. Produce those delivery artifacts once from that validated tree. If an input changes, rerun the affected checks and rebuild only affected artifacts. A successful scope check does not replace the other acceptance checks. Keep a reviewable local result until the user requests the exact commit, push, or other external action; request no publication approval while implementation remains unfinished.

The framework may be installed without an engine adapter, but it cannot unpack or patch an engine by itself. Once an engine SDK branch supplies its adapter, the same central context, translation, fallback, review, and source-preservation flow becomes executable without duplicating orchestration logic.
