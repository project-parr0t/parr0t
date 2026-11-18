import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import promptIsolation from "../src/game_l10n/templates/omp-prompt-isolation.mjs";

const temporary = [];
const originalWorkspace = process.env.PARR0T_OMP_PROMPT_WORKSPACE;
const originalControl = process.env.PARR0T_OMP_CONTROL_DIRECTORY;
beforeEach(() => { delete process.env.PARR0T_OMP_CONTROL_DIRECTORY; });
afterEach(() => {
  for (const directory of temporary.splice(0)) rmSync(directory, { recursive: true, force: true });
  if (originalWorkspace === undefined) delete process.env.PARR0T_OMP_PROMPT_WORKSPACE;
  else process.env.PARR0T_OMP_PROMPT_WORKSPACE = originalWorkspace;
  if (originalControl === undefined) delete process.env.PARR0T_OMP_CONTROL_DIRECTORY;
  else process.env.PARR0T_OMP_CONTROL_DIRECTORY = originalControl;
});
function writeJson(file, value) {
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(value));
}
// Registration-only schemas; the real-host suite uses the host's actual zod.
function schema() {
  return { min() { return this; }, optional() { return this; }, describe() { return this; }, strict() { return this; } };
}
const fakeZod = { string: schema, array: schema, object: schema };
fakeZod.z = fakeZod;
function host(workspace, kind = "sub", depth = kind === "main" ? 0 : 2, api = {}) {
  const handlers = new Map();
  const flags = new Map();
  const tools = new Map();
  const executions = [];
  const executionResult = { stdout: "synthetic stdout", stderr: "synthetic stderr", code: 7, killed: false };
  promptIsolation({
    zod: fakeZod,
    registerFlag(name, options) { flags.set(name, options); },
    getFlag() { return kind === "main" ? workspace : undefined; },
    on(name, handler) { handlers.set(name, handler); },
    registerTool(tool) { tools.set(tool.name, tool); },
    getAllTools() { return [...tools.values()]; },
    getActiveTools() { return [...tools.keys()]; },
    async exec(command, args, options) {
      executions.push({ command, args, options });
      return executionResult;
    },
    ...api,
  });
  const ctx = {
    cwd: workspace, agent: { kind, depth, id: kind === "main" ? "SyntheticRoot" : "SyntheticChild" },
    sessionManager: { getSessionId: () => "synthetic-host-session" },
  };
  return {
    handlers, flags, tools, executions, executionResult, ctx,
    emit: async (name, event = {}) => handlers.get(name)?.(event, ctx),
    async call(toolName, input, deliver = () => {}) {
      const decision = await handlers.get("tool_call")({ toolName, input }, ctx);
      if (!decision?.block) await deliver(decision?.input ?? input);
      return decision;
    },
  };
}
function fixture(native = false) {
  const workspace = mkdtempSync(path.join(os.tmpdir(), "parr0t-prompt-synthetic-"));
  temporary.push(workspace);
  process.env.PARR0T_OMP_PROMPT_WORKSPACE = workspace;
  const relative = native ? ".game-l10n/native/relay/synthetic-wave.json" : ".game-l10n/data-plane/synthetic/relay.json";
  const relay = path.join(workspace, relative);
  writeJson(relay, native
    ? { schema: "parr0t.native-wave-relay.v1", task_bindings: [] }
    : { schema: "parr0t.data-plane-relay.v4", task_id: "synthetic-task" });
  return { workspace, relay, marker: `PARR0T_OMP_RELAY=${relative}` };
}
async function child(f, sessionStart = false) {
  const session = host(f.workspace);
  if (sessionStart) await session.emit("session_start");
  await session.emit("before_agent_start", { prompt: `${f.marker}\nSynthetic task only.`, systemPrompt: [] });
  return session;
}
async function deniedWithoutDelivery(session, tool, input) {
  let deliveries = 0;
  const denial = await session.call(tool, input, () => { deliveries++; });
  expect(denial?.block).toBe(true);
  expect(deliveries).toBe(0);
  return denial;
}

