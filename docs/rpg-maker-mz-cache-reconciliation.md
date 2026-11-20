# RPG Maker MZ cached reconciliation

This development component proposes non-display corrections for audited
JavaScript consumers. It is not a full engine adapter or an SDK activation.
Only the bound SDK worker may use the private report; Core still owns pair
verification and sealing. No game file, inventory, or ledger is changed.

Run `python -m game_l10n.rpg_maker_mz.cache_reconciliation --workspace <workspace> --request <relative-json> --request-sha256 <digest> --report <fresh-relative-json>`
from a checkout with the package on the import path. The request schema is
`parr0t.mz-cache-reconciliation-request.v1`; its `bindings` object maps
`primary_cache`, `independent_cache`, `primary_inventory`,
`independent_inventory`, `primary_raw`, `independent_raw`, and `rules` to
separate `{ "path": ..., "sha256": ... }` inputs. Paths are resolved under the
workspace; digests bind exact bytes. Caches use `parr0t.primary-source-cache.v1`
(path-keyed files) and `parr0t.independent-structural-cache.v2` (file records).
Raw ledgers name physical `occurrences` with `location` and `source_sha256`.

Each rule names one audited `file`, statement `span`, exact `sha256`, `kind`,
receiver `member` and a containing `receiver_witness` (`span` and `sha256`).
Spans use half-open Unicode code-point offsets. An audited witness starts at
a known code boundary and proves that the named receiver refers to the native
consumer rather than an application-specific override. It must never be
inferred from a member name or a digest alone. Both caches must match both
ranges. Keep those witnesses and rule instances private.

The token scanner and separate closed-statement grammar must agree on each
literal's span and value. Covered forms are simple DOM/context/listener calls,
canvas paint property assignments with closed operands, and the separator in
a native media element's `type` assignment. Unknown syntax, unmatched caches,
ambiguous matches and missing or duplicate raw occurrences stay pending.
Only unprojected control-storage rows are eligible; locked results, existing
projections, and other inventory rows are left alone. Candidates in the report
carry separate method evidence. The numeric counts distinguish unruled ranges,
failed proofs, and each paired-inventory/ledger check.

Validate the SDK command with the synthetic cache suite:
`python -m unittest tests.test_rpg_maker_mz_cache_reconciliation -v`.
