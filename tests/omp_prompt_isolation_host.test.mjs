import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import promptIsolation from "../src/game_l10n/templates/omp-prompt-isolation.mjs";

// Opt-in, already-selected host only. Resolve its declared public exports rather
// than assuming its installation location or acquiring any package.
async function publicHostModules(root) {
  const manifest = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8"));
  assert.equal(manifest.name, "@oh-my-pi/pi-coding-agent");
  function publicExport(subpath) {
    let entry = manifest.exports[subpath];
    let replacement;
    if (entry === undefined) {
      const match = Object.keys(manifest.exports)
        .filter(key => key.includes("*"))
        .sort((a, b) => b.length - a.length)
        .find(key => {
          const [head, tail] = key.split("*");
          return subpath.startsWith(head) && subpath.endsWith(tail);
        });
      assert.ok(match, `Selected host does not export ${subpath}`);
      const [head, tail] = match.split("*");
      replacement = subpath.slice(head.length, tail ? -tail.length : undefined);
      entry = manifest.exports[match];
    }
    const target = typeof entry === "string" ? entry : entry.import;
    assert.equal(typeof target, "string", `Selected host has no import export for ${subpath}`);
    return import(pathToFileURL(path.resolve(root, replacement === undefined ? target : target.replace("*", replacement))).href);
  }
  const [extensions, loader, events, sessions, settings, registry, lifecycle, bus, write, bash] = await Promise.all([
    publicExport("./extensibility/extensions"),
    publicExport("./extensibility/extensions/loader"),
    publicExport("./utils/event-bus"),
    publicExport("./session/session-manager"),
    publicExport("./config/settings"),
    publicExport("./registry/agent-registry"),
    publicExport("./registry/agent-lifecycle"),
    publicExport("./irc/bus.js"),
    publicExport("./tools/write"),
    publicExport("./tools/bash"),
  ]);
  return { ...extensions, ...loader, ...events, ...sessions, ...settings, ...registry, ...lifecycle, ...bus, ...write, ...bash };
}

