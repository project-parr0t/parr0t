import { afterEach, describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { accessSync, constants, existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import guard from "../src/game_l10n/templates/omp-isolation-guard.mjs";
import prompt from "../src/game_l10n/templates/omp-prompt-isolation.mjs";
import { hasFrameworkExposure } from "../src/game_l10n/templates/framework-mode-approval.mjs";

const temporary = [];
const envNames = ["PARR0T_OMP_GUARD_SESSION", "PARR0T_OMP_GUARD_WORKSPACE", "PARR0T_OMP_PROMPT_WORKSPACE", "PARR0T_OMP_CONTROL_DIRECTORY"];
const original = Object.fromEntries(envNames.map(key => [key, process.env[key]]));
const entry = fileURLToPath(new URL("../src/game_l10n/templates/native-python.py", import.meta.url));
afterEach(() => {
  for (const directory of temporary.splice(0)) rmSync(directory, { recursive: true, force: true });
  for (const key of envNames) {
    if (original[key] === undefined) delete process.env[key];
    else process.env[key] = original[key];
  }
});
function writeJson(file, data) {
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(data));
}
function snapshot(directory) {
  const output = {};
  function walk(root) {
    for (const item of readdirSync(root, { withFileTypes: true })) {
      const file = path.join(root, item.name);
      if (item.isDirectory()) { output[path.relative(directory, file)] = "directory"; walk(file); }
      else output[path.relative(directory, file)] = readFileSync(file, "utf8");
    }
  }
  walk(directory);
  return output;
}
function schema() { return { min() { return this; }, optional() { return this; }, strict() { return this; } }; }
function host(factory, workspace, kind = "main", depth = 0, id = "SyntheticRoot") {
  const handlers = new Map();
  const tools = new Map();
  const confirmations = [];
  const processes = [];
  factory({
    registerFlag() {}, getFlag() { return workspace; },
    zod: { string: schema, boolean: schema, array: schema, object: schema },
    on(name, handler) { handlers.set(name, handler); },
    registerTool(tool) { tools.set(tool.name, tool); },
    async exec(...args) { processes.push(args); throw new Error("Agent runner must never receive approval proof"); },
  });
  const ctx = {
    cwd: workspace, agent: { kind, depth, id, parentId: kind === "main" ? undefined : "SyntheticRoot" },
    hasUI: true,
    ui: { async confirm(...args) { confirmations.push(args); return true; } },
    sessionManager: { getSessionId: () => kind === "main" ? "synthetic-approval-root-session" : `synthetic-session-${id}` },
    async shutdown() {},
  };
  return {
    ctx, tools, confirmations, processes,
    emit: (name, event = {}) => handlers.get(name)?.(event, ctx),
    call: (toolName, input) => handlers.get("tool_call")({ toolName, input }, ctx),
    approve: (params = { mode: "analysis" }, signal) => tools.get("parr0t_framework_mode").execute("synthetic-call", { user_requested: true, ...params }, signal, undefined, ctx),
  };
}
async function fixture(factory) {
  delete process.env.PARR0T_OMP_CONTROL_DIRECTORY;
  const workspace = realpathSync(mkdtempSync(path.join(os.tmpdir(), "parr0t-approval-synthetic-")));
  temporary.push(workspace);
  writeJson(path.join(workspace, "game-l10n.json"), {});
  writeJson(path.join(workspace, ".game-l10n/control-plane-state.json"), {
    schema: "parr0t.control-plane-state.v1", status: "ready", generation: 7,
    incident_id: "", issue_code: "", incident_code: "",
  });
  const root = host(factory, workspace);
  await root.emit("session_start");
  return { workspace, root };
}
function native(workspace, args, proof) {
  let python;
  for (const executable of process.platform === "win32" ? ["python.exe", "python3.exe"] : ["python3", "python"]) {
    for (const directory of (process.env.PATH ?? "").split(path.delimiter)) {
      if (!directory || !path.isAbsolute(directory)) continue;
      const candidate = path.join(directory, executable);
      try {
        accessSync(candidate, process.platform === "win32" ? constants.F_OK : constants.X_OK);
        if (statSync(candidate).isFile()) { python = realpathSync(candidate); break; }
      } catch { /* Same native discovery as the host helper. */ }
    }
    if (python) break;
  }
  if (!python) throw new Error("Native Python is required for the synthetic integration suite");
  return spawnSync(python, ["-I", "-B", entry, "framework-mode", "--workspace", workspace, ...args], {
    cwd: workspace, shell: false, windowsHide: true, encoding: "utf8", input: proof === undefined ? undefined : JSON.stringify(proof),
  });
}
// Synthetic trusted-host records exercise the actual native verifier, not a stub.
function stageApproval(workspace, overrides = {}) {
  const proof = { id: randomBytes(16).toString("hex"), token: randomBytes(32).toString("hex") };
  const statePath = path.join(workspace, ".game-l10n/framework-session.json");
  const revision = existsSync(statePath) ? JSON.parse(readFileSync(statePath, "utf8")).revision : 0;
  const record = {
    schema: "parr0t.framework-mode-approval.v1", workspace,
    session_id: "synthetic-approval-root-session", agent_id: "SyntheticRoot", agent_kind: "main", agent_depth: 0,
    mode: "analysis", revision, logs: [],
    token_sha256: createHash("sha256").update(proof.token, "ascii").digest("hex"),
    expires_at: Math.floor(Date.now() / 1000) + 120, ...overrides,
  };
  const directory = path.join(workspace, ".game-l10n/framework-approvals");
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  writeFileSync(path.join(directory, `${proof.id}.json`), JSON.stringify(record), { flag: "wx", mode: 0o600 });
  return proof;
}

for (const [label, factory] of [["guarded", guard], ["prompt", prompt]]) {
  describe(`${label} explicit root lifecycle approval`, () => {
    test("flag-only native calls deny unchanged; explicit root request consumes an exact one-shot approval without a second dialog", async () => {
      const { workspace, root } = await fixture(factory);
      const initial = snapshot(workspace);
      const unapproved = native(workspace, ["--mode", "analysis", "--user-requested"]);
      expect(unapproved.status).not.toBe(0);
      expect(snapshot(workspace)).toEqual(initial);
      const log = path.join(workspace, "synthetic-source.log");
      writeFileSync(log, "Synthetic diagnostic bytes, not a real session log.\n");
      expect((await root.call("parr0t_framework_mode", { mode: "analysis", user_requested: true, logs: ["synthetic-source.log"] }))?.block).not.toBe(true);
      const result = await root.approve({ mode: "analysis", logs: ["synthetic-source.log"] });
      expect(result.isError).toBe(false);
      expect(root.processes).toHaveLength(0);
      expect(root.confirmations).toHaveLength(0);
      const lifecycle = JSON.parse(readFileSync(path.join(workspace, ".game-l10n/framework-session.json"), "utf8"));
      expect(lifecycle.mode).toBe("analysis");
      expect(lifecycle.logs).toHaveLength(1);
      const directory = path.join(workspace, ".game-l10n/framework-approvals");
      const files = readdirSync(directory);
      expect(files).toHaveLength(1);
      expect(files[0]).toMatch(/^[a-f0-9]{32}\.consumed\.json$/);
      const record = JSON.parse(readFileSync(path.join(directory, files[0]), "utf8"));
      expect(record).toEqual({
        schema: "parr0t.framework-mode-approval.v1", workspace,
        session_id: "synthetic-approval-root-session", agent_id: "SyntheticRoot", agent_kind: "main", agent_depth: 0,
        mode: "analysis", revision: 0, logs: [log],
        token_sha256: expect.stringMatching(/^[a-f0-9]{64}$/), expires_at: expect.any(Number),
      });
      expect(record.expires_at).toBeGreaterThan(Math.floor(Date.now() / 1000));
      expect(record.expires_at).toBeLessThanOrEqual(Math.floor(Date.now() / 1000) + 120);
      expect(JSON.stringify(result)).not.toContain("token");
      const beforeReplay = snapshot(workspace);
      const replay = native(workspace, ["--mode", "analysis", "--user-requested", "--log", log, "--approval-stdin"], {
        id: files[0].slice(0, 32), token: "0".repeat(64),
      });
      expect(replay.status).not.toBe(0);
      expect(snapshot(workspace)).toEqual(beforeReplay);
    });

    test("confirmed shutdown revokes approved analysis grants without enabling localization", async () => {
      const { workspace, root } = await fixture(factory);
      writeFileSync(path.join(workspace, "synthetic-source.log"), "Synthetic diagnostic bytes.\n");
      expect((await root.approve({ mode: "analysis", logs: ["synthetic-source.log"] })).isError).toBe(false);
      expect((await root.approve({ mode: "shutdown" })).isError).toBe(false);
      const state = JSON.parse(readFileSync(path.join(workspace, ".game-l10n/framework-session.json"), "utf8"));
      expect(state).toEqual({
        schema: "parr0t.framework-session.v1", mode: "shutdown", revision: 2,
        instructions_active: false, user_requested: true, logs: [],
      });
      expect(root.confirmations).toHaveLength(0);
      expect(root.processes).toHaveLength(0);
    });

    test("real native verification rejects changed scope, expired or forged proof without mutation", async () => {
      const { workspace, root } = await fixture(factory);
      writeFileSync(path.join(workspace, "synthetic-source.log"), "Synthetic diagnostic bytes.\n");
      expect((await root.approve({ mode: "analysis", logs: ["synthetic-source.log"] })).isError).toBe(false);
      const other = realpathSync(mkdtempSync(path.join(os.tmpdir(), "parr0t-approval-other-")));
      temporary.push(other);
      const cases = [
        [{}, ["--mode", "shutdown"]],
        [{}, ["--mode", "analysis", "--log", path.join(workspace, "different.log")]],
        [{ workspace: other }, ["--mode", "analysis"]],
        [{ revision: 999 }, ["--mode", "analysis"]],
        [{ expires_at: Math.floor(Date.now() / 1000) - 1 }, ["--mode", "analysis"]],
        [{ agent_kind: "sub", agent_depth: 1 }, ["--mode", "analysis"]],
        [{ session_id: "" }, ["--mode", "analysis"]],
        [{ token_sha256: "0".repeat(64) }, ["--mode", "analysis"]],
      ];
      for (const [overrides, args] of cases) {
        const proof = stageApproval(workspace, overrides);
        const initial = snapshot(workspace);
        const result = native(workspace, [...args, "--user-requested", "--approval-stdin"], proof);
        expect(result.status).not.toBe(0);
        expect(snapshot(workspace)).toEqual(initial);
        expect(result.stdout + result.stderr).not.toContain(proof.token);
      }
    }, 30_000);

    test("an exact consumed native proof cannot replay or replace analysis grants", async () => {
      const { workspace } = await fixture(factory);
      const proof = stageApproval(workspace);
      const args = ["--mode", "analysis", "--user-requested", "--approval-stdin"];
      expect(native(workspace, args, proof).status).toBe(0);
      const initial = snapshot(workspace);
      const replay = native(workspace, args, proof);
      expect(replay.status).not.toBe(0);
      expect(snapshot(workspace)).toEqual(initial);
      expect(replay.stdout + replay.stderr).not.toContain(proof.token);
    });

    test("malformed private proof denies before creating lifecycle locks or receipts", async () => {
      const { workspace } = await fixture(factory);
      const initial = snapshot(workspace);
      for (const proof of [null, {}, { id: "a".repeat(32) }, { id: "a".repeat(32), token: "b".repeat(63) },
        { id: "a".repeat(32), token: "b".repeat(64), confirmed: true }]) {
        expect(native(workspace, ["--mode", "analysis", "--user-requested", "--approval-stdin"], proof).status).not.toBe(0);
        expect(snapshot(workspace)).toEqual(initial);
      }
    });

    test("explicit root requests work without interactive UI and preserve an actual cancellation", async () => {
      const { workspace, root } = await fixture(factory);
      root.ctx.hasUI = false;
      root.ctx.ui = { confirm: async () => { throw new Error("a second dialog is forbidden"); } };
      const initial = snapshot(workspace);
      const cancelled = new AbortController();
      cancelled.abort();
      await expect(root.approve({ mode: "pause" }, cancelled.signal)).rejects.toThrow("cancelled");
      expect(snapshot(workspace)).toEqual(initial);
      for (const value of [undefined, false, 1, "true"]) {
        const request = { mode: "analysis", user_requested: value };
        expect((await root.call("parr0t_framework_mode", request)).block).toBe(true);
        await expect(root.approve(request)).rejects.toThrow("explicit user_requested");
        expect(snapshot(workspace)).toEqual(initial);
      }
      expect((await root.approve({ mode: "shutdown" })).isError).toBe(false);
      expect(root.processes).toHaveLength(0);
      expect(root.confirmations).toHaveLength(0);
    });

    test("clean lifecycle resumes in-session but successful root history access requires canonical fresh recovery", async () => {
      const { workspace, root } = await fixture(factory);
      const statePath = path.join(workspace, ".game-l10n/control-plane-state.json");
      for (const mode of ["pause", "analysis", "shutdown", "localization"]) {
        expect((await root.approve({ mode })).isError).toBe(false);
      }
      expect(JSON.parse(readFileSync(statePath, "utf8")).generation).toBe(7);
      expect(JSON.parse(readFileSync(statePath, "utf8")).status).toBe("ready");
      await root.emit("tool_result", { toolName: "read", input: { path: "history://synthetic" }, isError: true });
      expect(JSON.parse(readFileSync(statePath, "utf8")).status).toBe("ready");
      await root.emit("tool_result", { toolName: "read", input: { path: "history://synthetic" }, isError: false });
      expect(JSON.parse(readFileSync(statePath, "utf8")).issue_code).toBe("control_plane_leak");
      expect((await root.approve({ mode: "localization" })).isError).toBe(true);
      expect(JSON.parse(readFileSync(statePath, "utf8")).status).toBe("rotation_required");
      expect(root.confirmations).toHaveLength(0);
    });

    test("successful private file-URL reads prevent clean resume while failed reads leave it available", async () => {
      const { workspace, root } = await fixture(factory);
      expect((await root.approve({ mode: "analysis" })).isError).toBe(false);
      const privateTask = path.join(workspace, ".game-l10n/data-plane/private # \uD55C\uAE00/task.json");
      writeJson(privateTask, { synthetic_private_source: "private fixture, not real game data" });
      const uri = pathToFileURL(privateTask).href;
      const statePath = path.join(workspace, ".game-l10n/control-plane-state.json");
      await root.emit("tool_result", { toolName: "read", input: { path: uri }, isError: true });
      expect(JSON.parse(readFileSync(statePath, "utf8")).status).toBe("ready");
      expect(JSON.parse(readFileSync(fileURLToPath(uri), "utf8"))).toEqual({
        synthetic_private_source: "private fixture, not real game data",
      });
      await root.emit("tool_result", { toolName: "read", input: { path: `${uri}:1-2` }, isError: false });
      const exposed = JSON.parse(readFileSync(statePath, "utf8"));
      expect(exposed.status).toBe("rotation_required");
      expect(exposed.issue_code).toBe("control_plane_leak");
      expect(exposed.incident_code).toBe("parent_opened_data_plane");
      expect((await root.approve({ mode: "localization" })).isError).toBe(true);
      expect(JSON.parse(readFileSync(statePath, "utf8"))).toEqual(exposed);
      expect(root.confirmations).toHaveLength(0);
    });

    test("recursive private search results quarantine the root, but zero matches and metadata-only hits do not", async () => {
      const { workspace, root } = await fixture(factory);
      expect((await root.approve({ mode: "analysis" })).isError).toBe(false);
      const searchPath = path.join(workspace, ".game-l10n/data-plane");
      const privateFile = path.join(searchPath, "search-fixture/task.json");
      writeJson(privateFile, { synthetic_source: "recursive source fixture" });
      const statePath = path.join(workspace, ".game-l10n/control-plane-state.json");
      const zero = { toolName: "grep", input: { path: ".game-l10n/data-plane", pattern: "absent" },
        isError: false, details: { searchPath, cwd: workspace, matchCount: 0, files: [] } };
      await root.emit("tool_result", zero);
      await root.emit("tool_result", { ...zero, toolName: "glob",
        details: { files: [privateFile] } });
      await root.emit("tool_result", { ...zero,
        details: { searchPath, cwd: workspace, matchCount: 1, files: ["relay.json"] } });
      expect(JSON.parse(readFileSync(statePath, "utf8")).status).toBe("ready");
      const directoryResult = { ...zero,
        details: { searchPath, cwd: workspace, matchCount: 1, files: ["search-fixture/task.json"] } };
      expect(hasFrameworkExposure(directoryResult, root.ctx, workspace)).toBe(true);
      expect(hasFrameworkExposure({ ...directoryResult, details: { matchCount: 1 } }, root.ctx, workspace)).toBe(true);
      expect(readFileSync(privateFile, "utf8")).toContain("recursive source fixture");
      const broadResult = { ...directoryResult, input: { path: ".", pattern: "recursive source fixture" },
        details: { searchPath: workspace, cwd: workspace, matchCount: 1,
          fileMatches: [{ path: ".game-l10n/data-plane/search-fixture/task.json", count: 1 }] } };
      await root.emit("tool_result", broadResult);
      expect(JSON.parse(readFileSync(statePath, "utf8")).status).toBe("rotation_required");
      expect((await root.approve({ mode: "localization" })).isError).toBe(true);
    });

    test("missing host authority, child roles and spoofed consent deny hooks and direct execute", async () => {
      const { workspace, root } = await fixture(factory);
      const initial = snapshot(workspace);
      const originalAgent = { ...root.ctx.agent };
      const originalManager = root.ctx.sessionManager;
      for (const agent of [undefined, { ...originalAgent, kind: "sub", depth: 1 }, { ...originalAgent, kind: "sub", depth: 2 },
        { ...originalAgent, kind: "sub", depth: 3, name: "DiagnosticReview" }, { ...originalAgent, depth: 1 },
        { ...originalAgent, depth: undefined }, { ...originalAgent, id: "" }]) {
        root.ctx.agent = agent;
        expect((await root.call("parr0t_framework_mode", { mode: "analysis" })).block).toBe(true);
        await expect(root.approve()).rejects.toThrow("root");
      }
      root.ctx.agent = originalAgent;
      for (const sessionManager of [undefined, {}, { getSessionId: () => "" }]) {
        root.ctx.sessionManager = sessionManager;
        await expect(root.approve()).rejects.toThrow("root");
      }
      root.ctx.sessionManager = originalManager;
      for (const extra of [{ root: true }, { approval: { id: "a".repeat(32), token: "b".repeat(64) } },
        { confirmed: true }, { runner: "forged" }, { contracts_read: true }, { text_free_confirmed: true }]) {
        const request = { mode: "analysis", ...extra };
        expect((await root.call("functions.parr0t_framework_mode", request)).block).toBe(true);
        await expect(root.approve(request)).rejects.toThrow("invalid lifecycle request");
      }
      for (const request of [{ mode: "unknown" }, { mode: "shutdown", logs: ["synthetic-source.log"] }]) {
        expect((await root.call("parr0t_framework_mode", request)).block).toBe(true);
        await expect(root.approve(request)).rejects.toThrow("invalid lifecycle request");
      }
      expect(root.confirmations).toHaveLength(0);
      expect(root.processes).toHaveLength(0);
      expect(snapshot(workspace)).toEqual(initial);
    });


    test("all roles deny lifecycle and approval writes, edits, aliases, renames and nested wrappers", async () => {
      const { workspace, root } = await fixture(factory);
      const state = ".game-l10n/framework-session.json";
      const approval = ".game-l10n/framework-approvals/" + "a".repeat(32) + ".json";
      const alias = path.join(workspace, "synthetic-metadata-alias");
      symlinkSync(path.join(workspace, ".game-l10n"), alias, process.platform === "win32" ? "junction" : "dir");
      const calls = [
        ["write", { path: state, content: "forged" }],
        ["functions.write", { path: "./.game-l10n/nested/../framework-session.json", content: "forged" }],
        ["write", { path: "%2egame-l10n/framework-approvals/forged.json", content: "forged" }],
        ["write", { path: "local://" + state, content: "forged" }],
        ["write", { path: "synthetic-metadata-alias/framework-session.json", content: "forged" }],
        ["edit", { input: `*** Begin Patch\n[${state}#ABCD]\nPUT 1.=1:\n+forged\n*** End Patch\n` }],
        ["edit", { input: `*** Begin Patch\n[ordinary.json#ABCD]\nMV ${approval}\n*** End Patch\n` }],
        ["edit", { input: `*** Begin Patch\n*** Update File: ${state}\n@@\n-forged\n+other\n*** End Patch` }],
        ["rename", { source: "ordinary.json", destination: state }],
        ["rename", { from: ".game-l10n", to: "moved-metadata" }],
        ["ast_edit", { paths: [state], ops: [] }],
        ["write", { path: "xd://ast_edit", content: JSON.stringify({ paths: [approval], ops: [] }) }],
        ["write", { path: "xd://write", content: JSON.stringify({ path: state, content: "forged" }) }],
        ["write", { path: "xd://edit", content: `*** Begin Patch\n[${state}#ABCD]\nREM\n*** End Patch` }],
        ["write", { path: "xd://parr0t_framework_mode", content: JSON.stringify({ mode: "analysis", user_requested: true }) }],
        ["write", { path: "xd://write", content: JSON.stringify({ path: "xd://parr0t_framework_mode", content: JSON.stringify({ mode: "analysis", user_requested: true }) }) }],
        ["ast_edit", { paths: [".game-l10n/*"], ops: [] }],
        ["ast_edit", { paths: ["synthetic-metadata-alias/*"], ops: [] }],
        ["multi_tool_use.parallel", { tool_uses: [{ recipient_name: "functions.write", parameters: { path: approval, content: "forged" } }] }],
        ["call_tool", { name: "parallel", arguments: JSON.stringify({ calls: [{ tool: "edit", input: { input: `*** Begin Patch\n[${state}#ABCD]\nREM\n*** End Patch` } }] }) }],
      ];
      for (const agent of [{ kind: "main", depth: 0 }, { kind: "sub", depth: 1 }, { kind: "sub", depth: 2 }, { kind: "sub", depth: 3, name: "DiagnosticReview" }]) {
        Object.assign(root.ctx.agent, agent);
        for (const [tool, input] of calls) {
          const decision = await root.call(tool, input);
          expect(decision?.block, `${label} ${agent.kind}/${agent.depth}: ${tool} ${JSON.stringify(input)}`).toBe(true);
        }
      }
      expect(root.confirmations).toHaveLength(0);
      expect(root.processes).toHaveLength(0);
      expect(existsSync(path.join(workspace, state))).toBe(false);
      expect(existsSync(path.join(workspace, approval))).toBe(false);
    });

    test("a real diagnostic child cannot call the root approval tool directly", async () => {
      const { workspace, root } = await fixture(factory);
      const result = await root.approve();
      expect(result.isError).toBe(false);
      if (factory === guard) expect((await root.emit("before_subagent_spawn", { invocationKind: "task", spawnKey: "DiagnosticReview" }))?.block).not.toBe(true);
      const diagnostic = host(factory, workspace, "sub", 1, "DiagnosticReview");
      await diagnostic.emit("session_start");
      await diagnostic.emit("before_agent_start", { prompt: "Inspect synthetic reusable code", systemPrompt: [] });
      const initial = snapshot(workspace);
      expect((await diagnostic.call("parr0t_framework_mode", { mode: "shutdown" })).block).toBe(true);
      await expect(diagnostic.approve({ mode: "shutdown" })).rejects.toThrow("actual root");
      expect(diagnostic.confirmations).toHaveLength(0);
      expect(snapshot(workspace)).toEqual(initial);
    });
  });
}
