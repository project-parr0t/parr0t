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

`ConsumerUsage` triages unresolved JavaScript literals in the same cache
pass. It separates object keys and values, conditional arms, switch labels,
direct assignments and comparison/compound operators. For a unique
same-scope local or static member, it follows direct reads and distinguishes
indexing, call arguments and comparisons. Mixed writes, cross-scope use
and unknown syntax stay unproven. API names identify candidate sinks, not
receiver types or authority to exclude a string. Report only categorical
counts and require matching classifications at the two cached spans.

Object arguments can be traced one call deep to a unique local function
with a closed positional signature. A property read is tied to the exact
parameter slot, not to equal strings or method names. Indexed writes can
link to later reads of the same scoped receiver and literal, constant, or
unreassigned parameter key. Duplicate writes, competing dynamic keys,
spread arguments, and unbound callees are not authoritative traces. These
are diagnostic links; they do not prove runtime reachability or eligibility.

For each object property value or indexed write, `classify_with_reason`
adds one outcome for its route, including `matched`. Rejected argument
routes distinguish missing direct arguments, unsupported callees or slots,
and parameter writes or missing property reads. Indexed routes distinguish
receiver binding, key support, competing writes, and no read at the same
address versus reads found only before the write. Counts from the private
trial report only agreeing cache outcomes; different outcomes get a separate
disagreement count. These are diagnostics, not exclusion evidence.

Previously unsupported computed keys are counted by syntactic form. The
analyzer can normalize grouped, unescaped string and safe decimal literals
joined by binary `+`, using JavaScript string/number addition semantics.
A folded key still requires a unique write and a later read at the same
address. Nonconstant expressions, calls, and member values cannot establish
an address from syntax alone.

Unsupported index shapes can be split by outer operator and operand kinds
without exposing operand values. Object-value diagnostics distinguish direct
objects, nested properties, and array elements from their outer storage or
argument site. For local bindings and supported positional arguments, reads
must match the whole property path, including the array slot. For every
literal on the path, earlier members and later members with a different
static name cannot overwrite the target definition. Compare source-order keys
for ordinary properties, shorthand, methods, accessors, and supported computed
or escaped names. Later same-key, unresolved computed or unsupported entries,
spread, and writes at a path prefix still block a trace. The literal prototype
setter remains outside this diagnostic. A clear key, unmatched route, or
syntactic display call is not a visibility decision.

For local initializers without a direct read, the SDK can carry the remaining
property path through single-assignment aliases and lexical positional
arguments. It supports local function declarations and `const`-bound function
expressions or block arrows with simple parameters. Matching requires the
exact remaining path and rejects competing writes; recursion and fanout are
bounded. A capture can cross recognized lexical blocks and simple immediately
invoked or directly called closures, using the call site rather than the
function body's source position to check initialization order. Shadowed or
ambiguous bindings, unresolved activations, dynamic access, and returns stay
diagnostic boundaries. A syntactic read does not prove runtime reachability
or authorize excluding a string.

## Locale-first text eligibility

`game_l10n.rpg_maker_mz.text_inventory.materialize` takes a bounded locale
probe and a lazy display-observation supplier. An applicable, source-bound
`LocaleCatalogProof` uses only its exact JSON value addresses. It can patch
an existing catalog or construct a loader-supported new catalog from a
source template. New catalogs require a complete enumeration and all bound
translations. The caller must treat their destinations as create-only.
An inconclusive or invalid proof never falls through to display extraction.

Only `LocaleUnavailable` opens the display supplier. The native rules cover
observed map/common-event/troop text and choice slots, and exact literal
arguments in the supported direct MZ render-call syntax with source-bound
native receiver evidence. They do not scan arbitrary plugin parameters,
infer types from a method name, or count unresolved routes as targets.
The caller provides engine observations and independent discovery; this
module checks their bindings and emits the Core inventory wire format.
It preserves JSON keys, MZ control tokens and bytes outside the selected
script spans. Game-specific loader and receiver evidence stays in the
workspace, not in the SDK.

Validate the SDK with:
`python -m unittest tests.test_rpg_maker_mz_text_inventory tests.test_rpg_maker_mz_cache_reconciliation tests.test_rpg_maker_mz_consumer_usage -v`.
