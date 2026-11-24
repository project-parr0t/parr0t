import { test } from "bun:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import createLifecycle, { publishHostModels } from "../src/game_l10n/templates/native-relay-lifecycle.mjs";

async function fixture(run, { wave = false } = {}) {
  const workspace = mkdtempSync(path.join(os.tmpdir(), "parr0t-lifecycle-state-"));
  try {
    const taskPath = path.join(workspace, "synthetic-task.json");
    const taskBytes = JSON.stringify({ schema: "synthetic-control-only" });
    writeFileSync(taskPath, taskBytes);
    const taskSha = createHash("sha256").update(taskBytes).digest("hex");
    const relayPath = path.join(workspace, "relay.json");
    const keys = wave ? ["tasks/task-0001.jsonl", "tasks/task-0002.jsonl"] : ["worker-0", "worker-1", "finalizer"];
    const dispatches = Object.fromEntries(keys.map((key, slot) => [key, {
      model: "synthetic/model:high", task_path: taskPath, task_sha256: taskSha, slot: wave ? key : slot,
      worker_prompt: `PARR0T_OMP_TASK=${key}\nSynthetic task ${taskPath} ${taskSha}`,
    }]));
    const relay = {
      ...(wave ? { schema: "parr0t.native-wave-relay.v1" } : {}),
      launch_contract: { model: "synthetic/model:high", worker_dispatches: dispatches },
      execution_plan: { tracking_required: !wave },
      command_argv: {
        "capability-available": ["preflight"],
        "worker-started": Object.fromEntries(keys.map(key => [key, ["start", key, "<worker-id>"]])),
        "relay-events": {
          "worker-started:0": ["event", "start:0"], "worker-started:1": ["event", "start:1"],
          "worker-ended:0": ["event", "end:0"], "worker-ended:1": ["event", "end:1"],
          "finalizer-started": ["event", "finalizer-start"], "finalizer-ended": ["event", "finalizer-end"],
        },
      },
    };
    const relayBytes = JSON.stringify(relay);
    const relaySha = createHash("sha256").update(relayBytes).digest("hex");
    writeFileSync(relayPath, relayBytes);
    const started = new Set();
    const events = [];
    let failPreflight = false;
    async function executeCore(argv) {
      if (argv[0] === "preflight" && failPreflight) return { code: 1, stdout: "", killed: false };
      if (argv[0] === "start") started.add(argv[1]);
      if (argv[0] === "event") {
        if (argv[1].startsWith("end:")) assert.ok(started.has(argv[1].replace("end:", "worker-")));
        events.push(argv[1]);
      }
      return { code: 0, killed: false, stdout: JSON.stringify({ issue_codes: [], unstarted_tasks: keys.filter(key => !started.has(key)) }) };
    }
    const make = () => createLifecycle({ workspace: () => workspace, relayPath: () => relayPath, executeCore });
    const parent = { agent: { id: "SyntheticRelay", kind: "sub", depth: 1, parentId: "Main" } };
    const lifecycle = make();
    await lifecycle.before_agent_start({ prompt: "Synthetic relay" }, parent);
    async function dispatch(keys, id) {
      return lifecycle.tool_call({ toolName: "task", toolCallId: id,
        input: { tasks: keys.map(key => ({ task: `PARR0T_OMP_TASK=${key}` })) } }, parent);
    }
    async function enter(key, id) {
      const worker = make();
      const ctx = { agent: { id, kind: "sub", depth: 2, parentId: parent.agent.id } };
      await worker.before_agent_start({ prompt: dispatches[key].worker_prompt }, ctx);
      return { end: event => worker.agent_end(event, ctx) };
    }
    const timingDir = path.join(workspace, "logs", "native-wave-timing");
    await run({ dispatch, enter, lifecycle, make, parent, events, started, taskPath, taskBytes, keys, relaySha, timingDir,
      failPreflight: value => { failPreflight = value; } });
  } finally { rmSync(workspace, { recursive: true, force: true }); }
}

