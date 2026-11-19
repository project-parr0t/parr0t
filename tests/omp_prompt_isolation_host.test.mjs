import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

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
  const [extensions, loader, events, sessions, settings, registry, lifecycle, bus, write, bash, read, sdk, models, auth, runtimeInit] = await Promise.all([
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
    publicExport("./tools/read"),
    publicExport("./sdk"),
    publicExport("./config/model-registry"),
    publicExport("./session/auth-storage"),
    publicExport("./modes/runtime-init"),
  ]);
  return { ...extensions, ...loader, ...events, ...sessions, ...settings, ...registry, ...lifecycle, ...bus, ...write, ...bash, ...read, ...sdk, ...models, ...auth, ...runtimeInit };
}

// Cold SDK construction owns the canonical registry, mounted-device state and
// dynamic activation queue; the wrapper fixture below cannot establish those.
async function proveColdStart(host, workspace, bootstrapPath, helperPath) {
  const { createAgentSession, initializeExtensions, loadExtensions, ModelRegistry, AuthStorage, Settings, SessionManager, AgentRegistry, ExtensionToolWrapper } = host;
  const agentDir = process.env.PI_CODING_AGENT_DIR;
  assert.ok(agentDir && path.isAbsolute(agentDir), "The SDK proof requires an isolated agent directory");
  const extensionDirectory = path.join(agentDir, "extensions");
  mkdirSync(extensionDirectory, { recursive: true });
  writeFileSync(path.join(extensionDirectory, "parr0t-native-process.ts"),
    `export { default } from ${JSON.stringify(pathToFileURL(bootstrapPath).href)};\n`);
  const originalControl = process.env.PARR0T_OMP_CONTROL_DIRECTORY;
  const originalWorkspace = process.env.PARR0T_OMP_PROMPT_WORKSPACE;
  delete process.env.PARR0T_OMP_CONTROL_DIRECTORY;
  const networkAttempts = [];
  const denyNetwork = async () => {
    const error = new Error("The model-free selected-host proof forbids network access");
    networkAttempts.push(error.stack);
    throw error;
  };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = denyNetwork;
  try {
    for (const scenario of ["ambient", "helper", "live"]) {
      const explicitHelper = scenario === "helper";
      const liveBootstrap = scenario === "live";
      const cwd = path.join(workspace, `sdk-${scenario}`);
      mkdirSync(cwd);
      const settings = await Settings.loadIsolated({
        cwd, agentDir, inMemory: true,
        overrides: {
          "tools.approvalMode": "yolo", "tools.xdev": true, "lsp.enabled": false,
          "secrets.enabled": false, "enabledModels": ["synthetic-no-provider/no-model"],
          "disabledProviders": ["ollama", "llama.cpp", "lm-studio", "apple"],
          ...(liveBootstrap ? { disabledExtensions: ["extension-module:parr0t-native-process"] } : {}),
        },
      });
      const auth = await AuthStorage.create(":memory:", { usageFetch: denyNetwork });
      const modelRegistry = new ModelRegistry(auth, path.join(agentDir, "models.yml"), {
        settings, cacheDbPath: path.join(cwd, "models.db"), fetch: denyNetwork,
      });
      assert.deepEqual(modelRegistry.getDiscoverableProviders(), [], "No dynamic provider may enter cold startup");
      const manager = SessionManager.inMemory(cwd);
      const originalArgv = [...process.argv];
      if (explicitHelper) process.argv.push("--parr0t-workspace", cwd);
      let startingExtensions = false;
      let session;
      try {
        const created = await createAgentSession({
          cwd, agentDir, settings, sessionManager: manager, modelRegistry,
          agentRegistry: new AgentRegistry(), agentId: `Sdk-${scenario}`,
          additionalExtensionPaths: explicitHelper ? [helperPath] : [],
          noContextFiles: true, contextFiles: [], skills: [], rules: [], promptTemplates: [], slashCommands: [],
          preloadedCustomToolPaths: [], enableMCP: false, enableLsp: false, enableIrc: false,
          skipPythonPreflight: true, cacheWarming: false, autoTitle: false,
          deferSkillDescriptionCompression: true, bindProcessState: false,
          hasUI: true, interactivePrompts: false, disableModelFallback: true,
          assertDispatchModel() { assert.fail("A model-free consumer must never dispatch a model"); },
          systemPrompt(defaultPrompt) {
            // Consumer prompt services may depend on completed extension startup.
            // Late registration must not make startup re-enter those services.
            if (startingExtensions) throw new Error("Prompt resources are unavailable until extension startup completes");
            return defaultPrompt;
          },
        });
        session = created.session;
        assert.deepEqual(created.extensionsResult.errors, []);
        assert.equal(session.model, undefined, "The SDK must stay model-less, not select a credentialed fallback");
        assert.equal(created.extensionsResult.extensions.some(extension =>
          path.basename(extension.path) === "parr0t-native-process.ts"), !liveBootstrap);
        if (explicitHelper) created.extensionsResult.runtime.flagValues.set("parr0t-workspace", cwd);
        startingExtensions = true;
        try {
          await initializeExtensions(session, {
            reportSendError(_action, error) { throw error; },
            reportRuntimeError(error) { throw new Error(`Unexpected SDK extension error: ${error.error}`); },
          });
        } finally {
          startingExtensions = false;
        }
        if (liveBootstrap) {
          const names = ["parr0t_process", "parr0t_framework_mode", "parr0t_session"];
          assert.ok(names.every(name => !session.getToolByName(name)));
          // OMP's live source reconciler loads a new factory, activates the
          // registered tools, and then replays session_start for that source.
          const bootstrap = await loadExtensions(
            [bootstrapPath], cwd, created.extensionsResult.runtime.eventBus, created.extensionsResult.runtime);
          assert.deepEqual(bootstrap.errors, []);
          await session.extensionRunner.addExtensions(bootstrap.extensions);
        }
        const context = { autoApprove: true, cwd, settings, sessionManager: manager };
        let sequence = 0;
        const execute = (name, input, signal) => {
          const tool = session.getToolByName(name);
          assert.ok(tool, `The cold SDK must expose a real ${name} executor`);
          const callable = tool instanceof ExtensionToolWrapper
            ? tool : new ExtensionToolWrapper(tool, session.extensionRunner);
          return callable.execute(`cold-${++sequence}`, input, signal, undefined, context);
        };
        assert.ok(session.getMountedXdevToolNames().length > 0, "The cold host must actually enable deferred-device transport");
        const device = (name, input) => execute("write", { path: `xd://${name}`, content: JSON.stringify(input) });
        const text = result => result.content.filter(part => part.type === "text").map(part => part.text).join("\n");
        for (const name of ["parr0t_process", "parr0t_framework_mode", "parr0t_session"]) {
          assert.ok(session.getActiveToolNames().includes(name), `${name} must be directly essential after activation`);
          assert.equal(session.getMountedXdevToolNames().includes(name), false);
          assert.equal(session.getAllToolNames().filter(candidate => candidate === name).length, 1);
          const docs = await execute("read", { path: `xd://${name}` });
          assert.notEqual(docs.isError, true, text(docs));
          assert.match(text(docs), new RegExp(name));
        }
        const native = await device("parr0t_process", {
          argv: [process.env.PARR0T_OMP_PYTHON ?? "python", "-I", "-B", "-c", "print('cold native transport')"],
        });
        assert.notEqual(native.isError, true, text(native));
        assert.equal(native.details.xdev.tool, "parr0t_process");
        assert.equal(native.details.xdev.inner.stdout.trim(), "cold native transport");
        // No game-l10n.json exists: the real native CLI must reject before any
        // controller/model launcher can run. This is an outcome, not an echo.
        const launch = await device("parr0t_session", {
          workspace: cwd, model: "synthetic-host/selected-model", user_requested: true,
        });
        assert.equal(launch.isError, true);
        assert.equal(launch.details.xdev.inner.exitCode, 2);
        assert.equal(JSON.parse(launch.details.xdev.inner.stderr).issue_code, "E_OMP_CONTROL_WORKSPACE_REQUIRED");
        const noModel = await device("parr0t_session", { workspace: cwd, user_requested: true });
        assert.equal(noModel.isError, true);
        assert.match(text(noModel), /model.*required|model.*unavailable/i);
        writeFileSync(path.join(cwd, "game-l10n.json"), "{}");
        const bindRuntime = async () => {
          const policy = await execute("read", { path: "cfg://modelRoles" });
          assert.notEqual(policy.isError, true, text(policy));
          const bound = await device("parr0t_process", {
            argv: [process.env.PARR0T_OMP_PYTHON ?? "python", "-I", "-B",
              fileURLToPath(new URL("../src/game_l10n/templates/native-python.py", import.meta.url)),
              "native-runtime", "--workspace", cwd, "--host", "omp", "--omp-isolation", "prompt"],
          });
          assert.notEqual(bound.isError, true, text(bound));
          return JSON.parse(bound.details.xdev.inner.stdout);
        };
        const originalPolicy = await bindRuntime();
        assert.match(originalPolicy.host_policy_fingerprint, /^[a-f0-9]{64}$/);
        settings.setModelRole("synthetic-selected", "synthetic-host/selected-model");
        const repairedPolicy = await bindRuntime();
        assert.notEqual(repairedPolicy.host_policy_fingerprint, originalPolicy.host_policy_fingerprint);
        assert.deepEqual(repairedPolicy.models, originalPolicy.models);
        const mode = { workspace: cwd, mode: "pause", user_requested: true };
        const changed = await device("parr0t_framework_mode", mode);
        assert.notEqual(changed.isError, true, text(changed));
        assert.equal(changed.details.xdev.tool, "parr0t_framework_mode");
        assert.equal(changed.details.xdev.inner.exitCode, 0);
        const statePath = path.join(cwd, ".game-l10n/framework-session.json");
        assert.equal(JSON.parse(readFileSync(statePath, "utf8")).mode, "pause");
        assert.equal((await execute("parr0t_framework_mode", { ...mode, mode: "localization" })).isError, false);
        assert.equal(JSON.parse(readFileSync(statePath, "utf8")).mode, "localization");
        const unrequested = () => device("parr0t_framework_mode", { ...mode, user_requested: false });
        if (explicitHelper) await assert.rejects(unrequested, /PARR0T_FRAMEWORK_APPROVAL/);
        else assert.equal((await unrequested()).isError, true);
        assert.equal(JSON.parse(readFileSync(statePath, "utf8")).mode, "localization");
        const revision = readFileSync(statePath, "utf8");
        const cancelled = new AbortController();
        cancelled.abort();
        await assert.rejects(execute("parr0t_framework_mode", mode, cancelled.signal), /cancel|abort/i);
        assert.equal(readFileSync(statePath, "utf8"), revision);
        if (explicitHelper) {
          await assert.rejects(() => device("parr0t_framework_mode", { ...mode, workspace }),
            /workspace does not match bound helper/);
          assert.equal(readFileSync(statePath, "utf8"), revision);
        }
        const enabled = session.getEnabledToolNames();
        await session.setActiveToolsByName(enabled.filter(name => name !== "parr0t_framework_mode"));
        const disabled = await device("parr0t_framework_mode", mode);
        assert.equal(disabled.isError, true);
        assert.match(text(disabled), /No such tool/);
        assert.equal(readFileSync(statePath, "utf8"), revision);
        const missing = await device("parr0t_missing_synthetic_tool", {});
        assert.equal(missing.isError, true);
        assert.match(text(missing), /No such tool/);
        await session.setActiveToolsByName(enabled);
      } finally {
        process.argv.splice(0, process.argv.length, ...originalArgv);
        await session?.dispose();
        await manager.close();
        auth.close();
      }
    }
    assert.deepEqual(networkAttempts, [], "Cold startup must not even attempt metadata/model network access");
  } finally {
    globalThis.fetch = originalFetch;
    if (originalControl !== undefined) process.env.PARR0T_OMP_CONTROL_DIRECTORY = originalControl;
    if (originalWorkspace === undefined) delete process.env.PARR0T_OMP_PROMPT_WORKSPACE;
    else process.env.PARR0T_OMP_PROMPT_WORKSPACE = originalWorkspace;
  }
}

