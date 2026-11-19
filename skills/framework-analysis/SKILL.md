---
name: framework-analysis
description: Apply evidence-first rules when diagnosing, improving, or managing analysis of Parr0t in framework analysis mode.
---

# Parr0t framework analysis

Apply this skill in analysis mode as the analysis manager (the root coordinating the work) or as a diagnostic or improvement agent. The manager binds each assignment to this skill, the authorized scope, and the evidence it may use. This skill grants no additional access, execution, or publication permission. Follow `docs/control-plane-contract.md` for lifecycle and approved log snapshots, and `skills/request-scope-guard/SKILL.md` for scope and external effects.

1. **State feasibility honestly.** Separate what the actual host or code supports, what requires a missing prerequisite, and what the available evidence cannot establish. Do not present a guess, workaround, synthetic result, or unverified capability as a working solution. Report an unsupported or blocked operation as such.
2. **Do not implement unnecessary backward compatibility.** Use the current Core and SDK. Preserve or explicitly migrate validated checkpoints when required; do not restore obsolete runtimes or add aliases, duplicate paths, or compatibility branches without an established requirement.
3. **Do not invent defects.** Distinguish an observed problem from a hypothesis and missing telemetry. If no problem is found, say so and name the scope of the check. The manager must not demand a minimum number of findings or turn an unconfirmed finding into a fix.
4. **Add only necessary tests.** Reuse existing coverage. Add a focused regression when an otherwise uncovered behavior can plausibly fail, or when the user asks for one. Do not add tests of wording, incidental wiring, or duplicate coverage. Run the relevant verification and report only what it proves.
