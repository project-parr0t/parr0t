# Native host API broker

This is an opt-in transport for an **existing native agent host API**. It keeps semantic workers behind deterministic code when a host cannot expose nested delegation to a relay agent. It does not add a missing tool to Codex App, start another Codex process, provide a model API substitute, or run an API service itself. A URL and a capability declaration alone are insufficient: the selected route must pass a live synthetic worker probe.

The default remains `collaboration` with a fresh disposable relay. If that relay has no actual nested dispatch tool, preserve the task and report `E_NATIVE_DISPATCH_UNAVAILABLE`. Never use direct root-to-semantic dispatch as an automatic compatibility mode. The user-selected provider preset and fallback ordering remain authoritative on either transport.

## Select an existing host

Only select this transport when the user has authorized that native host. Its service must implement the protocol below and have access to the same private workspace. Registering a host does not configure provider credentials, start translation, probe fallback routes, or change a provider preset.

```text
game-l10n configure-native-host --workspace <workspace> --host native-service --endpoint https://native-host.example.invalid/parr0t --api-key-env PARR0T_NATIVE_HOST_TOKEN
game-l10n native-runtime --workspace <workspace> --host native-service --transport native-host-api --model native-luna=<exact-model-id> --model native-sol=<exact-model-id>
game-l10n probe-native-host --workspace <workspace> --route native-luna
game-l10n probe-native-host --workspace <workspace> --route native-luna --live
```

The first probe inspects capabilities without starting a worker. `--live` explicitly starts one synthetic native worker using only a random canary. It can incur host usage; it does not contain game text. Probe `native-sol` only when that route is reached. Identical completed probes reuse their evidence. Probes bind the host profile, exact model, route, and control-plane generation.

Credentials are environment references only. Profiles are immutable; a different endpoint uses another host ID. HTTPS is required for credentials outside loopback. Redirects are rejected. Unknown hosts require both exact role mappings; no model inheritance, wildcard, or guessed host prefix is allowed. Rebinding the same host preserves approved custom mappings and the selected transport unless explicitly changed.

After the normal `prepare-data-plane-task` or `native-pass-step`, run the returned `broker_command` argument array. Do not spawn an LLM relay for an `executor: native-host-api` dispatch:

```text
game-l10n run-native-broker --workspace <workspace> --relay <core-generated-relay-path>
```

The broker validates the launch contract, private task hashes and path bindings, records actual returned worker IDs/models, drives context slots and the finalizer in order, then calls the existing core sealer. `native-pass-step` still owns import, splitting, retry, and routing. The broker does not advance stages or modify manifest IDs. Its CLI returns only a typed receipt or categorical pending/error metadata; the existing random-port live UI displays aggregate counts only.

## Host protocol: `parr0t.native-host-api.v1`

All paths below are relative to the registered endpoint. Return HTTP 200 with a JSON object for protocol responses. Transport/HTTP errors are operational failures, never content judgments. Do not return private text in errors, progress, worker IDs, model IDs, or any parent-facing metadata. The broker drops unknown response fields and never logs response bodies.

`GET /capabilities` must return:

```json
{
  "protocol": "parr0t.native-host-api.v1",
  "host": "native-service",
  "worker_runtime": "native_agent_api",
  "independent_workers": true,
  "broker_only_delivery": true,
  "fresh_history": true,
  "explicit_models": true,
  "atomic_wave_start": true,
  "idempotent_requests": true,
  "shared_workspace": true,
  "max_workers": 4,
  "models": ["exact-luna-id", "exact-sol-id"]
}
```

These capabilities are obligations for the host implementation. Each worker must be a fresh independent native-agent session, with no inherited chat history and no delegation tools. The service supplies the authorized native tools and executes only the bound task, with game roots read-only. Worker replies, provider exceptions, refusal explanations, agent trees, and session logs go exclusively to the broker/service's private data plane, never to the orchestration chat. The host is trusted infrastructure; a canary verifies dispatch and model selection, not an adversarial host's privacy guarantees.

`POST /waves` receives:

```json
{
  "protocol": "parr0t.native-host-api.v1",
  "request_id": "wave-<stable-digest>",
  "model": "exact-model-id",
  "history_mode": "none",
  "delegation_allowed": false,
  "reply_delivery": "broker_only",
  "tasks": []
}
```