async function runIntegration() {
  const root = process.env.PARR0T_OMP_HOST_PACKAGE;
  assert.ok(root, "PARR0T_OMP_HOST_PACKAGE must select an existing host package root");
  const {
    ExtensionRunner, ExtensionRuntime, ExtensionToolWrapper, loadExtensionFromFactory,
    wrapRegisteredTool, EventBus, SessionManager, Settings, AgentRegistry,
    AgentLifecycleManager, IrcBus, WriteTool, BashTool, MAIN_AGENT_ID,
  } = await publicHostModules(root);
  const workspace = mkdtempSync(path.join(os.tmpdir(), "parr0t-real-host-synthetic-"));
  const relative = ".game-l10n/native/relay/synthetic-wave.json";
  mkdirSync(path.dirname(path.join(workspace, relative)), { recursive: true });
  writeFileSync(path.join(workspace, relative), JSON.stringify({ schema: "parr0t.native-wave-relay.v1", task_bindings: [] }));
  const marker = `PARR0T_OMP_RELAY=${relative}`;
  // This subprocess owns every registry, bus and in-memory session. No models,
  // session discovery, credentials, persisted roster, or real recipients exist.
  const settings = Settings.isolated({ "tools.approvalMode": "yolo", "lsp.enabled": false });
  const registry = new AgentRegistry();
  const lifecycle = new AgentLifecycleManager(registry);
  const bus = new IrcBus(registry, lifecycle);
  IrcBus.installGlobal(bus);
  const sessions = [];
  const deliveries = [];
  const observations = [];
  let sequence = 0;

  async function makeSession(id, kind, depth, parentId) {
    const manager = SessionManager.inMemory(workspace);
    const identity = { id, name: id, kind, depth, parentId };
    const eventBus = new EventBus();
    const runtime = new ExtensionRuntime();
    runtime.flagValues.set("parr0t-workspace", workspace);
    const spawned = [];
    const dispatchedTasks = [];
    const extension = await loadExtensionFromFactory(pi => {
      promptIsolation(pi);
      // A synthetic, model-free consumer of real host task dispatch. Its input
      // is exactly what ExtensionToolWrapper passes after extension rewriting.
      pi.registerTool({
        name: "task",
        label: "Synthetic nested session",
        description: "Create only controlled in-memory descendants without a model.",
        parameters: pi.zod.object({
          context: pi.zod.string().optional(),
          tasks: pi.zod.array(pi.zod.object({
            task: pi.zod.string().optional(),
            steps: pi.zod.array(pi.zod.string()).optional(),
          })),
        }),
        async execute(_callId, input) {
          dispatchedTasks.push(input);
          for (const task of input.tasks) {
            const worker = await makeSession(`${id}.Worker${spawned.length + 1}`, "sub", depth + 1, id);
            // Shared context is intentionally NOT replayed as the child's first
            // user prompt: this is the consumer-visible marker-loss risk.
            await worker.runner.emitBeforeAgentStart(task.task ?? task.steps?.[0] ?? "Synthetic task", undefined, []);
            spawned.push(worker);
          }
          return { content: [{ type: "text", text: "Synthetic descendants created." }], details: {} };
        },
      });
    }, workspace, eventBus, runtime, "synthetic-prompt-boundary");
    // No ModelRegistry is constructed: context model queries are lazy and this
    // offline fixture never invokes one. The runner, API, schemas and wrappers
    // below are the selected host implementations, not a mocked dispatcher.
    const runner = new ExtensionRunner([extension], runtime, workspace, manager, undefined,
      undefined, settings, undefined, undefined, identity);
    runner.onError(error => { throw new Error(`Unexpected selected-host extension error: ${error.error}`); });
    const toolSession = {
      cwd: workspace, settings, hasUI: false, enableLsp: false, enableIrc: true,
      agentRegistry: registry, taskDepth: depth, sessionManager: manager,
      getAgentId: () => id, getSessionFile: () => null,
      getSessionId: () => manager.getSessionId(),
    };
    const context = { autoApprove: true, cwd: workspace, settings, sessionManager: manager };
    function wrap(tool) { return new ExtensionToolWrapper(tool, runner, identity); }
    const write = wrap(new WriteTool(toolSession));
    const bash = wrap(new BashTool(toolSession));
    function registered(name) {
      const tool = runner.getAllRegisteredTools().find(entry => entry.definition.name === name);
      assert.ok(tool, `Selected host did not register ${name}`);
      return wrap(wrapRegisteredTool(tool, runner));
    }
    const session = {
      id, runner, manager, spawned, dispatchedTasks, context,
      write: input => write.execute(`synthetic-${++sequence}`, input, undefined, undefined, context),
      bash: input => bash.execute(`synthetic-${++sequence}`, input, undefined, undefined, context),
      task: input => registered("task").execute(`synthetic-${++sequence}`, input, undefined, undefined, context),
      process: input => registered("parr0t_process").execute(`synthetic-${++sequence}`, input, undefined, undefined, context),
      processSchema: () => registered("parr0t_process").parameters,
    };
    sessions.push(session);
    registry.register({ id, displayName: id, kind, parentId, status: "running", session: {
      async deliverIrcMessage(message) { deliveries.push({ recipient: id, message }); return "injected"; },
      emitIrcRelayObservation(record) { observations.push({ recipient: id, record }); },
    } });
    return session;
  }

  async function deny(session, recipient) {
    await assert.rejects(session.write({ path: `agent://${recipient}`, content: "synthetic denied delivery" }),
      /PARR0T_OMP_PROMPT_ISOLATION/);
    assert.equal(bus.unreadCount(recipient), 0);
    assert.equal(bus.sentSince(session.id, recipient, 0), false);
  }

  try {
    const parent = await makeSession(MAIN_AGENT_ID, "main", 0);
    const relay = await makeSession("SyntheticRelay", "sub", 1, parent.id);
    const peer = await makeSession("SyntheticPeer", "sub", 1, parent.id);
    await parent.runner.emit({ type: "session_start" });
    // Positive control proves the real write tool, URL handler and bus can
    // reach a controlled recipient; a later zero is not an absent endpoint.
    const rootDelivery = await parent.write({ path: `agent://${relay.id}`, content: "synthetic root control" });
    assert.notEqual(rootDelivery.isError, true);
    assert.equal(deliveries.length, 1);
    assert.equal(deliveries[0].recipient, relay.id);
    deliveries.length = 0;
    observations.length = 0;

    // Fresh children are already restricted, before either lifecycle event.
    await deny(relay, parent.id);
    await deny(relay, peer.id);
    await relay.bash({ command: "printf synthetic > agent://Main" }).then(
      () => assert.fail("Embedded bash must be denied"),
      error => assert.match(error.message, /PARR0T_OMP_PROMPT_ISOLATION/),
    );
    // Marker present ONLY in shared context cannot turn an unmarked relay or
    // its depth-two child into a communication/diagnostic route.
    await relay.task({ context: marker, tasks: [{ task: "Synthetic unmarked diagnostic task" }] });
    const unmarked = relay.spawned[0];
    await deny(unmarked, parent.id);
    await deny(unmarked, peer.id);
    await deny(unmarked, relay.id);
    await unmarked.task({ tasks: [{ steps: ["Synthetic unmarked third-level task"] }] });
    await deny(unmarked.spawned[0], parent.id);

    // Once explicitly bound, task and steps reach the consumer with their own
    // standalone marker; propagation must not rely on the shared context.
    await relay.runner.emitBeforeAgentStart(`${marker}\nSynthetic bound relay`, undefined, []);
    await relay.task({ context: marker, tasks: [
      { task: "Synthetic bound worker" },
      { steps: ["Synthetic first step", "Synthetic second step"] },
    ] });
    const boundDispatch = relay.dispatchedTasks.at(-1);
    assert.ok(boundDispatch.tasks[0].task.startsWith(`${marker}\n`));
    assert.ok(boundDispatch.tasks[1].steps.every(step => step.startsWith(`${marker}\n`)));
    for (const worker of relay.spawned.slice(1)) {
      await deny(worker, parent.id);
      await deny(worker, peer.id);
    }
    assert.deepEqual(deliveries, []);
    assert.deepEqual(observations, []);

    // Native Python must run via the registered extension's real pi.exec,
    // preserve argv, and treat URL-like data as data rather than host routing.
    const python = process.env.PARR0T_OMP_PYTHON ?? "python";
    const code = "import json,os,sys; print(json.dumps({'cwd':os.getcwd(),'argument':sys.argv[1],'data':'agent://Main'})); sys.stderr.buffer.write(b'synthetic stderr\\n'); sys.exit(7)";
    const argument = "synthetic argument with spaces and 'quotes'";
    const argv = [python, "-c", code, argument];
    const schema = unmarked.processSchema();
    assert.equal(schema.safeParse({ argv }).success, true);
    for (const invalid of [{}, { argv: [] }, { argv: [1] }, { argv: "python" }]) {
      assert.equal(schema.safeParse(invalid).success, false);
    }
    for (const cwd of [undefined, path.join(workspace, "synthetic-process-cwd")]) {
      if (cwd) mkdirSync(cwd);
      const result = await unmarked.process({ argv, ...(cwd ? { cwd } : {}) });
      const native = result.details;
      assert.equal(native.code, 7);
      assert.equal(native.killed, false);
      assert.equal(native.stderr, "synthetic stderr\n");
      const output = JSON.parse(native.stdout);
      assert.equal(path.resolve(output.cwd), path.resolve(cwd ?? workspace));
      assert.equal(output.argument, argument);
      assert.equal(output.data, "agent://Main");
    }
    const local = path.join(workspace, "synthetic-local.txt");
    const content = "Synthetic local data mentions agent://Main and history:// without routing.";
    const localWrite = await unmarked.write({ path: local, content });
    assert.notEqual(localWrite.isError, true);
    assert.equal(readFileSync(local, "utf8"), content);
    assert.deepEqual(deliveries, []);
    assert.deepEqual(observations, []);
    for (const session of sessions) assert.equal(bus.unreadCount(session.id), 0);
  } finally {
    for (const session of sessions) {
      session.runner.clearManagedTimers();
      session.runner.disposeFileFallbacks();
      await session.manager.close();
      registry.unregister(session.id);
    }
    await lifecycle.dispose();
    rmSync(workspace, { recursive: true, force: true });
  }
}

if (process.argv.includes("--parr0t-real-host-worker")) {
  await runIntegration();
} else {
  const { test } = await import("bun:test");
  const realHostTest = process.env.PARR0T_OMP_HOST_PACKAGE ? test : test.skip;
  realHostTest("real selected host isolates nested sessions and executes native argv without model calls", async () => {
    // Host globals and extension registrations never enter the suite's process.
    const worker = Bun.spawn([process.execPath, fileURLToPath(import.meta.url), "--parr0t-real-host-worker"], {
      env: process.env, stdout: "pipe", stderr: "pipe",
    });
    try {
      const [code, stdout, stderr] = await Promise.all([
        worker.exited, new Response(worker.stdout).text(), new Response(worker.stderr).text(),
      ]);
      assert.equal(code, 0, `Selected-host integration failed:\n${stderr}\n${stdout}`);
    } finally {
      if (worker.exitCode === null) worker.kill();
    }
  }, 60_000);
}
