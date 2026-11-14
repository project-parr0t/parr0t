import { afterEach, describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import guard from "../src/game_l10n/templates/omp-isolation-guard.mjs";

const template = fileURLToPath(new URL("../src/game_l10n/templates/omp-isolation-guard.mjs", import.meta.url));
const core = path.resolve(path.dirname(template), "../../..");
const temporary = [];
const originalSession = process.env.PARR0T_OMP_GUARD_SESSION;
const originalWorkspace = process.env.PARR0T_OMP_GUARD_WORKSPACE;
const originalControlDirectory = process.env.PARR0T_OMP_CONTROL_DIRECTORY;
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
function host(workspace, kind = "main", depth = 0, name = "Anything") {
  const handlers = new Map();
  const tools = new Map();
  const shape = { optional() { return this; } };
  const pi = {
    registerFlag() {}, getFlag() { return kind === "main" ? workspace : undefined; },
    zod: { string: () => shape, array: () => shape, object: () => shape },
    on(name, handler) { handlers.set(name, handler); },
    registerTool(tool) { tools.set(tool.name, tool); },
  };
  const ctx = {
    cwd: workspace,
    agent: { kind, depth, name, id: kind === "main" ? "Main" : `synthetic-${depth}`, parentId: "synthetic-parent" },
    sessionManager: { getSessionId: () => kind === "main" ? "synthetic-root-session" : "synthetic-child-session" },
    shutdownCount: 0,
    shutdown() { this.shutdownCount++; },
  };
  guard(pi);
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
async function fixture() {
  const workspace = mkdtempSync(path.join(os.tmpdir(), "parr0t-guard-synthetic-"));
  temporary.push(workspace);
  writeJson(path.join(workspace, ".game-l10n/control-plane-state.json"), { generation: 7 });
  const root = host(workspace);
  await root.emit("session_start");
  const registrationPath = path.join(workspace, ".game-l10n/native/omp-isolation.json");
  const registration = JSON.parse(readFileSync(registrationPath, "utf8"));
  const relayRelative = ".game-l10n/data-plane/synthetic/relay.json";
  const relayPath = path.join(workspace, relayRelative);
  const relayMetadata = {
    schema: "parr0t.data-plane-relay.v4", task_id: "synthetic-task-id",
    task_path: ".game-l10n/data-plane/synthetic/task.json",
    launch_contract: { message_isolation: registration, tasks: ["worker-0"] },
  };
  writeJson(relayPath, relayMetadata);
  const marker = `PARR0T_OMP_RELAY=${relayRelative}`;
  async function child(depth, name = "Anything") {
    const session = host(workspace, "sub", depth, name);
    await session.emit("session_start");
    await session.emit("before_agent_start", { prompt: `${marker}\nSynthetic assignment; no game data.` });
    return session;
  }
  return { workspace, root, registrationPath, registration, relayPath, relayMetadata, relayRelative, marker, child };
}

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
    const nativeMetadata = { schema: "parr0t.native-wave-relay.v1", task_bindings: [{ task: "synthetic-task.json" }], launch_contract: { message_isolation: f.registration, tasks: ["synthetic-task.json"] } };
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
    await expect(stale.emit("session_start")).rejects.toThrow("stale");
    expect(stale.ctx.shutdownCount).toBe(1);
    expect((await stale.call("read", { path: "synthetic.json" })).block).toBe(true);
  });
});
