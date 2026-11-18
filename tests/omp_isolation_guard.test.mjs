import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, rmdirSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import guard from "../src/game_l10n/templates/omp-isolation-guard.mjs";

const template = fileURLToPath(new URL("../src/game_l10n/templates/omp-isolation-guard.mjs", import.meta.url));
const core = path.resolve(path.dirname(template), "../../..");
const temporary = [];
const originalSession = process.env.PARR0T_OMP_GUARD_SESSION;
const originalWorkspace = process.env.PARR0T_OMP_GUARD_WORKSPACE;
const originalControlDirectory = process.env.PARR0T_OMP_CONTROL_DIRECTORY;
beforeEach(() => { delete process.env.PARR0T_OMP_CONTROL_DIRECTORY; });
afterEach(() => {
  for (const directory of temporary.splice(0)) rmSync(directory, { recursive: true, force: true });
  if (originalSession === undefined) delete process.env.PARR0T_OMP_GUARD_SESSION;
  else process.env.PARR0T_OMP_GUARD_SESSION = originalSession;
  if (originalWorkspace === undefined) delete process.env.PARR0T_OMP_GUARD_WORKSPACE;
  else process.env.PARR0T_OMP_GUARD_WORKSPACE = originalWorkspace;
  if (originalControlDirectory === undefined) delete process.env.PARR0T_OMP_CONTROL_DIRECTORY;
  else process.env.PARR0T_OMP_CONTROL_DIRECTORY = originalControlDirectory;
});
function writeJson(file, value) {
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(value));
}
function host(workspace, kind = "main", depth = 0, name = "Anything", factory = guard, sessionId = "synthetic-root-session") {
  const handlers = new Map();
  const tools = new Map();
  const shape = { optional() { return this; }, strict() { return this; } };
  const pi = {
    registerFlag() {}, getFlag() { return kind === "main" ? workspace : undefined; },
    zod: { string: () => shape, array: () => shape, object: () => shape },
    on(name, handler) { handlers.set(name, handler); },
    registerTool(tool) { tools.set(tool.name, tool); },
  };
  const ctx = {
    cwd: workspace,
    agent: { kind, depth, name, id: kind === "main" ? "Main" : `synthetic-${depth}`, parentId: "synthetic-parent" },
    sessionManager: { getSessionId: () => kind === "main" ? sessionId : "synthetic-child-session" },
    shutdownCount: 0,
    shutdown() { this.shutdownCount++; },
  };
  factory(pi);
  return {
    ctx, handlers, tools,
    emit: async (name, event = {}) => handlers.get(name)?.(event, ctx),
    async call(toolName, input, deliver = () => {}) {
      const decision = await handlers.get("tool_call")({ toolName, input }, ctx);
      if (!decision?.block) await deliver(decision?.input ?? input);
      return decision;
    },
    exec(params, signal) { return tools.get("parr0t_exec").execute("synthetic-call", params, signal, undefined, ctx); },
  };
}
async function fixture(factory = guard) {
  const workspace = mkdtempSync(path.join(os.tmpdir(), "parr0t-guard-synthetic-"));
  temporary.push(workspace);
  writeJson(path.join(workspace, ".game-l10n/control-plane-state.json"), { generation: 7 });
  const root = host(workspace, "main", 0, "Anything", factory);
  await root.emit("session_start");
  const registrationPath = path.join(workspace, ".game-l10n/native/omp-isolation", `${createHash("sha256").update("synthetic-root-session").digest("hex")}.json`);
  const registration = JSON.parse(readFileSync(registrationPath, "utf8"));
  const relayRelative = ".game-l10n/data-plane/synthetic/relay.json";
  const relayPath = path.join(workspace, relayRelative);
  const relayMetadata = {
    schema: "parr0t.data-plane-relay.v4", task_id: "synthetic-task-id",
    task_path: ".game-l10n/data-plane/synthetic/task.json",
    expected_envelope: ".game-l10n/data-plane/synthetic/control-envelope.g7.attempt-1.json",
    launch_contract: { message_isolation: registration, tasks: ["worker-0"] },
  };
  writeJson(relayPath, relayMetadata);
  const taskPath = path.join(workspace, relayMetadata.task_path);
  function setPolicy({ semantic = false, network = false, sources = [] } = {}) {
    writeJson(taskPath, {
      content_source_policy: {
        schema: "parr0t.content-source-policy.v1",
        policy_digest: semantic ? "synthetic-semantic-policy" : "synthetic-inventory-policy",
        semantic_content_generation: semantic,
        content_network_access: network,
        allowed_external_sources: sources,
      },
    });
  }
  setPolicy();
  const marker = `PARR0T_OMP_RELAY=${relayRelative}`;
  async function child(depth, name = "Anything") {
    const session = host(workspace, "sub", depth, name, factory);
    await session.emit("session_start");
    await session.emit("before_agent_start", { prompt: `${marker}\nSynthetic assignment; no game data.` });
    return session;
  }
  return { workspace, root, registrationPath, registration, relayPath, relayMetadata, relayRelative, marker, child, setPolicy };
}
function framework(mode, revision = 1, logs = []) {
  return { schema: "parr0t.framework-session.v1", mode, revision, instructions_active: mode === "localization", user_requested: true, logs };
}