async function runIntegration() {
  const root = process.env.PARR0T_OMP_HOST_PACKAGE;
  assert.ok(root, "PARR0T_OMP_HOST_PACKAGE must select an existing host package root");
  const host = await publicHostModules(root);
  const {
    ExtensionRunner, ExtensionRuntime, ExtensionToolWrapper, loadExtensions, loadExtensionFromFactory,
    wrapRegisteredTool, EventBus, SessionManager, Settings, AgentRegistry,
    AgentLifecycleManager, IrcBus, WriteTool, BashTool, ReadTool, MAIN_AGENT_ID,
  } = host;
  const workspace = realpathSync(mkdtempSync(path.join(os.tmpdir(), "parr0t-real-host-synthetic-")));
  const assetRoot = realpathSync(mkdtempSync(path.join(os.tmpdir(), "parr0t-real-host-assets-")));
  const helperPath = fileURLToPath(new URL("../src/game_l10n/templates/omp-prompt-isolation.mjs", import.meta.url));
  const bootstrapPath = fileURLToPath(new URL("../src/game_l10n/templates/omp-native-process.mjs", import.meta.url));
  await proveColdStart(host, workspace, bootstrapPath, helperPath);
  const controlDirectory = path.join(workspace, ".game-l10n", "omp-control", "b".repeat(32));
  const originalControl = process.env.PARR0T_OMP_CONTROL_DIRECTORY;
  const originalWorkspace = process.env.PARR0T_OMP_PROMPT_WORKSPACE;
  mkdirSync(controlDirectory, { recursive: true });
  process.env.PARR0T_OMP_CONTROL_DIRECTORY = controlDirectory;
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

  async function makeSession(id, kind, depth, parentId, selectedHelper = helperPath) {
    const manager = SessionManager.inMemory(workspace);
    const identity = { id, name: id, kind, depth, parentId };
    const eventBus = new EventBus();
    const runtime = new ExtensionRuntime();
    if (selectedHelper) runtime.flagValues.set("parr0t-workspace", workspace);
    const spawned = [];
    const dispatchedTasks = [];
    const helperPaths = selectedHelper ? [bootstrapPath, selectedHelper] : [bootstrapPath];
    const originalArgv = [...process.argv];
    let loaded;
    try {
      // Match the official launcher's helper-owned CLI profile during factory load.
      if (selectedHelper) process.argv.push("--parr0t-workspace", workspace);
      loaded = await loadExtensions(helperPaths, workspace, eventBus, runtime);
    } finally {
      process.argv.splice(0, process.argv.length, ...originalArgv);
    }
    assert.deepEqual(loaded.errors, [], "Actual helper files must load through the selected host");
    assert.equal(loaded.extensions.length, helperPaths.length);
    const consumer = await loadExtensionFromFactory(pi => {
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
            model: pi.zod.string().optional(),
            agent: pi.zod.string().optional(),
            name: pi.zod.string().optional(),
            steps: pi.zod.array(pi.zod.string()).optional(),
          })),
        }),
        async execute(_callId, input) {
          dispatchedTasks.push(input);
          for (const task of input.tasks) {
            const worker = await makeSession(task.name ?? `${id}.Worker${spawned.length + 1}`, "sub", depth + 1, id, selectedHelper);
            // Shared context is intentionally NOT replayed as the child's first
            // user prompt: this is the consumer-visible marker-loss risk.
            await worker.runner.emitBeforeAgentStart(task.task ?? task.steps?.[0] ?? "Synthetic task", undefined, []);
            spawned.push(worker);
          }
          return { content: [{ type: "text", text: "Synthetic descendants created." }], details: {} };
        },
      });
    }, workspace, eventBus, runtime, "synthetic-task-consumer");
    // No ModelRegistry is constructed: context model queries are lazy and this
    // offline fixture never invokes one. The runner, API, schemas and wrappers
    // below are the selected host implementations, not a mocked dispatcher.
    const runner = new ExtensionRunner([...loaded.extensions, consumer], runtime, workspace, manager, undefined,
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
    const read = wrap(new ReadTool(toolSession));
    const registeredTools = new Map(runner.getAllRegisteredTools().map(entry =>
      [entry.definition.name, wrap(wrapRegisteredTool(entry, runner))]));
    const activeTools = new Map(registeredTools);
    // These runtime actions expose the actual wrappers dispatched below, not
    // optimistic name lists. Removing an active wrapper removes availability.
    runtime.getAllTools = () => runner.getAllRegisteredTools()
      .filter(entry => registeredTools.has(entry.definition.name))
      .map(entry => ({ name: entry.definition.name, description: entry.definition.description,
        parameters: entry.definition.parameters, sourceInfo: entry.sourceInfo }));
    runtime.getActiveTools = () => [...activeTools.keys()];
    const unsubscribeTools = runner.onToolRegistered(entry => {
      const tool = wrap(wrapRegisteredTool(entry, runner));
      registeredTools.set(entry.definition.name, tool);
      activeTools.set(entry.definition.name, tool);
    });
    function registered(name) {
      const tool = activeTools.get(name);
      assert.ok(tool, `Selected host has no active wrapper for ${name}`);
      return tool;
    }
    const session = {
      id, runner, manager, spawned, dispatchedTasks, context, registeredTools, activeTools, unsubscribeTools,
      write: input => write.execute(`synthetic-${++sequence}`, input, undefined, undefined, context),
      bash: input => bash.execute(`synthetic-${++sequence}`, input, undefined, undefined, context),
      read: input => read.execute(`synthetic-${++sequence}`, input, undefined, undefined, context),
      exec: input => registered("parr0t_exec").execute(`synthetic-${++sequence}`, input, undefined, undefined, context),
      mode: input => registered("parr0t_framework_mode").execute(`synthetic-${++sequence}`, input, undefined, undefined, context),
      task: input => registered("task").execute(`synthetic-${++sequence}`, input, undefined, undefined, context),
      process: input => registered("parr0t_process").execute(`synthetic-${++sequence}`, input, undefined, undefined, context),
      start: input => registered("parr0t_session").execute(`synthetic-${++sequence}`, input, undefined, undefined, context),
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
    // Ordinary OMP starts without --trusted-extension or workspace flags must
    // acquire the native tool without acquiring worker-isolation attestation.
    const bootstrapRoot = await makeSession("BootstrapRoot", "main", 0, undefined, null);
    await bootstrapRoot.runner.emit({ type: "session_start" });
    const bootstrapChild = await makeSession("BootstrapChild", "sub", 1, bootstrapRoot.id, null);
    await bootstrapChild.runner.emit({ type: "session_start" });
    const literal = "agent://Main; history://not-a-route | %PATH%";
    for (const session of [bootstrapRoot, bootstrapChild]) {
      const result = await session.process({
        argv: [process.env.PARR0T_OMP_PYTHON ?? "python", "-I", "-B", "-c",
          "import json,sys; print(json.dumps(sys.argv[1]))", literal],
      });
      assert.equal(result.isError, false);
      assert.equal(JSON.parse(result.details.stdout), literal);
      assert.equal(session.activeTools.has("parr0t_framework_mode"), true);
      assert.equal(session.activeTools.has("parr0t_session"), true);
      await session.runner.emit({ type: "session_start" });
      assert.equal(session.runner.getAllRegisteredTools().filter(tool => tool.definition.name === "parr0t_process").length, 1);
    }
    assert.equal(existsSync(path.join(controlDirectory, "ready.json")), false);
    // Ambient availability is not worker attestation and must not install the
    // official helper's messaging policy in unrelated projects.
    assert.notEqual((await bootstrapChild.write({ path: `agent://${bootstrapRoot.id}`, content: "ordinary child control" })).isError, true);
    deliveries.length = 0;
    observations.length = 0;
    const lifecycleState = path.join(workspace, ".game-l10n/framework-session.json");
    for (const session of [bootstrapRoot, bootstrapChild]) {
      assert.equal(session.runner.getAllRegisteredTools().filter(tool => tool.definition.name === "parr0t_framework_mode").length, 1);
    }
    await assert.rejects(bootstrapChild.mode({ workspace, mode: "pause", user_requested: true }), /root.*identity/i);
    await assert.rejects(bootstrapChild.start({ workspace, model: "synthetic-host/selected-model", user_requested: true }), /root.*identity/i);
    assert.equal(existsSync(lifecycleState), false);
    const parent = await makeSession(MAIN_AGENT_ID, "main", 0);
    const relay = await makeSession("SyntheticRelay", "sub", 1, parent.id);
    const peer = await makeSession("SyntheticPeer", "sub", 1, parent.id);
    const readinessPath = path.join(controlDirectory, "ready.json");
    parent.activeTools.delete("parr0t_process");
    await assert.rejects(parent.runner.emit({ type: "session_start" }), /parr0t_process is not active/);
    assert.equal(existsSync(readinessPath), false);
    parent.activeTools.set("parr0t_process", parent.registeredTools.get("parr0t_process"));
    parent.activeTools.delete("parr0t_framework_mode");
    await assert.rejects(parent.runner.emit({ type: "session_start" }), /parr0t_framework_mode is not active/);
    assert.equal(existsSync(readinessPath), false);
    parent.activeTools.set("parr0t_framework_mode", parent.registeredTools.get("parr0t_framework_mode"));
    await parent.runner.emit({ type: "session_start" });
    assert.equal(parent.runner.getAllRegisteredTools().filter(tool => tool.definition.name === "parr0t_process").length, 1);
    const receiptText = readFileSync(readinessPath, "utf8");
    const receipt = JSON.parse(receiptText);
    assert.deepEqual(receipt, {
      schema: "parr0t.omp-control-ready.v1", launch_id: path.basename(controlDirectory),
      session_id: parent.manager.getSessionId(), process_id: process.pid,
      helper_sha256: createHash("sha256").update(readFileSync(helperPath)).digest("hex"),
      process_tool: "parr0t_process",
      framework_tool: "parr0t_framework_mode",
    });
    assert.notEqual(receipt.session_id, parent.id);
    assert.notEqual(receipt.session_id, relay.manager.getSessionId());
    assert.notEqual(receipt.process_id, process.ppid);
    await relay.runner.emit({ type: "session_start" });
    await peer.runner.emit({ type: "session_start" });
    assert.equal(readFileSync(readinessPath, "utf8"), receiptText);
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
    for (const child of [relay, peer]) {
      await assert.rejects(child.mode({ mode: "pause", user_requested: true }), /PARR0T_(?:FRAMEWORK_APPROVAL|OMP_PROMPT_ISOLATION)/);
      await assert.rejects(child.start({ workspace, model: "synthetic-host/selected-model", user_requested: true }), /PARR0T_(?:FRAMEWORK_APPROVAL|OMP_PROMPT_ISOLATION)/);
      await assert.rejects(child.write({ path: "xd://parr0t_framework_mode", content: JSON.stringify({ mode: "pause", user_requested: true }) }),
        /PARR0T_(?:FRAMEWORK_APPROVAL|OMP_PROMPT_ISOLATION)/);
    }
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
    await unmarked.runner.emit({ type: "session_start" });
    await unmarked.spawned[0].runner.emit({ type: "session_start" });
    assert.equal(readFileSync(readinessPath, "utf8"), receiptText);

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

    // Compile a fresh first-party synthetic task. No installed game is read,
    // no provider is contacted, and the actual host runs every lifecycle argv.
    const coreSource = fileURLToPath(new URL("../src", import.meta.url));
    function coreMetadata(program, extra = []) {
      const result = Bun.spawnSync([python, "-X", "utf8", "-I", "-B", "-c",
        "import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); w=Path(sys.argv[2]);\n" + program,
        coreSource, workspace, ...extra], { cwd: workspace, stdout: "pipe", stderr: "pipe" });
      assert.equal(result.exitCode, 0, result.stderr.toString());
      return JSON.parse(result.stdout.toString());
    }
    const prepared = coreMetadata(`
from game_l10n.control_plane import CONTROL_STATE_PATH,establish_control_plane_entry,initial_control_plane_state
from game_l10n.data_plane_task import prepare_data_plane_task
from game_l10n.native_runtime import configure_native_runtime,bind_data_plane_launch
(w/"game-l10n.json").write_text("{}",encoding="utf-8")
(w/CONTROL_STATE_PATH).write_text(json.dumps(initial_control_plane_state()),encoding="utf-8")
establish_control_plane_entry(w)
configure_native_runtime(w,"omp",{},omp_isolation="prompt")
game=w/"synthetic-game"; game.mkdir()
config={"game_roots":[str(game)],"sdk_root":str(w/"synthetic-sdk"),"sdk_selection":{"inventory_fingerprint":"synthetic"}}
result=prepare_data_plane_task(w,w/".game-l10n/data-plane/source_probe",worker_role="source_probe",config=config)
bind_data_plane_launch(w,w/result["relay_path"])
print(json.dumps(result))
`);
    const compiledRelay = JSON.parse(readFileSync(path.join(workspace, prepared.relay_path), "utf8"));
    const controlledRelay = await makeSession("CompiledRelay", "sub", 1, parent.id);
    const firstTurn = await controlledRelay.runner.emitBeforeAgentStart(
      `PARR0T_OMP_RELAY=${prepared.relay_path}\nSynthetic relay`, undefined, [],
    );
    const firstCall = firstTurn.systemPrompt.flatMap(block => block.split("\n"))
      .find(line => line.startsWith("Your first tool call is the built-in task with "));
    assert.ok(firstCall, "the relay must be able to dispatch before reading any contract with a tool");
    const args = JSON.parse(firstCall.slice(firstCall.indexOf("{"), firstCall.lastIndexOf("}") + 1));
    await controlledRelay.task(args);
    const delivered = controlledRelay.dispatchedTasks[0].tasks[0];
    const dispatch = compiledRelay.launch_contract.worker_dispatches["worker-0"];
    assert.equal(delivered.task, dispatch.worker_prompt);
    assert.equal(delivered.model, dispatch.model);
    assert.ok(delivered.task.includes(dispatch.task_path));
    assert.ok(delivered.task.includes(dispatch.task_sha256));
    const actualWorker = controlledRelay.spawned[0];
    const summary = coreMetadata(`
from game_l10n.native_runtime import native_launch_summary
print(json.dumps(native_launch_summary(w,w/sys.argv[3])))
`, [prepared.relay_path]);
    assert.equal(summary.started_workers, 1);
    assert.deepEqual(summary.issue_codes, []);
    await assert.rejects(() => controlledRelay.task({ tasks: [{ task: "Synthetic retry after lost output" }] }),
      /PARR0T_(?:NATIVE_LIFECYCLE|OMP_PROMPT_ISOLATION)/);
    assert.equal(controlledRelay.dispatchedTasks.length, 1);
    const seal = () => controlledRelay.process({ argv: compiledRelay.command_argv.seal });
    await assert.rejects(seal, /PARR0T_(?:NATIVE_LIFECYCLE|OMP_PROMPT_ISOLATION)/);
    await actualWorker.runner.emit({ type: "agent_end", messages: [], willContinue: true, awaitingAsyncWork: false });
    await assert.rejects(seal, /PARR0T_(?:NATIVE_LIFECYCLE|OMP_PROMPT_ISOLATION)/);
    await actualWorker.runner.emit({ type: "agent_end", messages: [], willContinue: false, awaitingAsyncWork: false });
    const sealed = JSON.parse((await seal()).details.stdout);
    assert.ok(sealed.issue_codes.includes("task_incomplete"));
    assert.ok(!sealed.issue_codes.includes("E_NATIVE_WORKER_NOT_STARTED"));

    // The guarded file is loaded by the same real host loader, not imported as
    // a mocked factory. Its native Python runner enforces the bound offline IO.
    delete process.env.PARR0T_OMP_CONTROL_DIRECTORY;
    // Match initialize_workspace: guarded startup requires the persisted Core
    // state, not only the prompt reader's implicit initial-state fallback.
    assert.equal(existsSync(path.join(workspace, ".game-l10n/control-plane-state.json")), true);
    const guardPath = fileURLToPath(new URL("../src/game_l10n/templates/omp-isolation-guard.mjs", import.meta.url));
    const guardedRoot = await makeSession("GuardedRoot", "main", 0, undefined, guardPath);
    await guardedRoot.runner.emit({ type: "session_start" });
    assert.equal(guardedRoot.activeTools.has("parr0t_process"), false);
    const registrationPath = path.join(workspace, ".game-l10n/native/omp-isolation",
      `${createHash("sha256").update(guardedRoot.manager.getSessionId()).digest("hex")}.json`);
    const registration = JSON.parse(readFileSync(registrationPath, "utf8"));
    const asset = path.join(assetRoot, "selected.txt");
    const unbound = path.join(assetRoot, "unbound.txt");
    const resultFile = path.join(workspace, "offline-result.txt");
    const script = path.join(workspace, "approved-offline.py");
    writeFileSync(asset, "synthetic offline asset");
    writeFileSync(unbound, "not authorized");
    writeFileSync(script, "import sys\nfrom pathlib import Path\nPath(sys.argv[2]).write_bytes(Path(sys.argv[1]).read_bytes())\n");
    const digest = file => createHash("sha256").update(readFileSync(file)).digest("hex");
    const taskRelative = ".game-l10n/data-plane/offline/task.json";
    const offlineRelative = ".game-l10n/data-plane/offline/relay.json";
    mkdirSync(path.dirname(path.join(workspace, taskRelative)), { recursive: true });
    const offlineArgv = [script, asset, resultFile];
    const task = {
      output_artifact: resultFile,
      content_source_policy: { schema: "parr0t.content-source-policy.v1", policy_digest: "synthetic",
        semantic_content_generation: true, content_network_access: false, allowed_external_sources: [] },
      bound_local_assets: [{ path: asset, sha256: digest(asset) }],
      offline_processing: [{ argv: offlineArgv, sha256: digest(script) }],
    };
    writeFileSync(path.join(workspace, taskRelative), JSON.stringify(task));
    writeFileSync(path.join(workspace, offlineRelative), JSON.stringify({
      schema: "parr0t.data-plane-relay.v4", task_id: "synthetic-offline",
      task_path: taskRelative, launch_contract: { message_isolation: registration },
    }));
    const offlineWorker = await makeSession("OfflineWorker", "sub", 2, guardedRoot.id, guardPath);
    await offlineWorker.runner.emit({ type: "session_start" });
    await assert.rejects(offlineWorker.mode({ mode: "pause", user_requested: true }), /PARR0T_(?:FRAMEWORK_APPROVAL|OMP_ISOLATION)/);
    await assert.rejects(offlineWorker.write({ path: "xd://parr0t_framework_mode", content: JSON.stringify({ mode: "pause", user_requested: true }) }),
      /PARR0T_(?:FRAMEWORK_APPROVAL|OMP_ISOLATION)/);
    await offlineWorker.runner.emitBeforeAgentStart(`PARR0T_OMP_RELAY=${offlineRelative}`, undefined, []);
    await assert.rejects(offlineWorker.write({ path: `agent://${guardedRoot.id}`, content: "denied" }), /PARR0T_OMP_ISOLATION/);
    await assert.rejects(offlineWorker.read({ path: unbound }), /PARR0T_OMP_ISOLATION/);
    assert.notEqual((await offlineWorker.read({ path: asset })).isError, true);
    assert.equal((await offlineWorker.exec({ argv: offlineArgv })).isError, false);
    assert.equal(readFileSync(resultFile, "utf8"), "synthetic offline asset");
    writeFileSync(asset, "changed after inventory");
    await assert.rejects(offlineWorker.read({ path: asset }), /PARR0T_OMP_ISOLATION/);
    await assert.rejects(offlineWorker.exec({ argv: offlineArgv }), /PARR0T_OMP_ISOLATION/);
    const generation = registration.control_generation;
    for (const mode of ["pause", "analysis", "shutdown", "localization"]) {
      assert.equal((await guardedRoot.mode({ mode, user_requested: true })).isError, false);
    }
    assert.equal(JSON.parse(readFileSync(path.join(workspace, ".game-l10n/control-plane-state.json"), "utf8")).generation, generation);
    assert.equal((await guardedRoot.mode({ mode: "analysis", user_requested: true })).isError, false);
    const privateUri = pathToFileURL(path.join(workspace, taskRelative)).href;
    assert.notEqual((await guardedRoot.read({ path: privateUri })).isError, true);
    const exposed = JSON.parse(readFileSync(path.join(workspace, ".game-l10n/control-plane-state.json"), "utf8"));
    assert.equal(exposed.status, "rotation_required");
    assert.equal(exposed.issue_code, "control_plane_leak");
    assert.equal((await guardedRoot.mode({ mode: "localization", user_requested: true })).isError, true);
    assert.deepEqual(deliveries, []);
    assert.deepEqual(observations, []);
  } finally {
    for (const session of sessions) {
      session.unsubscribeTools();
      session.runner.clearManagedTimers();
      session.runner.disposeFileFallbacks();
      await session.manager.close();
      registry.unregister(session.id);
    }
    await lifecycle.dispose();
    if (originalControl === undefined) delete process.env.PARR0T_OMP_CONTROL_DIRECTORY;
    else process.env.PARR0T_OMP_CONTROL_DIRECTORY = originalControl;
    if (originalWorkspace === undefined) delete process.env.PARR0T_OMP_PROMPT_WORKSPACE;
    else process.env.PARR0T_OMP_PROMPT_WORKSPACE = originalWorkspace;
    rmSync(workspace, { recursive: true, force: true });
    rmSync(assetRoot, { recursive: true, force: true });
  }
}