function controlLaunch(workspace) {
  const directory = path.join(workspace, ".game-l10n", "omp-control", "a".repeat(32));
  mkdirSync(directory, { recursive: true });
  process.env.PARR0T_OMP_CONTROL_DIRECTORY = directory;
  return directory;
}

describe("OMP prompt helper readiness", () => {
  test("direct-loaded operation does not require readiness APIs or write a receipt", async () => {
    const f = fixture();
    const session = host(f.workspace, "main", 0, { getAllTools: undefined, getActiveTools: undefined });
    await session.emit("session_start");
    expect(existsSync(path.join(f.workspace, ".game-l10n", "omp-control"))).toBe(false);
  });

  test("proves the actual main session, native PID, loaded helper hash and active process tool", async () => {
    const f = fixture();
    const directory = controlLaunch(f.workspace);
    const root = host(f.workspace, "main");
    await root.emit("session_start");
    const file = path.join(directory, "ready.json");
    const receipt = readFileSync(file, "utf8");
    expect(JSON.parse(receipt)).toEqual({
      schema: "parr0t.omp-control-ready.v1", launch_id: "a".repeat(32),
      session_id: root.ctx.sessionManager.getSessionId(), process_id: process.pid,
      helper_sha256: createHash("sha256").update(readFileSync(new URL("../src/game_l10n/templates/omp-prompt-isolation.mjs", import.meta.url))).digest("hex"),
      process_tool: "parr0t_process",
    });
    expect(JSON.parse(receipt).session_id).not.toBe(root.ctx.agent.id);
    await root.emit("session_start");
    const descendant = host(f.workspace);
    descendant.ctx.sessionManager.getSessionId = () => "synthetic-descendant-session";
    await descendant.emit("session_start");
    expect(readFileSync(file, "utf8")).toBe(receipt);
    const otherRoot = host(f.workspace, "main");
    otherRoot.ctx.sessionManager.getSessionId = () => "synthetic-other-root-session";
    await expect(otherRoot.emit("session_start")).rejects.toThrow("already registered to another host session");
    expect(readFileSync(file, "utf8")).toBe(receipt);
    expect(readdirSync(directory)).toEqual(["ready.json"]);
  });

  test("unavailable, unregistered and excluded tools never produce readiness or activate tools", async () => {
    const f = fixture();
    const directory = controlLaunch(f.workspace);
    for (const [api, reason] of [
      [{ getAllTools: undefined }, "host tool availability API required"],
      [{ getAllTools: () => [] }, "parr0t_process is not registered"],
      [{ getActiveTools: () => [] }, "parr0t_process is not active"],
    ]) {
      const session = host(f.workspace, "main", 0, {
        ...api, setActiveTools() { throw new Error("must not activate an excluded tool"); },
      });
      await expect(session.emit("session_start")).rejects.toThrow(reason);
      expect(readdirSync(directory)).toEqual([]);
    }
  });

  test("invalid host identity or another workspace cannot claim launch readiness", async () => {
    const f = fixture();
    const directory = controlLaunch(f.workspace);
    for (const sessionId of [undefined, "", "invalid/session", "x".repeat(129)]) {
      const session = host(f.workspace, "main");
      session.ctx.sessionManager.getSessionId = () => sessionId;
      await expect(session.emit("session_start")).rejects.toThrow("host root session ID is unavailable");
    }
    const other = fixture();
    await expect(host(other.workspace, "main").emit("session_start")).rejects.toThrow("workspace binding");
    const wrongCwd = host(f.workspace, "main");
    wrongCwd.ctx.cwd = other.workspace;
    await expect(wrongCwd.emit("session_start")).rejects.toThrow("workspace binding");
    for (const invalid of [path.join(f.workspace, "a".repeat(32)), path.join(directory, ".."), "relative-control"]) {
      process.env.PARR0T_OMP_CONTROL_DIRECTORY = invalid;
      await expect(host(f.workspace, "main").emit("session_start")).rejects.toThrow("invalid control launch");
    }
    expect(readdirSync(directory)).toEqual([]);
  });
});