describe("live framework instruction lifecycle", () => {
  test("the same main host withdraws only owned rules and changes analysis/shutdown instructions per turn", async () => {
    const f = await fixture();
    const statePath = path.join(f.workspace, ".game-l10n/framework-session.json");
    const systemPrompt = ["Host/system policy remains binding.", "Existing Parr0t localization instruction block."];
    const event = { prompt: "Synthetic code diagnosis", systemPrompt };
    expect(await f.root.emit("before_agent_start", event)).toBeUndefined();
    writeJson(statePath, framework("localization"));
    expect(await f.root.emit("before_agent_start", event)).toBeUndefined();
    const content = "Synthetic diagnostic snapshot, never a real log.";
    const sha256 = createHash("sha256").update(content).digest("hex");
    const snapshot = { path: `.game-l10n/diagnostics/logs/${sha256}.log`, sha256, bytes: Buffer.byteLength(content) };
    mkdirSync(path.dirname(path.join(f.workspace, snapshot.path)), { recursive: true });
    writeFileSync(path.join(f.workspace, snapshot.path), content);
    writeJson(statePath, framework("analysis", 2, [snapshot]));
    const analysis = await f.root.emit("before_agent_start", event);
    expect(analysis.systemPrompt.slice(0, -1)).toEqual(systemPrompt);
    expect(event.systemPrompt).toBe(systemPrompt);
    expect(systemPrompt).toHaveLength(2);
    const instructions = analysis.systemPrompt.at(-1);
    expect(instructions).not.toContain(content);
    expect(instructions).not.toContain(snapshot.path);
    // Inactive root diagnosis must not refresh/parse contaminated control-plane metadata.
    writeFileSync(path.join(f.workspace, ".game-l10n/control-plane-state.json"), "invalid synthetic metadata");
    for (const toolName of ["read", "write", "edit", "grep", "glob", "bash", "eval"]) {
      expect((await f.root.call(toolName, { path: "src/synthetic.py", input: "synthetic", command: "synthetic", code: "synthetic" }))?.block).not.toBe(true);
    }
    writeJson(statePath, framework("shutdown", 3));
    const shutdown = await f.root.emit("before_agent_start", event);
    expect(shutdown.systemPrompt.slice(0, -1)).toEqual(systemPrompt);
    expect(shutdown.systemPrompt.at(-1)).toContain("mode: shutdown");
    expect(shutdown.systemPrompt.at(-1)).toContain("shutdown permits no diagnostic log reads");
    expect(shutdown.systemPrompt.at(-1)).not.toContain("Read diagnostic logs ONLY");
    writeJson(statePath, framework("analysis", 4));
    expect((await f.root.emit("before_agent_start", event)).systemPrompt.at(-1)).toContain("mode: analysis");
    expect(f.root.ctx.shutdownCount).toBe(0);
    expect(readFileSync(path.join(f.workspace, snapshot.path), "utf8")).toBe(content);
  });

  test("mode changes deny new root/relay dispatch and existing child work without stopping children or deleting results", async () => {
    const f = await fixture();
    const relay = await f.child(1);
    const worker = await f.child(2);
    const statePath = path.join(f.workspace, ".game-l10n/framework-session.json");
    const input = { tasks: [{ model: "provider/exact-model", task: "Synthetic work" }] };
    expect((await f.root.call("task", input))?.block).not.toBe(true);
    const completed = path.join(f.workspace, "synthetic-completed.json");
    writeJson(completed, { completed: true });
    let deliveries = 0;
    for (const [index, mode] of ["analysis", "shutdown"].entries()) {
      writeJson(statePath, framework(mode, index + 1));
      expect((await f.root.call("task", input))?.block === true).toBe(mode === "shutdown");
      for (const session of [f.root, relay, worker]) {
        for (const invocationKind of ["task", "eval", "workpool", "custom"]) {
          expect((await session.emit("before_subagent_spawn", { invocationKind })).block).toBe(true);
        }
      }
      for (const session of [relay, worker]) {
        for (const toolName of ["task", "wait", "read", "write", "edit", "grep", "glob", "parr0t_exec", "bash", "eval"]) {
          expect((await session.call(toolName, { path: completed, argv: ["-c", "pass"], ...input }, () => deliveries++)).block).toBe(true);
        }
        for (const toolName of ["read", "write"]) expect((await session.call(toolName, { path: "xd://parr0t_exec", content: "{}" }, () => deliveries++)).block).toBe(true);
        await expect(session.exec({ argv: ["-c", "pass"] })).rejects.toThrow("framework is inactive");
        await expect(session.emit("before_agent_start", { prompt: f.marker })).rejects.toThrow("framework is inactive");
        expect((await session.call("yield", {}))?.block).not.toBe(true);
        expect(session.ctx.shutdownCount).toBe(0);
      }
      const newChild = host(f.workspace, "sub", 2);
      await newChild.emit("session_start");
      await expect(newChild.emit("before_agent_start", { prompt: f.marker })).rejects.toThrow("framework is inactive");
    }
    expect(deliveries).toBe(0);
    expect(JSON.parse(readFileSync(completed, "utf8"))).toEqual({ completed: true });
    const evidence = readFileSync(path.join(f.workspace, ".game-l10n/native/omp-isolation-events.jsonl"), "utf8").trim().split("\n").map(JSON.parse);
    expect(evidence.every(record => ["framework_inactive", "diagnostic_admission_invalid"].includes(record.issue_code))).toBe(true);
  });

  test("a dangling lifecycle link never reactivates root, relay, or worker admission", async () => {
    const f = await fixture();
    const relay = await f.child(1);
    const worker = await f.child(2);
    const target = path.join(f.workspace, "synthetic-missing-state");
    mkdirSync(target);
    symlinkSync(target, path.join(f.workspace, ".game-l10n/framework-session.json"), process.platform === "win32" ? "junction" : "dir");
    rmdirSync(target);
    let deliveries = 0;
    for (const session of [f.root, relay, worker]) {
      await expect(session.emit("before_agent_start", { prompt: f.marker, systemPrompt: ["Host policy"] })).rejects.toThrow("invalid framework session state");
      for (const toolName of ["read", "task", "parr0t_exec"]) {
        const decision = await session.call(toolName, { path: "synthetic.py" }, () => deliveries++);
        expect(decision.block).toBe(true);
      }
      expect((await session.emit("before_subagent_spawn", { invocationKind: "task", spawnKey: "SyntheticChild" })).block).toBe(true);
      await expect(session.exec({ argv: ["-c", "print('MUST_NOT_EXECUTE')"] })).rejects.toThrow("invalid framework session state");
    }
    expect(deliveries).toBe(0);
    expect((await f.root.emit("tool_result", {})).additionalContext).toContain("lifecycle metadata is invalid");
  });

  test("malformed state fails closed categorically before turns, tools, spawns, and direct executor entry", async () => {
    const f = await fixture();
    const worker = await f.child(2);
    const statePath = path.join(f.workspace, ".game-l10n/framework-session.json");
    const sha256 = "a".repeat(64);
    const log = { path: `.game-l10n/diagnostics/logs/${sha256}.log`, sha256, bytes: 0 };
    const valid = framework("analysis", 1, [log]);
    const malformed = [null, [], "PRIVATE_SYNTHETIC_SENTINEL", {},
      { ...valid, schema: "wrong" }, { ...valid, mode: "other" },
      { ...valid, revision: 0 }, { ...valid, revision: 1.5 }, { ...valid, revision: Number.MAX_SAFE_INTEGER + 1 },
      { ...valid, instructions_active: true }, { ...valid, instructions_active: 0 },
      { ...valid, user_requested: false }, { ...valid, user_requested: "true" },
      { ...valid, logs: {} }, { ...valid, extra: "PRIVATE_SYNTHETIC_SENTINEL" },
      { ...framework("shutdown"), logs: [log] }, { ...framework("localization"), logs: [log] },
      ...[null, [], {}, { ...log, extra: true }, { ...log, bytes: -1 }, { ...log, bytes: 0.5 },
        { ...log, bytes: Number.MAX_SAFE_INTEGER + 1 }, { ...log, sha256: sha256.toUpperCase() },
        { ...log, sha256: "short" }, { ...log, path: "../PRIVATE_SYNTHETIC_SENTINEL.log" },
        { ...log, path: `C:/synthetic/${sha256}.log` }, { ...log, path: `agent://Main/${sha256}.log` },
        { ...log, path: `.game-l10n/diagnostics/logs/../logs/${sha256}.log` },
        { ...log, path: `.game-l10n/diagnostics/logs/${"b".repeat(64)}.log` },
      ].map(item => ({ ...valid, logs: [item] })),
    ];
    const missingKey = { ...valid };
    delete missingKey.logs;
    malformed.push(missingKey);
    let deliveries = 0;
    for (const serialized of ["{invalid PRIVATE_SYNTHETIC_SENTINEL", ...malformed.map(value => JSON.stringify(value))]) {
      writeFileSync(statePath, serialized);
      for (const session of [f.root, worker]) {
        await expect(session.emit("before_agent_start", { prompt: f.marker, systemPrompt: ["Host policy"] })).rejects.toThrow("invalid framework session state");
        for (const toolName of ["read", "task", "parr0t_exec"]) {
          const decision = await session.call(toolName, { path: "synthetic.py" }, () => deliveries++);
          expect(decision).toEqual({ block: true, reason: "PARR0T_OMP_ISOLATION: invalid framework session state" });
        }
        expect((await session.emit("before_subagent_spawn", { invocationKind: "task" })).block).toBe(true);
        await expect(session.exec({ argv: ["-c", "pass"] })).rejects.toThrow("invalid framework session state");
      }
    }
    expect(deliveries).toBe(0);
    const evidence = readFileSync(path.join(f.workspace, ".game-l10n/native/omp-isolation-events.jsonl"), "utf8");
    expect(evidence).not.toContain("PRIVATE_SYNTHETIC_SENTINEL");
    expect(evidence.trim().split("\n").map(JSON.parse).every(record => record.issue_code === "framework_state_invalid")).toBe(true);
    // A repaired malformed file is reread rather than poisoning the live guard.
    writeJson(statePath, framework("localization"));
    expect((await f.root.call("task", { tasks: [{ task: "Synthetic" }] }))?.block).not.toBe(true);
  });

  test("hosts initialized during suspension observe later core lifecycle recovery", async () => {
    const f = await fixture();
    const statePath = path.join(f.workspace, ".game-l10n/framework-session.json");
    writeJson(statePath, framework("analysis"));
    const fresh = host(f.workspace);
    await fresh.emit("session_start");
    await fresh.emit("before_agent_start", { prompt: "Synthetic recovery", systemPrompt: ["Host policy"] });
    expect((await fresh.call("task", { tasks: [{ task: "Optional code inspection" }] }))?.block).not.toBe(true);
    // Python recovery tests own canonical handoff validation; these are its
    // synthetic committed lifecycle/generation outputs at the host boundary.
    writeJson(statePath, framework("localization", 2));
    writeJson(path.join(f.workspace, ".game-l10n/control-plane-state.json"), { generation: 8 });
    expect(await fresh.emit("before_agent_start", { prompt: "Synthetic", systemPrompt: ["Host policy"] })).toBeUndefined();
    expect((await fresh.call("task", {}))?.block).not.toBe(true);
    expect(JSON.parse(readFileSync(f.registrationPath, "utf8")).control_generation).toBe(8);
  });

  test("analysis permits optional ordinary code reviewers without translation relay requirements", async () => {
    const f = await fixture();
    writeJson(path.join(f.workspace, ".game-l10n/framework-session.json"), framework("analysis"));
    const task = { tasks: [{ task: "Inspect synthetic reusable code" }] };
    expect((await f.root.call("task", task)).input).toEqual(task);
    expect((await f.root.emit("before_subagent_spawn", { invocationKind: "task", spawnKey: "DiagnosticReview-1" }))?.block).not.toBe(true);
    const reviewer = host(f.workspace, "sub", 1, "Anything");
    Object.assign(reviewer.ctx.agent, { id: "DiagnosticReview-1", parentId: f.root.ctx.agent.id });
    await reviewer.emit("session_start");
    const policy = await reviewer.emit("before_agent_start", { prompt: "Inspect reusable code directly", systemPrompt: ["Host policy"] });
    expect(policy.systemPrompt[0]).toBe("Host policy");
    let delivered = 0;
    for (const toolName of ["read", "bash", "eval", "lsp"]) {
      await reviewer.call(toolName, { path: "synthetic.py", code: "synthetic", command: "synthetic" }, () => delivered++);
    }
    expect(delivered).toBe(4);
    const execution = await reviewer.exec({ argv: ["-c", "print('SYNTHETIC_DIAGNOSTIC_EXEC')"] });
    expect(execution.isError).toBe(false);
    expect(execution.details.stdout.trim()).toBe("SYNTHETIC_DIAGNOSTIC_EXEC");
    expect((await reviewer.call("write", { path: "agent://Main", content: "unapproved message" })).block).toBe(true);
    expect((await f.root.call("task", { tasks: [{ agent: "seance", sourceSession: "unapproved-history", task: "Inspect" }] })).block).toBe(true);
    expect((await reviewer.emit("before_subagent_spawn", { invocationKind: "task", spawnKey: "DiagnosticReview-2" }))?.block).not.toBe(true);
    const descendant = host(f.workspace, "sub", 2);
    Object.assign(descendant.ctx.agent, { id: "DiagnosticReview-2", parentId: reviewer.ctx.agent.id });
    await descendant.emit("session_start");
    await descendant.emit("before_agent_start", { prompt: "Inspect code", systemPrompt: [] });
    expect((await descendant.call("bash", { command: "synthetic" }))?.block).not.toBe(true);
    writeJson(path.join(f.workspace, ".game-l10n/framework-session.json"), framework("shutdown", 2));
    expect((await reviewer.call("bash", { command: "synthetic" })).block).toBe(true);
    writeJson(path.join(f.workspace, ".game-l10n/framework-session.json"), framework("localization", 3));
    expect((await reviewer.call("read", { path: "synthetic.py" })).block).toBe(true);
  });

  test("existing localization children and spoofed diagnostic identities do not gain diagnostic privileges", async () => {
    const f = await fixture();
    const worker = await f.child(2);
    writeJson(path.join(f.workspace, ".game-l10n/framework-session.json"), framework("analysis"));
    await f.root.emit("before_subagent_spawn", { invocationKind: "task", spawnKey: worker.ctx.agent.id });
    expect((await worker.call("bash", { command: "synthetic" })).block).toBe(true);
    const unrecorded = host(f.workspace, "sub", 2, "DiagnosticReview");
    Object.assign(unrecorded.ctx.agent, { id: "Unrecorded", parentId: f.root.ctx.agent.id });
    await unrecorded.emit("session_start");
    await expect(unrecorded.emit("before_agent_start", { prompt: "PARR0T_OMP_DIAGNOSTIC=approved", systemPrompt: [] })).rejects.toThrow("framework is inactive");
    await f.root.emit("before_subagent_spawn", { invocationKind: "task", spawnKey: "WrongParent" });
    const wrongParent = host(f.workspace, "sub", 1);
    Object.assign(wrongParent.ctx.agent, { id: "WrongParent", parentId: "NotTheAdmittingParent" });
    await expect(wrongParent.emit("session_start")).rejects.toThrow("diagnostic admission");
  });

  test("mode changes refresh trusted continuation guidance without another agent preparation", async () => {
    const f = await fixture();
    await f.root.emit("before_agent_start", { prompt: "Synthetic", systemPrompt: ["Host policy"] });
    const statePath = path.join(f.workspace, ".game-l10n/framework-session.json");
    writeJson(statePath, framework("analysis"));
    const withdrawn = await f.root.emit("tool_result", { toolName: "bash", isError: false });
    expect(withdrawn.additionalContext).toContain("mode: analysis");
    expect(await f.root.emit("tool_result", {})).toBeUndefined();
    writeJson(statePath, framework("localization", 2));
    writeJson(path.join(f.workspace, ".game-l10n/control-plane-state.json"), { generation: 8 });
    const restored = await f.root.emit("tool_result", { toolName: "bash", isError: false });
    expect(restored.additionalContext).toContain("Earlier inactive-only guidance no longer applies");
    expect((await f.root.call("task", { tasks: [{ task: "Synthetic bound task" }] }))?.block).not.toBe(true);
  });
});

