import { test } from "bun:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import createLifecycle, { publishHostModels } from "../src/game_l10n/templates/native-relay-lifecycle.mjs";

async function fixture(run) {
  const workspace = mkdtempSync(path.join(os.tmpdir(), "parr0t-lifecycle-state-"));
  try {
    const taskPath = path.join(workspace, "synthetic-task.json");
    const taskBytes = JSON.stringify({ schema: "synthetic-control-only" });
    writeFileSync(taskPath, taskBytes);
    const taskSha = createHash("sha256").update(taskBytes).digest("hex");
    const relayPath = path.join(workspace, "relay.json");
    const keys = ["worker-0", "worker-1", "finalizer"];
    const dispatches = Object.fromEntries(keys.map((key, slot) => [key, {
      model: "synthetic/model:high", task_path: taskPath, task_sha256: taskSha, slot,
      worker_prompt: `PARR0T_OMP_TASK=${key}\nSynthetic task ${taskPath} ${taskSha}`,
    }]));
    const relay = {
      launch_contract: { model: "synthetic/model:high", worker_dispatches: dispatches },
      execution_plan: { tracking_required: true },
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
    writeFileSync(relayPath, JSON.stringify(relay));
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
    await run({ dispatch, enter, lifecycle, parent, events, started, taskPath, taskBytes,
      failPreflight: value => { failPreflight = value; } });
  } finally { rmSync(workspace, { recursive: true, force: true }); }
}

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
