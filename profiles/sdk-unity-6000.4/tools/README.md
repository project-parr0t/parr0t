# Unity 6000.4 explicit binding tools

Read `docs/unity-6000.4.md` in the SDK checkout for the private plan schema,
environment, dependencies, commands and limits.

- `unpack.py SOURCE_ROOT OUTPUT_DIRECTORY` validates explicit private bindings
  and produces an ordered inventory with incomplete discovery coverage.
- `pack.py COMPLETED_RESULTS_JSON OUTPUT_DIRECTORY` applies exact-bound typed/raw
  strings and Mono/CIL literals, optionally integrates explicit TMP fallback
  groups, verifies reconstruction and uses the existing core verified-pack API.
- `unity_serialized.py` and `unity_fonts.py` expose in-memory byte operations.
- `ManagedLiteralWriter.cs` is compiled only in a private tooling directory with
  Mono.Cecil 0.11.6; no compiled executable is shipped.

Require UnityPy 1.25.4 in the private environment. Keep bindings, copied assemblies,
schemas, fonts, payloads and observations outside the SDK. No tool installs,
launches, scans images or infers translation eligibility. Binary/static success
does not establish rendering or complete text/UI coverage.
