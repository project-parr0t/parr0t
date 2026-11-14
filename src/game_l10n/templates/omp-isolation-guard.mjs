import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { accessSync, appendFileSync, constants, existsSync, mkdirSync, readFileSync, realpathSync, renameSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const TEMPLATE = fileURLToPath(import.meta.url);
// The guard is a package resource in both src checkouts and installed wheels.
const PACKAGE = path.resolve(path.dirname(TEMPLATE), "..");
const CORE_SRC = path.dirname(PACKAGE);
const CHECKOUT = path.basename(CORE_SRC) === "src" && existsSync(path.join(CORE_SRC, "../pyproject.toml"));
const CORE = CHECKOUT ? path.dirname(CORE_SRC) : PACKAGE;
const SCHEMA = "parr0t.omp-isolation.v1";
const SESSION_ENV = "PARR0T_OMP_GUARD_SESSION";
const WORKSPACE_ENV = "PARR0T_OMP_GUARD_WORKSPACE";
const OUTPUT_BYTES = 1024 * 1024;
const LOCAL_TOOLS = new Set(["read", "write", "edit", "grep", "glob"]);
const EXEC_VERBS = {
  "record-native-launch": { values: ["--workspace", "--relay", "--event", "--task", "--worker-id", "--model", "--observed-model"], flags: [] },
  "record-data-plane-relay-event": { values: ["--workspace", "--relay", "--event", "--slot"], flags: [] },
  "seal-data-plane-task": { values: ["--workspace", "--relay"], flags: ["--worker-ended"] },
  "seal-native-wave-relay": { values: ["--workspace", "--dispatch", "--ended-task"], flags: ["--relay-ended"] },
};

function fail(message, issueCode = "isolation_policy_denied") {
  throw Object.assign(new Error(`PARR0T_OMP_ISOLATION: ${message}`), { issueCode });
}
function normalize(value) {
  let result = String(value).normalize("NFKC");
  for (let n = 0; n < 8; n++) {
    let decoded;
    try { decoded = decodeURIComponent(result); } catch { fail("invalid encoded target"); }
    if (decoded === result) break;
    result = decoded;
  }
  return result.replace(/[\s\u0000-\u001f\u007f\u200b-\u200f\ufeff]/gu, "");
}
function localTarget(value) {
  const normalized = normalize(value);
  if (/^[a-z][a-z0-9+.-]+:/i.test(normalized) || normalized.includes("://")) fail("virtual and network targets are forbidden", "virtual_target_forbidden");
  if (!normalized || normalized.includes("%")) fail("ambiguous target");
  return value;
}
function inside(root, target) {
  const relative = path.relative(root, target);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}
function resolvedLocal(value, cwd) {
  localTarget(value);
  const absolute = path.resolve(cwd, value);
  // Resolve the nearest existing ancestor too, so a directory symlink cannot
  // turn an apparently local write into a write outside the workspace.
  let ancestor = absolute;
  while (true) {
    try { return path.resolve(realpathSync(ancestor), path.relative(ancestor, absolute)); }
    catch (error) {
      if (error.code !== "ENOENT" && error.code !== "ENOTDIR") throw error;
      const parent = path.dirname(ancestor);
      if (parent === ancestor) throw error;
      ancestor = parent;
    }
  }
}
function pythonFromPath() {
  const names = process.platform === "win32" ? ["python.exe", "python3.exe"] : ["python3", "python"];
  for (const name of names) {
    for (const directory of (process.env.PATH ?? "").split(path.delimiter)) {
      if (!directory || !path.isAbsolute(directory)) continue;
      const candidate = path.join(directory, name);
      try {
        accessSync(candidate, process.platform === "win32" ? constants.F_OK : constants.X_OK);
        if (statSync(candidate).isFile()) return realpathSync(candidate);
      } catch { /* Continue PATH lookup, never invoke a shell or launcher. */ }
    }
  }
  fail("native Python executable is unavailable on host PATH");
}
function generation(workspace) {
  const state = JSON.parse(readFileSync(path.join(workspace, ".game-l10n/control-plane-state.json"), "utf8"));
  if (!Number.isSafeInteger(state.generation) || state.generation < 0) fail("invalid control generation");
  return state.generation;
}
function editPaths(input) {
  if (typeof input.input !== "string") fail("unsupported edit format");
  const targets = [];
  for (const line of input.input.split(/\r?\n/)) {
    const header = /^\[(.+)#[a-f0-9]{4}\]$/i.exec(line);
    if (header) targets.push(header[1]);
    const move = /^MV\s+(.+)$/.exec(line);
    if (move) targets.push(move[1].replace(/^"(.*)"$/, "$1"));
  }
  if (!targets.length) fail("edit has no file target");
  return targets;
}
function taskInput(input, subagent) {
  const output = structuredClone(input);
  const items = Array.isArray(output.tasks) ? output.tasks : [output];
  for (const item of items) {
    if (item.tools !== undefined && (!Array.isArray(item.tools) || item.tools.length)) fail("shared callback tools are forbidden");
    delete item.tools;
    if (typeof item.sourceSession === "string" && !item.sourceSession.trim() && item.agent !== "seance") delete item.sourceSession;
    if (subagent && item.sourceSession !== undefined) fail("worker history is forbidden");
    if (subagent && item.agent === "seance") fail("history agents are forbidden");
    if (subagent && (typeof item.model !== "string" || !/^[^\s/]+\/[^\s*?,]+$/.test(item.model))) fail("an explicit exact worker model is required");
    for (const key of ["history", "parentHistory", "inheritHistory", "session", "sessionId"]) {
      if (item[key] !== undefined) fail("inherited history is forbidden");
    }
  }
  return output;
}
function relayFromPrompt(prompt, workspace) {
  const markers = [...String(prompt).matchAll(/^PARR0T_OMP_RELAY=(.+)$/gm)].map(match => match[1].trim());
  if (new Set(markers).size !== 1) fail("exactly one core relay binding is required");
  const relayPath = resolvedLocal(markers[0], workspace);
  const nativeRoot = path.join(workspace, ".game-l10n/native/relay");
  const dataRoot = path.join(workspace, ".game-l10n/data-plane");
  const native = inside(nativeRoot, relayPath) && relayPath.endsWith(".json");
  const data = inside(dataRoot, relayPath) && path.basename(relayPath) === "relay.json";
  if (!native && !data) fail("relay binding is not core relay metadata");
  const metadata = JSON.parse(readFileSync(relayPath, "utf8"));
  if (native && (metadata.schema !== "parr0t.native-wave-relay.v1" || !Array.isArray(metadata.task_bindings))) fail("invalid native relay metadata");
  if (data && (!/^parr0t\.data-plane-relay\.v[1-4]$/.test(metadata.schema) || typeof metadata.task_id !== "string")) fail("invalid data-plane relay metadata");
  const registration = JSON.parse(readFileSync(path.join(workspace, ".game-l10n/native/omp-isolation.json"), "utf8"));
  const proof = metadata.launch_contract?.message_isolation;
  const keys = ["schema", "control_generation", "session_id", "process_id", "guard_sha256"];
  if (!proof || Object.keys(proof).length !== keys.length || Object.keys(registration).length !== keys.length || keys.some(key => proof[key] !== registration[key]) || registration.schema !== SCHEMA || registration.session_id !== process.env[SESSION_ENV] || registration.control_generation !== generation(workspace) || registration.guard_sha256 !== createHash("sha256").update(readFileSync(TEMPLATE)).digest("hex")) fail("relay isolation proof is absent or stale");
  return { path: relayPath, metadata, native };
}
function relayRead(value, cwd, relay) {
  const target = resolvedLocal(value.replace(/:(?:raw:)?(?:\d|[-,]|raw).*$/, ""), cwd);
  const docs = path.join(CORE, "docs");
  if (target === path.join(CORE, "AGENTS.md") || (inside(docs, target) && target.endsWith(".md")) || target === relay.path) return;
  fail("relay may read only public core AGENTS/docs and its bound relay metadata");
}
function relayArguments(argv, cwd, workspace, relay) {
  if (argv[0] !== "-m" || argv[1] !== "game_l10n.cli") fail("relay must invoke the fixed core CLI module");
  const verb = argv[2];
  const spec = EXEC_VERBS[verb];
  if (!spec) fail("relay command is not a metadata lifecycle verb");
  if (relay.native !== (verb === "seal-native-wave-relay" || verb === "record-native-launch")) {
    // record-native-launch is also needed for data-plane launch accounting.
    if (verb !== "record-native-launch") fail("lifecycle verb does not match bound relay");
  }
  const options = new Map();
  for (let index = 3; index < argv.length; index++) {
    const key = argv[index];
    if (spec.flags.includes(key)) {
      if (options.has(key)) fail("duplicate lifecycle option");
      options.set(key, true);
    } else if (spec.values.includes(key)) {
      const value = argv[++index];
      if (typeof value !== "string" || value.startsWith("-") || !value) fail("invalid lifecycle option");
      if (options.has(key) && key !== "--ended-task") fail("duplicate lifecycle option");
      options.set(key, value);
      if (key === "--task" && verb === "record-native-launch") {
        if (!relay.metadata.launch_contract?.tasks?.includes(value)) fail("launch task key is not bound to relay");
      } else if (key === "--ended-task") {
        const target = resolvedLocal(value, workspace);
        if (!relay.native || !relay.metadata.task_bindings.some(item => typeof item.task === "string" && resolvedLocal(item.task, workspace) === target)) fail("lifecycle task is not bound to relay");
      }
    } else fail("unknown lifecycle option");
  }
  if (typeof options.get("--workspace") !== "string" || resolvedLocal(options.get("--workspace"), cwd) !== workspace) fail("workspace mismatch");
  const relayOption = verb === "seal-native-wave-relay" ? "--dispatch" : "--relay";
  if (typeof options.get(relayOption) !== "string" || resolvedLocal(options.get(relayOption), workspace) !== relay.path) fail("relay mismatch");
  const bootstrap = `import runpy,sys;sys.path.insert(0,${JSON.stringify(CORE_SRC)});runpy.run_module('game_l10n.cli',run_name='__main__')`;
  return ["-I", "-B", "-c", bootstrap, ...argv.slice(2)];
}
async function executePython(executable, argv, cwd, signal) {
  if (signal?.aborted) fail("execution cancelled");
  const env = { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONPATH: CORE_SRC };
  delete env.PYTHONSTARTUP;
  delete env.PYTHONINSPECT;
  // Native subprocesses have no OMP virtual URL dispatcher or agent registry.
  return await new Promise((resolve, reject) => {
    const child = spawn(executable, argv, { cwd, env, shell: false, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    const streams = { stdout: [], stderr: [] };
    const sizes = { stdout: 0, stderr: 0 };
    const totalBytes = { stdout: 0, stderr: 0 };
    const truncated = { stdout: false, stderr: false };
    for (const name of ["stdout", "stderr"]) child[name].on("data", chunk => {
      totalBytes[name] += chunk.length;
      const remaining = OUTPUT_BYTES - sizes[name];
      if (chunk.length > remaining) truncated[name] = true;
      if (remaining > 0) {
        streams[name].push(chunk.subarray(0, remaining));
        sizes[name] += Math.min(chunk.length, remaining);
      }
    });
    const cancel = () => child.kill();
    signal?.addEventListener("abort", cancel, { once: true });
    if (signal?.aborted) cancel();
    child.once("error", error => { signal?.removeEventListener("abort", cancel); reject(error); });
    child.once("close", (code, terminationSignal) => {
      signal?.removeEventListener("abort", cancel);
      const stdout = Buffer.concat(streams.stdout).toString("utf8");
      const stderr = Buffer.concat(streams.stderr).toString("utf8");
      const details = { exitCode: code, signal: terminationSignal, cancelled: Boolean(signal?.aborted), stdout, stderr, truncated, totalBytes, retainedBytes: sizes, outputComplete: !truncated.stdout && !truncated.stderr };
      resolve({ content: [{ type: "text", text: JSON.stringify(details) }], details, isError: code !== 0 || Boolean(signal?.aborted) || !details.outputComplete });
    });
  });
}

// File-backed factories are rebound by OMP for task/eval children and re-imported
// on worker threads. No module-level session identity or mutable policy is shared.
export default function isolationGuard(pi) {
  pi.registerFlag("parr0t-workspace", { description: "Absolute Parr0t workspace guarded for this root session", type: "string" });
  const python = pythonFromPath();
  let workspace;
  let ready = false;
  let fatal;
  let relay;
  function initialize(ctx) {
    if (!ctx.agent || !["main", "sub"].includes(ctx.agent.kind)) fail("host agent identity is unavailable");
    let configured = pi.getFlag("parr0t-workspace");
    if (ctx.agent.kind === "sub" && (typeof configured !== "string" || !path.isAbsolute(configured))) configured = process.env[WORKSPACE_ENV];
    if (typeof configured !== "string" || !path.isAbsolute(configured)) fail("--parr0t-workspace must be absolute");
    workspace = realpathSync(configured);
    if (ctx.agent.kind === "main") {
      const sessionId = ctx.sessionManager.getSessionId();
      if (typeof sessionId !== "string" || !sessionId) fail("host root session ID is unavailable");
      process.env[SESSION_ENV] = sessionId;
      process.env[WORKSPACE_ENV] = workspace;
      const registration = {
        schema: SCHEMA,
        control_generation: generation(workspace),
        session_id: sessionId,
        process_id: process.pid,
        guard_sha256: createHash("sha256").update(readFileSync(TEMPLATE)).digest("hex"),
      };
      const registrationPath = path.join(workspace, ".game-l10n/native/omp-isolation.json");
      let current;
      try { current = readFileSync(registrationPath, "utf8"); } catch (error) { if (error.code !== "ENOENT") throw error; }
      const serialized = `${JSON.stringify(registration)}\n`;
      if (current !== serialized) {
        mkdirSync(path.dirname(registrationPath), { recursive: true });
        const temporary = `${registrationPath}.${process.pid}.tmp`;
        writeFileSync(temporary, serialized, { mode: 0o600 });
        renameSync(temporary, registrationPath);
      }
      const launchDirectory = process.env.PARR0T_OMP_CONTROL_DIRECTORY;
      if (launchDirectory) {
        const directory = resolvedLocal(launchDirectory, workspace);
        if (path.dirname(directory) !== path.join(workspace, ".game-l10n/omp-control") || !/^[a-f0-9]{32}$/.test(path.basename(directory))) fail("invalid control launch directory");
        const receiptPath = path.join(directory, "ready.json");
        const receipt = `${JSON.stringify({ schema: "parr0t.omp-control-ready.v1", launch_id: path.basename(directory), session_id: sessionId, process_id: process.pid, guard_sha256: registration.guard_sha256 })}\n`;
        let previous;
        try { previous = readFileSync(receiptPath, "utf8"); } catch (error) { if (error.code !== "ENOENT") throw error; }
        if (previous !== receipt) {
          const temporary = `${receiptPath}.${process.pid}.tmp`;
          writeFileSync(temporary, receipt, { mode: 0o600 });
          renameSync(temporary, receiptPath);
        }
      }
    } else {
      const registration = JSON.parse(readFileSync(path.join(workspace, ".game-l10n/native/omp-isolation.json"), "utf8"));
      if (registration.schema !== SCHEMA || registration.session_id !== process.env[SESSION_ENV] || registration.control_generation !== generation(workspace) || registration.guard_sha256 !== createHash("sha256").update(readFileSync(TEMPLATE)).digest("hex")) fail("root guard registration is absent or stale");
    }
    ready = true;
  }
  pi.on("session_start", async (_event, ctx) => {
    try { initialize(ctx); }
    catch (error) {
      fatal = error;
      ready = false;
      deny(ctx, "startup", { issueCode: "guard_initialization_failed", message: "PARR0T_OMP_ISOLATION: guard initialization failed" });
      await ctx.shutdown();
      throw error;
    }
  });
  pi.on("before_agent_start", (event, ctx) => {
    if (!ready && !fatal) initialize(ctx);
    if (!ready || fatal) fail("guard startup failed");
    if (ctx.agent.kind === "sub") {
      const bound = relayFromPrompt(event.prompt, workspace);
      if (relay && relay.path !== bound.path) fail("relay binding cannot change within a child session");
      relay = bound;
    }
  });
  function deny(ctx, toolName, error) {
    // Categorical evidence only: no inputs, targets, commands or error prose.
    if (workspace) {
      const knownTools = new Set([...LOCAL_TOOLS, "parr0t_exec", "task", "wait", "eval", "bash", "yield", "ask", "web_search"]);
      const record = {
        session: ctx.sessionManager.getSessionId(),
        role: ctx.agent?.kind === "main" ? "root" : ctx.agent?.depth === 1 ? "relay" : "worker",
        depth: Number.isSafeInteger(ctx.agent?.depth) ? ctx.agent.depth : null,
        tool: knownTools.has(toolName) ? toolName : "unknown",
        issue_code: error.issueCode ?? "isolation_policy_denied",
      };
      try {
        appendFileSync(path.join(workspace, ".game-l10n/native/omp-isolation-events.jsonl"), `${JSON.stringify(record)}\n`, { mode: 0o600 });
      } catch { return { block: true, reason: "PARR0T_OMP_ISOLATION: denial evidence could not be recorded" }; }
    }
    return { block: true, reason: error.message };
  }
  pi.on("before_subagent_spawn", (event, ctx) => {
    if (!ready || fatal) return deny(ctx, "task", { message: "PARR0T_OMP_ISOLATION: guard startup failed" });
    if (event.invocationKind !== "task") return deny(ctx, "task", { issueCode: "spawn_origin_forbidden", message: "PARR0T_OMP_ISOLATION: use built-in task; eval agent/workpool/custom spawns and shared callbacks are forbidden" });
    if (ctx.agent.kind === "sub" && ctx.agent.depth !== 1) return deny(ctx, "task", { message: "PARR0T_OMP_ISOLATION: worker delegation is forbidden" });
  });
  pi.on("tool_call", (event, ctx) => {
    try {
      if (!ready || fatal) fail("guard startup failed");
      if (ctx.agent.kind === "main") {
        initialize(ctx);
        if (event.toolName === "task") return { input: taskInput(event.input, false) };
        return;
      }
      if (ctx.agent.kind !== "sub") fail("unknown host identity");
      if (event.toolName === "yield") return;
      if (!relay) fail("relay binding is unavailable");
      relay = relayFromPrompt(`PARR0T_OMP_RELAY=${relay.path}`, workspace);
      // OMP exposes deferred extension tools through their exact xd:// device.
      // Only this owned device is admitted; execute() still validates role/argv.
      if ((event.toolName === "read" || event.toolName === "write") && event.input.path === "xd://parr0t_exec") return;
      if (!["parr0t_exec", "task", "wait", ...LOCAL_TOOLS].includes(event.toolName)) fail("tool is forbidden for subagents");
      if (event.toolName === "write" || event.toolName === "edit") {
        const targets = event.toolName === "edit" ? editPaths(event.input) : [event.input.path];
        for (const target of targets) {
          if (typeof target !== "string") fail("file target is required");
          for (const part of target.split(";")) localTarget(part);
        }
      }
      if (event.toolName === "task") {
        if (ctx.agent.depth !== 1) fail("worker delegation is forbidden");
        if (!relay) fail("relay binding is unavailable");
        const input = taskInput(event.input, true);
        for (const item of input.tasks ?? [input]) {
          const marker = `PARR0T_OMP_RELAY=${path.relative(workspace, relay.path).split(path.sep).join("/")}`;
          if (typeof item.task === "string") item.task = `${marker}\n${item.task}`;
          if (Array.isArray(item.steps)) item.steps = item.steps.map(step => `${marker}\n${step}`);
        }
        return { input };
      }
      if (event.toolName === "wait") {
        if (ctx.agent.depth !== 1) fail("worker wait is forbidden");
        return;
      }
      if (event.toolName === "parr0t_exec") {
        if (!Array.isArray(event.input.argv) || !event.input.argv.length || event.input.argv.some(arg => typeof arg !== "string" || arg.includes("\0"))) fail("Python argv is required");
        const cwd = resolvedLocal(event.input.cwd ?? workspace, ctx.cwd);
        if (!inside(workspace, cwd) && cwd !== CORE) fail("execution cwd must be inside workspace or the exact core root");
        if (ctx.agent.depth === 1) relayArguments(event.input.argv, cwd, workspace, relay);
        else if (ctx.agent.depth < 2) fail("unsupported subagent depth");
        return;
      }
      if (ctx.agent.depth === 1) {
        if (event.toolName !== "read" || !relay || typeof event.input.path !== "string") fail("relay local tool is forbidden");
        relayRead(event.input.path, ctx.cwd, relay);
        return;
      }
      const targets = event.toolName === "edit" ? editPaths(event.input) : [event.input.path ?? (event.toolName === "grep" || event.toolName === "glob" ? "." : undefined)];
      for (const target of targets) {
        if (typeof target !== "string") fail("file target is required");
        for (const part of target.split(";")) {
          localTarget(part);
          if ((event.toolName === "write" || event.toolName === "edit") && !inside(workspace, resolvedLocal(part, ctx.cwd))) fail("writes must remain inside workspace");
        }
      }
    } catch (error) { return deny(ctx, event.toolName, error); }
  });
  const z = pi.zod;
  pi.registerTool({
    name: "parr0t_exec",
    label: "Parr0t Native Python",
    description: "Run native Python argv without an OMP shell/eval bridge or implicit deadline. Relay: fixed core CLI metadata lifecycle commands only. Workers: bound local game-data work. Subagents may be spawned only through built-in task, never eval agent/workpool or shared callbacks. This is not an OS sandbox.",
    parameters: z.object({ argv: z.array(z.string()), cwd: z.string().optional() }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      if (!ready || fatal) fail("guard startup failed");
      if (!Array.isArray(params.argv) || !params.argv.length || params.argv.some(arg => typeof arg !== "string" || arg.includes("\0"))) fail("Python argv is required");
      const cwd = resolvedLocal(params.cwd ?? workspace, ctx.cwd);
      if (!inside(workspace, cwd) && cwd !== CORE) fail("execution cwd must be inside workspace or the exact core root");
      let argv = ["-B", ...params.argv];
      if (ctx.agent.kind === "sub") {
        if (!relay) fail("relay binding is unavailable");
        relay = relayFromPrompt(`PARR0T_OMP_RELAY=${relay.path}`, workspace);
        if (ctx.agent.depth === 1) argv = relayArguments(params.argv, cwd, workspace, relay);
        else if (ctx.agent.depth < 2) fail("unsupported subagent depth");
      } else if (ctx.agent.kind !== "main") fail("unknown host identity");
      return await executePython(python, argv, cwd, signal);
    },
  });
}