Admit all supplied sessions atomically or start none and return `status: capacity_unavailable` with the same `request_id`. Reserve capacity before admission. A repeated `request_id` must return the same execution and worker IDs; a different payload for that ID is an error, never another start. Do not silently fall back to a different model.

Two task kinds are supported:

- `probe`: `task_key`, `kind`, `canary`. Start a worker on the exact model with only the canary. Return its SHA-256 over the canonical JSON serialization used by `game_l10n.store.stable_hash` as `probe_digest`, and the host-observed exact model. Do not echo arbitrary worker prose.
- `native_task`: `task_key`, `kind`, absolute `workspace`, workspace-relative `task` and `result`, optional `manifest`, plus `slot` and `finalizer`. Native translation/review workers follow the bound manifest and task, write the canonical ordered JSONL, and end. Data-plane workers follow the immutable private task. A non-null context slot executes only its claim/process/commit loop; `finalizer: true` runs the task's core finalizer after all slots end. Ordinary workers write the existing control envelope. Optional record counts are scheduling statistics, not permission to change scope.

The host must not read the entire source to reconstruct a prompt, create a new response schema, or send raw semantic responses in this protocol. Per-record failures belong in canonical results/queues; a host/session failure is `failed` for that exact worker and must not discard peer results.

Responses and `GET /waves/by-request/<request_id>` use this shape:

```json
{
  "request_id": "wave-<stable-digest>",
  "status": "completed",
  "workers": [
    {
      "task_key": "task-<digest>",
      "worker_id": "opaque-stable-session-id",
      "model": "exact-model-id",
      "observed_model": "exact-model-id",
      "status": "completed"
    }
  ],
  "usage": {"input_tokens": 100, "provider_cost_usd": 0.001}
}
```

Wave states are `queued`, `running`, `completed`; worker states are `queued`, `running`, `completed`, `failed`. Every admitted response contains every task key and a unique stable session ID. `observed_model` may be null while evidence is unavailable; an exact observation may enrich an existing session, never replace it. Terminal wave status requires every worker terminal. Retain execution metadata durably across client and service restarts. Missing execution lookup returns the same `request_id`, `status: not_found`, and `retry_safe: true` **only when the host can prove no execution exists**. Otherwise omit `retry_safe`; the broker preserves an uncertain launch and does not start a duplicate.

Optional `usage` is cumulative for that request, numerical only, and contains actual input tokens and actual USD cost. Do not invent zero for unknown usage. Complete native-wave telemetry is added to the existing core ledger once per request ID; missing telemetry remains unmeasured. Probe and data-plane usage stays in private broker execution metadata. This initial transport does not implement host-enforced explicit per-job budgets: a manifest with a user-approved bulk budget is rejected before new work with `E_NATIVE_HOST_EXPLICIT_BUDGET_UNSUPPORTED`. It never silently drops that limit or asks for a new global limit. Default jobs acquire no usage ceiling.

## Recovery and capacity

The broker persists intent before network calls and uses an OS lock per execution. After interruption, rerun the same command. It reconciles the same request ID, reuses completed groups, and never blindly resubmits an uncertain start. A changed task/model/profile cannot adopt another execution's ID. No implicit generation timeout is added; interruption preserves resumable state.

New native task plans clamp parallelism to the probed host capacity. Existing wider immutable native waves run in bounded groups; their wave receipt is sealed once, after all groups finish. Admission group bindings persist across restarts. Context plans clamp capacity before creating slots; an already-bound context plan requiring more simultaneous slots than a new host can supply stays pending rather than violating its all-slots-start-before-any-end rule. Restore sufficient capacity or recover an ended unstarted attempt through the existing core procedure.

`E_NATIVE_HOST_PROBE_REQUIRED`, `E_NATIVE_HOST_CAPACITY_UNAVAILABLE`, `E_NATIVE_HOST_LAUNCH_UNCERTAIN`, and transport failures describe host state. Preserve attempts, caches, queues and scope. Do not count these as translation refusals, content exclusions, provider exhaustion, or permission to relax isolation.
