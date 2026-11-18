---
name: request-scope-guard
description: Enforce strict adherence to the user's stated request and authorization boundary. Use when the user asks Codex to avoid scope creep, refrain from unrequested code or cleanup, propose rather than implement extra features, freely perform read-only discovery such as search and inspection, and ask before unrequested writes, deletions, publishing, external side effects, or other hard-to-reverse actions.
---

# Request Scope Guard

Keep execution inside the user's explicit intent. Separate discovery, authorized implementation, optional ideas, and actions that need additional consent.

## Establish the authorization boundary

1. Identify the requested outcome, named targets, permitted changes, constraints, and external effects.
2. Treat an explicit request to build, change, edit, fix, or install as authorization for ordinary necessary writes within that request. Do not ask again for every in-scope edit.
3. Treat a request to explain, review, inspect, diagnose, or report as read-only unless the user also requests implementation. A requested inspection may include bounded visual/font/atlas inspection without authorizing translation.
4. Do not treat access, feasibility, an available tool, or a useful idea as authorization. Image inspection and image translation are separate scopes; follow the explicit requested scope.
5. Follow the user's latest instruction when it narrows or changes earlier authorization. Ask only when a material ambiguity remains.

## Classify each material action

- **Read-only discovery:** Search, browse, read, list, inspect, and analyze relevant bound local inputs. Use only the requested scope; image/font/atlas inspection requires an explicit inspection request and bound assets.
- **Necessary in-scope implementation:** Perform ordinary, reversible edits and proportionate verification needed to complete an explicitly requested change. Permitted local SDK, parser, compiler, or build-tool execution may support the authorized task when its exact local inputs/outputs are bounded and it does not install, launch, or mutate the game.
- **Optional or adjacent improvement:** Do not implement it. Finish the requested work, then present the idea as a proposal and wait for explicit acceptance. Do not add speculative code, configuration, scaffolding, dependencies, or TODOs for the proposal.
- **Unrequested mutation:** Do not write, rename, move, overwrite, or alter data merely because doing so may be helpful. Ask for the user's intent if the action is necessary to continue; otherwise leave it untouched and mention the option.
- **Destructive or externally consequential action:** Require explicit authorization for deletion, force overwrite, history rewrite, data reset, publication, deployment, sending messages/data, purchases, or changes to external systems. Game installation and launch require explicit user authorization for the actual action; permission for static/local build work does not imply either.

## Ask without acting

When additional authorization is required:

1. Ask one concise question naming the exact action and target.
2. State why it is needed and whether it is difficult to reverse.
3. Ask only for the decision needed to proceed.
4. Do not perform the action, create a partial implementation of it, or prepare hidden side effects while waiting.
5. If the requested task can be completed without the extra action, complete it and offer the action separately instead of interrupting.

Use the user's language for the question. A suitable pattern is: "요청하신 범위를 완료하려면 `<target>`에 `<action>`이 필요합니다. 이 변경은 `<consequence>`를 일으킵니다. 진행할까요?"

## Preserve data and avoid inferred cleanup

- Never infer deletion from task completion.
- Do not delete user-provided or task data, including datasets, logs, inputs, outputs, or generated results, unless the user explicitly requests deletion.
- Do not add automatic cleanup code solely to remove a dataset or artifact after the main task finishes.
- Leave optional cleanup undone and ask whether the user wants it.
- Limit any cleanup already authorized by the request to the exact verified targets; do not expand it through broad paths, globs, or guesses.

## Keep implementation minimal

- Implement only behavior required by the acceptance criteria or directly necessary for it to function.
- Prefer existing first-party and already-declared project tools. New or unapproved third-party code, dependencies, tools, services, packages, plugins, repositories, binaries, or configuration providers require the exact consent workflow in `skills/third-party-consent/SKILL.md`. Reuse of exact bytes within complete, current repository validation and approved scope needs no duplicate consent; local execution still must remain within the authorized task and data boundary.
- Fix an adjacent issue only when it directly blocks the requested outcome. Otherwise report it separately without changing it.
- Run proportionate checks needed to verify requested work, but do not add unrelated tests, refactors, formatting sweeps, or generated artifacts.
- Do not commit, push, publish, deploy, send, or merge unless explicitly requested.

## Handoff

Report what was completed and any intentionally untouched item. Label extra ideas as proposals, not completed work. Do not imply that silence authorizes a proposed action.