describe("file-backed OMP host/message isolation", () => {
  test("incident write agent://Main is denied before target delivery, regardless of supplied name", async () => {
    const f = await fixture();
    for (const depth of [0, 1, 2, 3]) {
      const child = await f.child(depth, "Main");
      let deliveries = 0;
      const decision = await child.call("write", { path: "agent://Main", content: "synthetic private text" }, () => deliveries++);
      expect(decision.block).toBe(true);
      expect(deliveries).toBe(0);
    }
  });
  test("all virtual writer URIs and normalized aliases fail before dispatch", async () => {
    const f = await fixture();
    const worker = await f.child(2);
    for (const target of ["AGENT://Other", " agent : // Main ", "%61gent%3a%2f%2fMain", "%2561gent%253a%252f%252fMain", "out://sink", "proc://Main/kill", "xd://edit", "local://shared", "ssh://host/path", "file://server/share", "https://example.invalid/"]) {
      expect((await worker.call("write", { path: target, content: "synthetic" })).block).toBe(true);
      expect((await worker.call("edit", { input: `[${target}#ABCD]\nPUT 1.=1:\n+synthetic` })).block).toBe(true);
    }
  });
  test("deferred native tool device permits local work but cannot become a relay code bridge", async () => {
    const f = await fixture();
    const worker = await f.child(2);
    expect((await worker.call("read", { path: "xd://parr0t_exec" }))?.block).not.toBe(true);
    const output = path.join(f.workspace, "native-device-result.json");
    const argv = ["-c", `from pathlib import Path;Path(${JSON.stringify(output)}).write_text('{"completed":true}',encoding='utf-8')`];
    let executed;
    await worker.call("write", { path: "xd://parr0t_exec", content: JSON.stringify({ argv }) }, async input => { executed = await worker.exec(JSON.parse(input.content)); });
    expect(executed.isError).toBe(false);
    expect(JSON.parse(readFileSync(output, "utf8"))).toEqual({ completed: true });
    const relay = await f.child(1);
    await expect(relay.call("write", { path: "xd://parr0t_exec", content: JSON.stringify({ argv: ["-c", "print('forbidden')"] }) }, async input => relay.exec(JSON.parse(input.content)))).rejects.toThrow("fixed core CLI");
    expect((await worker.call("write", { path: "xd://edit", content: "{}" })).block).toBe(true);
  });
  test("semantic workers deny network tools, arbitrary processes, and unbound local reads", async () => {
    const f = await fixture();
    f.setPolicy({ semantic: true });
    const worker = await f.child(2);
    expect((await worker.call("web_search", { query: "synthetic" })).block).toBe(true);
    expect((await worker.call("read", { path: "https://example.invalid/source" })).block).toBe(true);
    expect((await worker.call("parr0t_exec", { argv: ["-c", "print('synthetic')"] })).block).toBe(true);
    const unbound = path.join(f.workspace, "local.json");
    expect((await worker.call("read", { path: unbound })).block).toBe(true);
    const task = path.join(f.workspace, f.relayMetadata.task_path);
    expect((await worker.call("read", { path: task }))?.block).not.toBe(true);
    expect((await worker.call("grep", { path: task, pattern: "synthetic" }))?.block).not.toBe(true);
    expect((await worker.call("glob", { path: task }))?.block).not.toBe(true);
    expect((await worker.call("read", { path: path.join(f.workspace, "..", "outside.json") })).block).toBe(true);
  });
  test("semantic workers admit only an exact externally granted read target", async () => {
    const f = await fixture();
    const exact = "https://example.invalid/exact-source";
    f.setPolicy({ semantic: true, network: true, sources: [exact] });
    const worker = await f.child(2);
    expect((await worker.call("read", { path: exact }))?.block).not.toBe(true);
    expect((await worker.call("read", { path: exact + "/other" })).block).toBe(true);
    expect((await worker.call("web_search", { query: `site:example.invalid synthetic` })).block).toBe(true);
  });
  test("denial evidence is categorical, has no input text, and never rotates generation", async () => {
    const f = await fixture();
    const worker = await f.child(2);
    const before = readFileSync(f.registrationPath, "utf8");
    await worker.call("write", { path: "agent://Main", content: "PRIVATE_SYNTHETIC_SENTINEL" });
    const evidence = readFileSync(path.join(f.workspace, ".game-l10n/native/omp-isolation-events.jsonl"), "utf8");
    const records = evidence.trim().split("\n").map(line => JSON.parse(line));
    expect(records).toEqual([{ session: "synthetic-child-session", role: "worker", depth: 2, tool: "write", issue_code: "virtual_target_forbidden" }]);
    expect(evidence).not.toContain("agent://");
    expect(evidence).not.toContain("PRIVATE_SYNTHETIC_SENTINEL");
    expect(readFileSync(f.registrationPath, "utf8")).toBe(before);
    expect(JSON.parse(readFileSync(path.join(f.workspace, ".game-l10n/control-plane-state.json"), "utf8"))).toEqual({ generation: 7 });
  });
  test("worker local writes/edits/read/grep/glob remain available inside workspace", async () => {
    const f = await fixture();
    const worker = await f.child(2);
    const target = path.join(f.workspace, "synthetic-result.json");
    await worker.call("write", { path: target, content: "synthetic" }, input => writeFileSync(input.path, input.content));
    expect(readFileSync(target, "utf8")).toBe("synthetic");
    expect((await worker.call("edit", { input: `[${target}#ABCD]\nPUT 1.=1:\n+replacement` }))?.block).not.toBe(true);
    for (const tool of ["read", "grep", "glob"]) expect((await worker.call(tool, { path: target }))?.block).not.toBe(true);
    expect((await worker.call("write", { path: path.join(f.workspace, "..", "outside.json"), content: "no" })).block).toBe(true);
  });
  test("built-in eval/bash and generic/custom callback bridges fail closed", async () => {
    const f = await fixture();
    for (const depth of [1, 2]) {
      const child = await f.child(depth);
      for (const tool of ["eval", "bash", "exec", "tool.write", "custom_writer", "web_search", "ask", "browser", "completion"]) {
        let delivered = false;
        const result = await child.call(tool, { code: 'await tool.write({path:"agent://Main",content:"synthetic"})', command: "write agent://Main" }, () => { delivered = true; });
        expect(result.block).toBe(true);
        expect(delivered).toBe(false);
      }
    }
  });
  test("relay task remains available, workers cannot delegate or wait", async () => {
    const f = await fixture();
    const relay = await f.child(1);
    const input = { context: "Synthetic explicit context only", tasks: [{ name: "Main", model: "provider/exact-model:high", task: "Bound synthetic work", sourceSession: "", tools: [] }] };
    const result = await relay.call("task", input);
    expect(result.block).not.toBe(true);
    expect(result.input.tasks[0].task).toBe(`${f.marker}\nBound synthetic work`);
    expect(result.input.tasks[0].model).toBe("provider/exact-model:high");
    expect(result.input.tasks[0]).not.toHaveProperty("sourceSession");
    expect(result.input.tasks[0]).not.toHaveProperty("tools");
    expect(input.tasks[0].sourceSession).toBe("");
    expect((await relay.call("wait", {}))?.block).not.toBe(true);
    const worker = await f.child(2);
    expect((await worker.call("task", input)).block).toBe(true);
    expect((await worker.call("wait", {})).block).toBe(true);
    expect((await worker.emit("before_subagent_spawn", { invocationKind: "task" })).block).toBe(true);
    expect((await relay.emit("before_subagent_spawn", { invocationKind: "task" }))?.block).not.toBe(true);
    for (const patch of [{ tools: ["shared_writer"] }, { model: ["provider/exact-model"] }, { model: "@role" }, { sourceSession: "private-history" }, { history: [] }]) {
      const changed = structuredClone(input);
      Object.assign(changed.tasks[0], patch);
      expect((await relay.call("task", changed)).block).toBe(true);
    }
  });
  test("root eval remains trusted but eval/workpool/custom spawn origins cannot carry callbacks", async () => {
    const f = await fixture();
    expect((await f.root.call("eval", { code: "trusted root computation" }))?.block).not.toBe(true);
    expect((await f.root.emit("before_subagent_spawn", { invocationKind: "task" }))?.block).not.toBe(true);
    for (const invocationKind of ["eval", "agent", "workpool", "custom", undefined]) {
      expect((await f.root.emit("before_subagent_spawn", { invocationKind })).block).toBe(true);
    }
    const evidence = readFileSync(path.join(f.workspace, ".game-l10n/native/omp-isolation-events.jsonl"), "utf8").trim().split("\n").map(JSON.parse);
    expect(evidence).toHaveLength(5);
    expect(evidence.every(record => record.issue_code === "spawn_origin_forbidden" && record.role === "root" && record.tool === "task")).toBe(true);
  });
  test("root preserves nonempty/seance sourceSession and changes only empty non-seance option", async () => {
    const f = await fixture();
    const source = { tasks: [{ agent: "seance", sourceSession: "" }, { sourceSession: "kept" }, { sourceSession: "" }] };
    const result = await f.root.call("task", source);
    expect(result.input.tasks[0].sourceSession).toBe("");
    expect(result.input.tasks[1].sourceSession).toBe("kept");
    expect(result.input.tasks[2]).not.toHaveProperty("sourceSession");
    expect((await f.root.call("bash", { command: "trusted root command" }))?.block).not.toBe(true);
    expect((await f.root.call("write", { path: "agent://Main", content: "trusted root" }))?.block).not.toBe(true);
  });
  test("relay reads metadata/public docs, never private task/result/log or virtual sources", async () => {
    const f = await fixture();
    const relay = await f.child(1);
    for (const target of [f.relayPath, path.join(core, "AGENTS.md"), path.join(core, "docs/control-plane-contract.md")]) {
      expect((await relay.call("read", { path: target }))?.block).not.toBe(true);
    }
    for (const target of [".game-l10n/data-plane/synthetic/task.json", "private-result.json", "worker.log", "agent://Worker", "history://Worker", "https://example.invalid/"]) {
      expect((await relay.call("read", { path: target })).block).toBe(true);
    }
    expect((await relay.call("glob", { path: "**/*" })).block).toBe(true);
    expect((await relay.call("grep", { path: f.workspace, pattern: "anything" })).block).toBe(true);
  });
  test("safe exec passes argv literally to fixed native Python, with bytecode disabled", async () => {
    const f = await fixture();
    const worker = await f.child(2);
    const literal = "agent://Main; echo must-not-be-a-shell | %PATH%";
    const result = await worker.exec({ argv: ["-c", "import json,os,sys;print(json.dumps([sys.argv[1],os.environ.get('PYTHONDONTWRITEBYTECODE'),sys.dont_write_bytecode]))", literal], cwd: f.workspace });
    expect(result.isError).toBe(false);
    expect(JSON.parse(result.details.stdout)).toEqual([literal, "1", true]);
    expect(result.details.outputComplete).toBe(true);
    expect(result.details.truncated).toEqual({ stdout: false, stderr: false });
    expect(result.details.totalBytes).toEqual(result.details.retainedBytes);
    await expect(worker.exec({ argv: ["-c", "pass"], cwd: "agent://Main" })).rejects.toThrow("virtual");
  });
  for (const layout of ["checkout", "installed"]) {
    test(`${layout} guard imports its adjacent Python package and keeps public read roots bounded`, async () => {
      const directory = mkdtempSync(path.join(os.tmpdir(), "parr0t-package-synthetic-"));
      temporary.push(directory);
      const installation = path.join(directory, "synthetic 'package é");
      const importRoot = path.join(installation, layout === "checkout" ? "src" : "site-packages");
      const packageRoot = path.join(importRoot, "game_l10n");
      const guardPath = path.join(packageRoot, "templates/omp-isolation-guard.mjs");
      mkdirSync(path.dirname(guardPath), { recursive: true });
      writeFileSync(guardPath, readFileSync(template));
      writeFileSync(path.join(path.dirname(guardPath), "framework-mode-approval.mjs"), readFileSync(path.join(path.dirname(template), "framework-mode-approval.mjs")));
      writeFileSync(path.join(packageRoot, "templates/native-relay-lifecycle.mjs"), readFileSync(path.join(path.dirname(template), "native-relay-lifecycle.mjs")));
      writeFileSync(path.join(packageRoot, "templates/native-python.py"), readFileSync(path.join(path.dirname(template), "native-python.py")));
      writeFileSync(path.join(packageRoot, "__init__.py"), "");
      const cliPath = path.join(packageRoot, "cli.py");
      writeFileSync(cliPath, "import json, sys\nprint(json.dumps({'module': __file__, 'isolated': sys.flags.isolated, 'bytecode_disabled': sys.dont_write_bytecode, 'args': sys.argv[1:]}))\n");
      if (layout === "checkout") writeFileSync(path.join(installation, "pyproject.toml"), "[project]\nname = 'synthetic'\n");
      mkdirSync(path.join(installation, "docs"), { recursive: true });
      const contract = path.join(installation, "docs/control-plane-contract.md");
      writeFileSync(contract, "Synthetic public contract.\n");
      writeFileSync(path.join(installation, "AGENTS.md"), "Synthetic public instructions.\n");
      const factory = (await import(pathToFileURL(guardPath).href)).default;
      const f = await fixture(factory);
      expect(f.registration.guard_sha256).toBe(createHash("sha256").update(readFileSync(guardPath)).digest("hex"));
      const worker = await f.child(2);
      const coreDirectory = layout === "checkout" ? installation : packageRoot;
      const workerResult = await worker.exec({ argv: ["-c", "import game_l10n,json;print(json.dumps(game_l10n.__file__))"], cwd: coreDirectory });
      expect(workerResult.isError).toBe(false);
      expect(path.resolve(JSON.parse(workerResult.details.stdout))).toBe(path.join(packageRoot, "__init__.py"));
      // A workspace module must not shadow the fixed relay lifecycle CLI.
      writeFileSync(path.join(f.workspace, "game_l10n.py"), "raise RuntimeError('synthetic workspace shadow')\n");
      const relay = await f.child(1);
      const argv = ["-m", "game_l10n.cli", "record-native-launch", "--workspace", f.workspace, "--relay", f.relayRelative, "--event", "capability-available"];
      const result = await relay.exec({ argv });
      expect(result.isError).toBe(false);
      const observed = JSON.parse(result.details.stdout);
      expect(path.resolve(observed.module)).toBe(cliPath);
      expect(observed.isolated).toBe(1);
      expect(observed.bytecode_disabled).toBe(true);
      expect(observed.args).toEqual(argv.slice(2));
      expect((await relay.call("read", { path: contract }))?.block === true).toBe(layout === "installed");
      expect((await relay.call("read", { path: path.join(installation, "AGENTS.md") }))?.block === true).toBe(layout === "installed");
      expect((await relay.call("read", { path: `${f.relayPath}:1-2:raw` }))?.block).not.toBe(true);
      expect((await relay.call("read", { path: path.join(packageRoot, "cli.py") })).block).toBe(true);
      expect((await relay.call("read", { path: path.join(f.workspace, "game_l10n.py") })).block).toBe(true);
    });
  }
  test("relay exec rejects free code/scripts, wrong workspace/relay and non-metadata CLI", async () => {
    const f = await fixture();
    const relay = await f.child(1);
    for (const argv of [
      ["-c", "print('synthetic')"], ["some-script.py"],
      ["-m", "game_l10n.cli", "status", "--workspace", f.workspace],
      ["-m", "game_l10n.cli", "seal-data-plane-task", "--workspace", f.workspace, "--relay", "private-task.json"],
      ["-m", "game_l10n.cli", "seal-data-plane-task", "--workspace", path.dirname(f.workspace), "--relay", f.relayPath],
      ["-m", "game_l10n.cli", "seal-data-plane-task", "--workspace", f.workspace, "--relay", f.relayPath, "--unknown", "value"],
    ]) await expect(relay.exec({ argv })).rejects.toThrow("PARR0T_OMP_ISOLATION");
  });
  test("relay lifecycle argv is validated before dispatch for both metadata relay families", async () => {
    const f = await fixture();
    const relay = await f.child(1);
    let deliveries = 0;
    const allowed = ["-m", "game_l10n.cli", "seal-data-plane-task", "--workspace", f.workspace, "--relay", f.relayRelative, "--worker-ended"];
    expect((await relay.call("parr0t_exec", { argv: allowed }, () => deliveries++))?.block).not.toBe(true);
    expect((await relay.call("parr0t_exec", { argv: ["-c", "open('private-task.json').read()"] }, () => deliveries++)).block).toBe(true);
    expect(deliveries).toBe(1);
    const nativeRelative = ".game-l10n/native/relay/synthetic-wave-0001.json";
    const nativeManifest = ".game-l10n/native/relay/synthetic-manifest.json";
    const nativeMetadata = { schema: "parr0t.native-wave-relay.v1", manifest: nativeManifest, task_bindings: [{ task: "synthetic-task.json" }], launch_contract: { message_isolation: f.registration, tasks: ["synthetic-task.json"] } };
    writeJson(path.join(f.workspace, nativeManifest), { content_source_policy: { schema: "parr0t.content-source-policy.v1", policy_digest: "synthetic-semantic-policy", semantic_content_generation: true, content_network_access: false, allowed_external_sources: [] } });
    writeJson(path.join(f.workspace, nativeRelative), nativeMetadata);
    const native = host(f.workspace, "sub", 1);
    await native.emit("session_start");
    await native.emit("before_agent_start", { prompt: `PARR0T_OMP_RELAY=${nativeRelative}` });
    const nativeArgs = ["-m", "game_l10n.cli", "seal-native-wave-relay", "--workspace", f.workspace, "--dispatch", nativeRelative, "--ended-task", "synthetic-task.json", "--relay-ended"];
    expect((await native.call("parr0t_exec", { argv: nativeArgs }, () => deliveries++))?.block).not.toBe(true);
    expect(deliveries).toBe(2);
    const unbound = nativeArgs.map(arg => arg === "synthetic-task.json" ? "other-task.json" : arg);
    expect((await native.call("parr0t_exec", { argv: unbound }, () => deliveries++)).block).toBe(true);
    expect(deliveries).toBe(2);
    const started = ["-m", "game_l10n.cli", "record-native-launch", "--workspace", f.workspace, "--relay", f.relayRelative, "--event", "worker-started", "--task", "worker-0", "--worker-id", "synthetic-worker", "--model", "provider/exact-model"];
    expect((await relay.call("parr0t_exec", { argv: started, cwd: core }, () => deliveries++))?.block).not.toBe(true);
    const wrongKey = started.map(arg => arg === "worker-0" ? "worker-99" : arg);
    expect((await relay.call("parr0t_exec", { argv: wrongKey, cwd: core }, () => deliveries++)).block).toBe(true);
    expect(deliveries).toBe(3);
  });
  test("truncated native output is explicitly incomplete and error-marked", async () => {
    const f = await fixture();
    const worker = await f.child(2);
    const result = await worker.exec({ argv: ["-c", "import sys;sys.stdout.write('x'*1048581);sys.stderr.write('ok')"] });
    expect(result.isError).toBe(true);
    expect(result.details.outputComplete).toBe(false);
    expect(result.details.truncated).toEqual({ stdout: true, stderr: false });
    expect(result.details.totalBytes.stdout).toBe(1048581);
    expect(result.details.retainedBytes.stdout).toBe(1048576);
    expect(result.details.stderr).toBe("ok");
  });
  test("signal cancellation is honored without implicit deadline", async () => {
    const f = await fixture();
    const worker = await f.child(2);
    const controller = new AbortController();
    const running = worker.exec({ argv: ["-c", "import time;time.sleep(60)"] }, controller.signal);
    controller.abort();
    expect((await running).details.cancelled).toBe(true);
    await expect(worker.exec({ argv: ["-c", "pass"] }, controller.signal)).rejects.toThrow("cancelled");
  });
  test("independent roots retain separate registration during interleaved calls", async () => {
    const f = await fixture();
    const before = readFileSync(f.registrationPath, "utf8");
    const other = host(f.workspace, "main", 0, "Anything", guard, "synthetic-other-root");
    await other.emit("session_start");
    const otherPath = path.join(f.workspace, ".game-l10n/native/omp-isolation", `${createHash("sha256").update("synthetic-other-root").digest("hex")}.json`);
    const otherProof = readFileSync(otherPath, "utf8");
    await f.root.call("read", { path: "trusted-root" });
    const worker = await f.child(2);
    await other.call("read", { path: "trusted-root" });
    expect(readFileSync(f.registrationPath, "utf8")).toBe(before);
    expect(readFileSync(otherPath, "utf8")).toBe(otherProof);
    await f.root.call("read", { path: "trusted-root" });
    expect((await worker.call("read", { path: "synthetic-input.json" }))?.block).not.toBe(true);
  });
  test("only loaded guard bytes can be certified after the file is replaced", async () => {
    const f = await fixture();
    const copied = path.join(f.workspace, "synthetic-core/src/game_l10n/templates/omp-isolation-guard.mjs");
    mkdirSync(path.dirname(copied), { recursive: true });
    const original = readFileSync(template);
    writeFileSync(copied, original);
    writeFileSync(path.join(path.dirname(copied), "framework-mode-approval.mjs"), readFileSync(path.join(path.dirname(template), "framework-mode-approval.mjs")));
    writeFileSync(path.join(path.dirname(copied), "native-relay-lifecycle.mjs"), readFileSync(path.join(path.dirname(template), "native-relay-lifecycle.mjs")));
    const loaded = (await import(pathToFileURL(copied).href)).default;
    const root = host(f.workspace, "main", 0, "Anything", loaded);
    await root.emit("session_start");
    writeFileSync(copied, Buffer.concat([original, Buffer.from("\n// Synthetic replacement.\n")]));
    await root.call("read", { path: "trusted-root" });
    const proof = JSON.parse(readFileSync(f.registrationPath, "utf8"));
    expect(proof.guard_sha256).toBe(createHash("sha256").update(original).digest("hex"));
    expect(proof.guard_sha256).not.toBe(createHash("sha256").update(readFileSync(copied)).digest("hex"));
  });
  test("localization final prose and yield payloads expose only bound replies", async () => {
    const f = await fixture();
    for (const depth of [1, 2]) {
      const child = await f.child(depth);
      const expected = depth === 1 ? ".game-l10n/data-plane/synthetic/receipt.json" : f.relayMetadata.expected_envelope;
      const message = { content: [{ type: "text", text: "PRIVATE_SYNTHETIC_FINAL" }, { type: "text", text: "PRIVATE_SYNTHETIC_MORE" }] };
      const rewritten = await child.emit("assistant_message", { message });
      expect(rewritten.content.map(block => block.text).join("")).toBe(expected);
      expect(message.content[0].text).toBe("PRIVATE_SYNTHETIC_FINAL");
      let delivered;
      await child.call("yield", { data: { text: "PRIVATE_SYNTHETIC_YIELD" }, error: "PRIVATE_SYNTHETIC_ERROR", type: "PRIVATE_SECTION" }, input => { delivered = input; });
      expect(delivered).toEqual({ data: expected });
      const call = { type: "toolCall", id: "synthetic-call", name: "read", arguments: { path: "synthetic-input.json" } };
      const work = await child.emit("assistant_message", { message: { content: [{ type: "text", text: "PRIVATE_COMMENTARY" }, call] } });
      expect(work.content).toEqual([{ type: "text", text: "" }, call]);
    }
    const unbound = host(f.workspace, "sub", 2);
    const reply = await unbound.emit("assistant_message", { message: { content: [{ type: "text", text: "PRIVATE_SYNTHETIC_FINAL" }] } });
    expect(reply.content).toEqual([{ type: "text", text: "E_NATIVE_MESSAGE_ISOLATION_REQUIRED" }]);
  });
  test("diagnostic prose and yield remain available to an admitted analysis child", async () => {
    const f = await fixture();
    writeJson(path.join(f.workspace, ".game-l10n/framework-session.json"), framework("analysis"));
    await f.root.emit("before_subagent_spawn", { invocationKind: "task", spawnKey: "SyntheticDiagnostic" });
    const child = host(f.workspace, "sub", 1);
    Object.assign(child.ctx.agent, { id: "SyntheticDiagnostic", parentId: f.root.ctx.agent.id });
    await child.emit("session_start");
    await child.emit("before_agent_start", { prompt: "Synthetic code diagnosis", systemPrompt: [] });
    const message = { content: [{ type: "text", text: "Synthetic public code finding" }] };
    expect(await child.emit("assistant_message", { message })).toBeUndefined();
    let delivered;
    await child.call("yield", { data: "Synthetic public code finding" }, input => { delivered = input; });
    expect(delivered).toEqual({ data: "Synthetic public code finding" });
  });
  test("invalid launch events and missing worker arguments never start Python", async () => {
    const f = await fixture();
    const relay = await f.child(1);
    const base = ["-m", "game_l10n.cli", "record-native-launch", "--workspace", f.workspace, "--relay", f.relayRelative];
    let deliveries = 0;
    for (const tail of [[], ["--event", "synthetic-invalid"], ["--event", "worker-started"],
      ["--event", "capability-available", "--worker-id", "synthetic-worker"]]) {
      const decision = await relay.call("parr0t_exec", { argv: [...base, ...tail] }, () => deliveries++);
      expect(decision.block).toBe(true);
    }
    expect(deliveries).toBe(0);
    const allowed = [...base, "--event", "capability-available"];
    expect((await relay.call("parr0t_exec", { argv: allowed }, () => deliveries++))?.block).not.toBe(true);
    expect(deliveries).toBe(1);
  });
  test("an approved Parr0t selection cannot be rejected without observed execution evidence", async () => {
    const f = await fixture();
    f.relayMetadata.launch_contract.model_authorization = { status: "approved", scope: "all_sdks" };
    writeJson(f.relayPath, f.relayMetadata);
    const relay = await f.child(1);
    const base = ["-m", "game_l10n.cli", "record-native-launch", "--workspace", f.workspace, "--relay", f.relayRelative];
    let deliveries = 0;
    expect((await relay.call("parr0t_exec", { argv: [...base, "--event", "model-unavailable"] }, () => deliveries++)).block).toBe(true);
    expect((await relay.call("parr0t_exec", { argv: [...base, "--event", "worker-started", "--task", "worker-0", "--worker-id", "synthetic-worker", "--model", "fixture/exact", "--failure-code", "host_dispatch_denied"] }, () => deliveries++)).block).toBe(true);
    expect(deliveries).toBe(0);
    expect((await relay.call("parr0t_exec", { argv: [...base, "--event", "model-unavailable", "--failure-code", "host_dispatch_denied"] }, () => deliveries++))?.block).not.toBe(true);
    expect(deliveries).toBe(1);
  });
  test("only root registers actual session/PID/digest and refreshes generation", async () => {
    const f = await fixture();
    expect(Object.keys(f.registration).sort()).toEqual(["schema", "control_generation", "session_id", "process_id", "guard_sha256"].sort());
    expect(f.registration.session_id).toBe("synthetic-root-session");
    expect(f.registration.process_id).toBe(process.pid);
    expect(f.registration.guard_sha256).toBe(createHash("sha256").update(readFileSync(template)).digest("hex"));
    const before = readFileSync(f.registrationPath, "utf8");
    const worker = await f.child(2);
    expect(readFileSync(f.registrationPath, "utf8")).toBe(before);
    expect(process.env.PARR0T_OMP_GUARD_SESSION).toBe("synthetic-root-session");
    writeJson(path.join(f.workspace, ".game-l10n/control-plane-state.json"), { generation: 8 });
    expect((await worker.call("write", { path: "synthetic.json", content: "no stale task" })).block).toBe(true);
    await f.root.call("read", { path: "trusted-root" });
    expect(JSON.parse(readFileSync(f.registrationPath, "utf8")).control_generation).toBe(8);
    expect((await worker.call("write", { path: "synthetic.json", content: "no stale proof" })).block).toBe(true);
  });
  test("fresh control launch acknowledges actual host identity independently of an executable shim", async () => {
    const f = await fixture();
    const directory = path.join(f.workspace, ".game-l10n/omp-control", "a".repeat(32));
    mkdirSync(directory, { recursive: true });
    process.env.PARR0T_OMP_CONTROL_DIRECTORY = directory;
    await f.root.call("read", { path: "trusted-root" });
    const readyPath = path.join(directory, "ready.json");
    const ready = JSON.parse(readFileSync(readyPath, "utf8"));
    expect(ready.launch_id).toBe("a".repeat(32));
    expect(ready.process_id).toBe(process.pid);
    expect(ready.session_id).toBe(f.registration.session_id);
    await f.child(2);
    expect(JSON.parse(readFileSync(readyPath, "utf8"))).toEqual(ready);
  });
  test("inherited child guard initializes before first task without replaying root CLI flags", async () => {
    const f = await fixture();
    const inherited = host(f.workspace, "sub", 2);
    await inherited.emit("before_agent_start", { prompt: f.marker });
    expect((await inherited.call("write", { path: "agent://Main", content: "synthetic denied message" })).block).toBe(true);
    let delivered = false;
    expect((await inherited.call("write", { path: path.join(f.workspace, "result.json"), content: "{}" }, () => { delivered = true; }))?.block).not.toBe(true);
    expect(delivered).toBe(true);
    expect(JSON.parse(readFileSync(f.registrationPath, "utf8"))).toEqual(f.registration);
  });
  test("missing startup and malformed/stale relay proof fail closed", async () => {
    const f = await fixture();
    const unstarted = host(f.workspace, "sub", 2);
    expect((await unstarted.call("write", { path: "agent://Main", content: "no" })).block).toBe(true);
    const child = host(f.workspace, "sub", 2);
    await child.emit("session_start");
    await expect(child.emit("before_agent_start", { prompt: "no marker" })).rejects.toThrow("binding");
    f.relayMetadata.launch_contract.message_isolation.guard_sha256 = "wrong";
    writeJson(f.relayPath, f.relayMetadata);
    await expect(child.emit("before_agent_start", { prompt: f.marker })).rejects.toThrow("proof");
    expect((await child.call("write", { path: "synthetic.json", content: "no" })).block).toBe(true);
    delete process.env.PARR0T_OMP_GUARD_SESSION;
    const stale = host(f.workspace, "sub", 2);
    await expect(stale.emit("session_start")).rejects.toMatchObject({ issueCode: "isolation_policy_denied" });
    expect(stale.ctx.shutdownCount).toBe(1);
    expect((await stale.call("read", { path: "synthetic.json" })).block).toBe(true);
  });
});