describe("OMP descendant communication boundary", () => {
  test("restricts every fresh descendant before session_start or the first user prompt", async () => {
    const f = fixture();
    for (const depth of [1, 2, 3, 8]) {
      const session = host(f.workspace, "sub", depth);
      for (const target of ["agent://Main", "agent://SyntheticRoot", "agent://SyntheticPeer"]) {
        const denial = await deniedWithoutDelivery(session, "write", { path: target, content: "SYNTHETIC_PRIVATE_ARGUMENT" });
        expect(JSON.stringify(denial)).not.toContain("SYNTHETIC_PRIVATE_ARGUMENT");
        expect(JSON.stringify(denial)).not.toContain(target);
      }
      for (const tool of ["eval", "bash", "python", "unknown_tool"]) {
        await deniedWithoutDelivery(session, tool, { command: "synthetic", code: "synthetic" });
      }
    }
  });

  test("native/data markers and session_start replay never determine whether a child is restricted", async () => {
    for (const native of [false, true]) {
      for (const sessionStart of [false, true]) {
        const session = await child(fixture(native), sessionStart);
        await deniedWithoutDelivery(session, "write", { path: "agent://Main", content: "synthetic" });
      }
    }
  });

  test("rejects encoded, case, whitespace, history, message, session, and IRC route targets", async () => {
    const session = host(fixture().workspace);
    const targets = [
      "agent://Main", "history://", "session://synthetic", "sessions://synthetic", "irc://Main",
      "a G e N t : / / Main", "%61gent%3A%2F%2FMain", "%2561gent%253a%252f%252fMain",
      "a%E2%80%8Bgent%3a%2f%2fMain", "ＡＧＥＮＴ：／／Main", "local.txt;HiStOrY://synthetic",
      "xd://eval", "XD: / / builtin", "xd://irc", "xd://history", "peer://SyntheticPeer", "message://Main",
    ];
    let reason;
    for (const target of targets) {
      for (const tool of ["read", "write", "grep", "glob"]) {
        const denied = await deniedWithoutDelivery(session, tool, { path: target, content: "SYNTHETIC_SECRET" });
        reason ??= denied.reason;
        expect(denied.reason).toBe(reason);
        expect(denied.reason).not.toContain(target);
        expect(denied.reason).not.toContain("SYNTHETIC_SECRET");
      }
    }
    for (const name of ["irc", "irc.reply", "functions.send_message", "history", "session_history", "builtin.write", "functions.%65val", "unregistered_extension"]) {
      await deniedWithoutDelivery(session, name, { content: "SYNTHETIC_SECRET", code: "synthetic" });
    }
  });

  test("blocks embedded bash filesystem and eval bridges, including nested wrapper and JSON calls", async () => {
    const session = host(fixture().workspace);
    const calls = [
      ["multi_tool_use.parallel", { tool_uses: [{ recipient_name: "functions.write", parameters: { path: "agent://Main", content: "synthetic" } }] }],
      ["parallel", { tool_uses: [{ recipient_name: "multi_tool_use.parallel", parameters: { tool_uses: [{ recipient_name: "functions.read", parameters: { path: "%68istory%3a//synthetic" } }] } }] }],
      ["call_tool", { name: "write", arguments: JSON.stringify({ path: "irc://Main", content: "synthetic" }) }],
      ["invoke_tool", { tool: "functions.eval", input: { code: "synthetic ordinary-looking code" } }],
      ["eval", { language: "py", code: "await tool.write({'path': 'agent://' + 'Main', 'content': 'synthetic'})" }],
      ["bash", { command: "printf synthetic > agent://Main" }],
      ["functions.bash", { command: "cat history://" }],
      ["multi_tool_use.parallel", { tool_uses: [{ recipient_name: "functions.bash", parameters: { command: "printf synthetic > agent://Main" } }] }],
      ["invoke_tool", { tool: "bash", input: JSON.stringify({ command: "printf synthetic > agent://Main" }) }],
      ["edit", { input: "[agent://Main#ABCD]\nPUT >1:\n+synthetic" }],
      ["call_tool", { name: "unknown_extension", arguments: "{}" }],
    ];
    for (const [name, input] of calls) await deniedWithoutDelivery(session, name, input);
  });

  test("denies parent-history task selectors even when they are not virtual URLs", async () => {
    const session = host(fixture().workspace);
    for (const key of ["sourceSession", "history", "parentHistory", "inheritHistory", "session", "sessionId"]) {
      const task = { task: "Synthetic bound task", model: "fixture/model", [key]: "synthetic-parent-session" };
      await deniedWithoutDelivery(session, "task", { tasks: [task] });
      await deniedWithoutDelivery(session, "task", task);
    }
    for (const agent of ["seance", "SEANCE", "%73eance"]) {
      await deniedWithoutDelivery(session, "task", { tasks: [{ agent, task: "synthetic" }] });
    }
    for (const tools of [["synthetic_callback"], "synthetic_callback"]) {
      await deniedWithoutDelivery(session, "task", { tasks: [{ task: "synthetic", tools }] });
    }
  });

  test("does not censor local data mentioning routing or deny local tools and safe wrappers", async () => {
    const f = fixture();
    const session = host(f.workspace);
    const target = path.join(f.workspace, "synthetic-result.txt");
    const content = "Synthetic data containing agent://Main, history://, and IRC is not routing.";
    expect(await session.call("write", { path: target, content }, input => writeFileSync(input.path, input.content))).toBeUndefined();
    expect(readFileSync(target, "utf8")).toBe(content);
    const localCalls = [
      ["read", { path: target }],
      ["edit", { input: `[${target}#ABCD]\nPUT >1:\n+${content}` }],
      ["grep", { path: f.workspace, pattern: "agent://Main" }],
      ["glob", { path: `${f.workspace}/*.txt` }],
      ["task", { task: "Synthetic task mentions history:// but is not a routing field." }],
      ["wait", { i: "Waiting for synthetic task" }],
      ["todo", { items: [{ text: content, status: "pending" }] }],
      ["parr0t_process", { argv: ["python", "-c", "print('agent://Main')"] }],
      ["read", { path: "xd://parr0t_process" }],
      ["write", { path: "xd://parr0t_process", content: JSON.stringify({ argv: ["python", "-c", "print('synthetic')"] }) }],
      ["multi_tool_use.parallel", { tool_uses: [{ recipient_name: "functions.write", parameters: { path: target, content } }] }],
      ["call_tool", { name: "read", arguments: JSON.stringify({ path: target }) }],
    ];
    for (const [name, input] of localCalls) expect(await session.call(name, input)).toBeUndefined();
    for (const target of ["xd://parr0t_process/extra", "xd://parr0t_process?tool=eval", "xd://unknown_device"]) {
      await deniedWithoutDelivery(session, "write", { path: target, content: "{}" });
    }
  });

  test("root normal routing, embedded tools, and unknown tools remain unaffected", async () => {
    const f = fixture();
    const root = host(f.workspace, "main");
    await root.emit("session_start");
    expect(await root.emit("before_agent_start", { prompt: f.marker, systemPrompt: [] })).toBeUndefined();
    for (const [name, input] of [
      ["write", { path: "agent://SyntheticChild", content: "synthetic root routing" }],
      ["read", { path: "history://" }],
      ["eval", { code: "synthetic" }],
      ["bash", { command: "synthetic" }],
      ["unknown_tool", {}],
    ]) {
      let deliveries = 0;
      expect(await root.call(name, input, () => { deliveries++; })).toBeUndefined();
      expect(deliveries).toBe(1);
    }
  });

  test("unmarked analysis and diagnostic children cannot become communication channels", async () => {
    const f = fixture();
    writeJson(path.join(f.workspace, ".game-l10n/framework-session.json"), { mode: "analysis" });
    for (const name of ["SyntheticDiagnostic", "Parr0tSemanticWorker"]) {
      const diagnostic = host(f.workspace);
      diagnostic.ctx.agent.name = name;
      await diagnostic.emit("before_agent_start", { prompt: "Synthetic diagnosis with no marker.", systemPrompt: [] });
      await deniedWithoutDelivery(diagnostic, "eval", { code: "synthetic diagnosis" });
      await deniedWithoutDelivery(diagnostic, "write", { path: "agent://Main", content: "synthetic diagnostic report" });
    }
    const session = await child(f);
    await session.emit("before_agent_start", { prompt: "Synthetic diagnostic continuation without a marker.", systemPrompt: [] });
    await deniedWithoutDelivery(session, "write", { path: "agent://Main", content: "synthetic" });
  });

  test("relay binding is prefixed into every child task and step, not merely shared context", async () => {
    for (const native of [false, true]) {
      const f = fixture(native);
      const session = await child(f);
      const inputs = [
        { context: f.marker, tasks: [{ task: "Synthetic first task" }, { task: "Synthetic second task" }] },
        { context: f.marker, tasks: [{ task: "Synthetic parent task", steps: ["Synthetic first step", "Synthetic second step"] }] },
        { context: f.marker, tasks: [{ steps: ["Synthetic steps-only task"] }] },
        { steps: ["Synthetic single steps-only task"] },
        { task: "Synthetic single task" },
      ];
      for (const input of inputs) {
        let delivered;
        await session.call("task", input, effective => { delivered = effective; });
        for (const task of delivered.tasks ?? [delivered]) {
          if (task.task !== undefined) expect(task.task.startsWith(`${f.marker}\n`)).toBe(true);
          for (const step of task.steps ?? []) expect(step.startsWith(`${f.marker}\n`)).toBe(true);
        }
      }
      const nested = { tool_uses: [{ recipient_name: "functions.task", parameters: { tasks: [{ task: "Synthetic nested task" }] } }] };
      let effective;
      await session.call("multi_tool_use.parallel", nested, input => { effective = input; });
      expect(effective.tool_uses[0].parameters.tasks[0].task.startsWith(`${f.marker}\n`)).toBe(true);
    }
  });

  test("registers a native argv process using pi.exec with cwd and cancellation but no implicit timeout", async () => {
    const f = fixture();
    const session = host(f.workspace);
    const tool = session.tools.get("parr0t_process");
    expect(tool?.parameters).toBeDefined();
    const signal = new AbortController().signal;
    const argv = ["python", "-c", "print('synthetic argument with spaces')"];
    const result = await tool.execute("synthetic-call", { argv }, signal, undefined, session.ctx);
    expect(session.executions).toEqual([{ command: argv[0], args: argv.slice(1), options: { cwd: f.workspace, signal } }]);
    expect(result.details).toEqual(session.executionResult);
    await tool.execute("synthetic-cwd-call", { argv, cwd: path.join(f.workspace, "synthetic-cwd") }, signal, undefined, session.ctx);
    expect(session.executions[1].options).toEqual({ cwd: path.join(f.workspace, "synthetic-cwd"), signal });
    expect(session.executions.every(call => !Object.hasOwn(call.options, "timeout"))).toBe(true);
  });

  test("changed and malformed core relay claims stay fail-closed without exposing arguments", async () => {
    const f = fixture();
    const session = await child(f);
    const otherRelative = ".game-l10n/native/relay/synthetic-other.json";
    writeJson(path.join(f.workspace, otherRelative), { schema: "parr0t.native-wave-relay.v1", task_bindings: [] });
    await expect(session.emit("before_agent_start", { prompt: `PARR0T_OMP_RELAY=${otherRelative}`, systemPrompt: [] })).rejects.toThrow("child relay binding cannot change");
    for (const marker of ["PARR0T_OMP_RELAY=synthetic-private.json", `${f.marker}\n${f.marker}`, "PARR0T_OMP_RELAY=../synthetic-private.json"]) {
      const invalid = host(f.workspace);
      await expect(invalid.emit("before_agent_start", { prompt: marker, systemPrompt: [] })).rejects.toThrow("PARR0T_OMP_PROMPT_ISOLATION: invalid core relay binding");
      await deniedWithoutDelivery(invalid, "write", { path: "agent://Main", content: "synthetic" });
    }
  });
});