test("wave timing uses only settled lifecycle events and deduplicates the exact binding", async () => {
  await fixture(async ({ dispatch, enter, lifecycle, make, parent, keys, relaySha, timingDir }) => {
    assert.equal((await dispatch(keys, "dispatch")).block, undefined);
    const early = make();
    await early.before_agent_start({ prompt: "Synthetic relay" }, parent);
    await early.agent_end({}, parent);
    assert.equal(existsSync(timingDir), false);
    const terminal = {
      get provider_generation_ms() { throw new Error("must not infer generation time"); },
      get messages() { throw new Error("must not inspect history"); },
    };
    for (const [index, key] of keys.entries()) {
      const worker = await enter(key, `SyntheticWorker${index}`);
      await worker.end({ willContinue: true });
      await worker.end(terminal);
    }
    const relayEvent = path.join(timingDir, `${relaySha}.json`);
    await lifecycle.agent_end({ awaitingAsyncWork: true }, parent);
    assert.equal(existsSync(relayEvent), false);
    await lifecycle.agent_end(terminal, parent);
    const first = readFileSync(relayEvent, "utf8");
    const replay = make();
    await replay.before_agent_start({ prompt: "Synthetic replay" }, parent);
    await replay.agent_end(terminal, parent);
    assert.equal(readFileSync(relayEvent, "utf8"), first);
    const records = readdirSync(timingDir).map(name => JSON.parse(readFileSync(path.join(timingDir, name), "utf8")));
    assert.deepEqual(records.map(record => record.event).sort(), ["relay_ended", "worker_ended", "worker_ended"]);
    assert.deepEqual(records.filter(record => record.task_sha256).map(record => record.task_sha256).sort(),
      keys.map(key => createHash("sha256").update(key).digest("hex")).sort());
    for (const record of records) {
      assert.equal(record.schema, "parr0t.native-wave-timing.v1");
      assert.equal(record.relay_sha256, relaySha);
      assert.ok(Number.isInteger(record.elapsed_ms) && record.elapsed_ms >= 0);
      assert.equal(record.provider_generation_ms, null);
    }
  }, { wave: true });
});

test("context workers end independently and finalizer waits for every terminal event", async () => {
  await fixture(async ({ dispatch, enter, lifecycle, parent, events }) => {
    assert.equal((await dispatch(["worker-0", "worker-1"], "batch")).block, undefined);
    const first = await enter("worker-0", "First");
    await first.end({ willContinue: false, awaitingAsyncWork: false });
    assert.deepEqual(events, ["start:0", "end:0"]);
    assert.equal((await lifecycle.tool_call({ toolName: "parr0t_process", input: { argv: ["seal-data-plane-task"] } }, parent)).block, true);
    const second = await enter("worker-1", "Second");
    assert.deepEqual(events, ["start:0", "end:0", "start:1"]);
    assert.equal((await dispatch(["finalizer"], "too-early")).block, true);
    await second.end({ willContinue: false, awaitingAsyncWork: true });
    assert.equal((await dispatch(["finalizer"], "still-early")).block, true);
    await second.end({ willContinue: false, awaitingAsyncWork: false });
    assert.equal((await dispatch(["finalizer"], "ready")).block, undefined);
    const finalizer = await enter("finalizer", "Finalizer");
    await finalizer.end({ willContinue: false, awaitingAsyncWork: false });
    assert.deepEqual(events, ["start:0", "end:0", "start:1", "end:1", "finalizer-start", "finalizer-end"]);
    assert.equal(await lifecycle.tool_call({ toolName: "parr0t_process", input: { argv: ["seal-data-plane-task"] } }, parent), undefined);
  });
});