if (process.argv.includes("--parr0t-real-host-worker")) {
  await runIntegration();
} else {
  const { test } = await import("bun:test");
  const realHostTest = process.env.PARR0T_OMP_HOST_PACKAGE ? test : test.skip;
  realHostTest("real selected host cold-starts ambient tools and enforces native lifecycle and descendant isolation", async () => {
    // All host discovery roots, state and credentials belong to this temporary
    // subprocess. Retain only the selected host/Python and OS executable paths.
    const sandbox = realpathSync(mkdtempSync(path.join(os.tmpdir(), "parr0t-host-home-")));
    const env = Object.fromEntries(Object.entries(process.env).filter(([name]) =>
      ["PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "PARR0T_OMP_HOST_PACKAGE", "PARR0T_OMP_PYTHON"].includes(name.toUpperCase())));
    for (const name of ["HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR"]) {
      env[name] = sandbox;
    }
    env.PI_CONFIG_DIR = ".omp";
    // Preserve the host's documented offline test-mode behavior in this isolated
    // subprocess, including its otherwise automatic Codex-version refresh.
    env.NODE_ENV = "test";
    env.PI_CODING_AGENT_DIR = path.join(sandbox, ".omp", "agent");
    const worker = Bun.spawn([process.execPath, fileURLToPath(import.meta.url), "--parr0t-real-host-worker"], {
      cwd: sandbox, env, stdout: "pipe", stderr: "pipe",
    });
    try {
      const [code, stdout, stderr] = await Promise.all([
        worker.exited, new Response(worker.stdout).text(), new Response(worker.stderr).text(),
      ]);
      assert.equal(code, 0, `Selected-host integration failed:\n${stderr}\n${stdout}`);
    } finally {
      if (worker.exitCode === null) worker.kill();
      rmSync(sandbox, { recursive: true, force: true });
    }
  }, 120_000);
}