test("pre-spawn failures remain recoverable and changed task bytes never record a start", async () => {
  await fixture(async ({ dispatch, enter, lifecycle, started, taskPath, taskBytes, failPreflight }) => {
    failPreflight(true);
    assert.equal((await dispatch(["worker-0"], "denied")).block, true);
    failPreflight(false);
    assert.equal((await dispatch(["worker-0"], "retry")).block, undefined);
    writeFileSync(taskPath, "changed synthetic control");
    await assert.rejects(() => enter("worker-0", "Unstarted"), /task digest changed/);
    assert.equal(started.size, 0);
    lifecycle.tool_result({ toolCallId: "retry", isError: true });
    writeFileSync(taskPath, taskBytes);
    assert.equal((await dispatch(["worker-0"], "fresh")).block, undefined);
    await enter("worker-0", "Actual");
    assert.equal((await dispatch(["worker-0"], "duplicate")).block, true);
    assert.deepEqual([...started], ["worker-0"]);
    assert.equal(readFileSync(taskPath, "utf8"), taskBytes);
  });
});

test("partial batch admission failure cannot strand a completed sibling", async () => {
  await fixture(async ({ dispatch, enter, lifecycle, parent, events }) => {
    assert.equal((await dispatch(["worker-0", "worker-1"], "partial-batch")).block, undefined);
    const first = await enter("worker-0", "First");
    lifecycle.tool_result({ toolCallId: "partial-batch", isError: true });
    await first.end({ willContinue: false, awaitingAsyncWork: false });
    assert.deepEqual(events, ["start:0", "end:0"]);
    assert.equal((await dispatch(["finalizer"], "failed-sibling")).block, true);
    assert.equal(await lifecycle.tool_call({
      toolName: "parr0t_process", input: { argv: ["seal-data-plane-task"] },
    }, parent), undefined);
    assert.equal((await dispatch(["worker-1"], "recover-unstarted")).block, undefined);
    const second = await enter("worker-1", "Second");
    await second.end({ willContinue: false, awaitingAsyncWork: false });
    assert.equal((await dispatch(["worker-0"], "never-replay-completed")).block, true);
    assert.equal((await dispatch(["finalizer"], "ready")).block, undefined);
    const finalizer = await enter("finalizer", "Finalizer");
    await finalizer.end({ willContinue: false, awaitingAsyncWork: false });
    assert.deepEqual(events, ["start:0", "end:0", "start:1", "end:1", "finalizer-start", "finalizer-end"]);
  });
});

test("public host catalog snapshot contains resolution metadata only and never grants authorization", () => {
  const workspace = mkdtempSync(path.join(os.tmpdir(), "parr0t-public-models-"));
  try {
    const model = { provider: "provider", id: "literal-model@upstream:high", apiKey: "PRIVATE_CREDENTIAL", headers: { authorization: "PRIVATE_HEADER" } };
    const ctx = {
      agent: { kind: "main", depth: 0 },
      sessionManager: { getSessionId: () => "synthetic-main-session" },
      modelRegistry: {
        getAll: () => [model, model, { provider: "other", id: "literal-model@upstream:high" }],
        getAvailable() { throw new Error("must not query credentials or availability"); },
        getApiKey() { throw new Error("must not query credentials"); },
      },
      model,
    };
    publishHostModels(ctx, workspace);
    const file = path.join(workspace, ".game-l10n/native/host-models.json");
    const bytes = readFileSync(file, "utf8");
    assert.deepEqual(JSON.parse(bytes), {
      schema: "parr0t.native-host-models.v1", host: "omp",
      session_id: "synthetic-main-session", process_id: process.pid,
      models: [
        { provider: "provider", id: model.id, selector: `provider/${model.id}` },
        { provider: "other", id: model.id, selector: `other/${model.id}` },
      ],
      current_model: `provider/${model.id}`,
    });
    assert.ok(!bytes.includes("PRIVATE"));
    publishHostModels({ ...ctx, agent: { kind: "sub", depth: 1 } }, workspace);
    assert.equal(readFileSync(file, "utf8"), bytes);
    publishHostModels({ ...ctx, modelRegistry: undefined }, workspace);
    assert.equal(existsSync(file), false);
  } finally { rmSync(workspace, { recursive: true, force: true }); }
});
