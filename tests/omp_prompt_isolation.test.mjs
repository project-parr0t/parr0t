import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import promptIsolation from "../src/game_l10n/templates/omp-prompt-isolation.mjs";

const temporary = [];
const originalWorkspace = process.env.PARR0T_OMP_PROMPT_WORKSPACE;
afterEach(() => {
  for (const directory of temporary.splice(0)) rmSync(directory, { recursive: true, force: true });
  if (originalWorkspace === undefined) delete process.env.PARR0T_OMP_PROMPT_WORKSPACE;
  else process.env.PARR0T_OMP_PROMPT_WORKSPACE = originalWorkspace;
});
function writeJson(file, value) {
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(value));
}
function host(workspace, kind = "sub") {
  const handlers = new Map();
  const flags = new Map();
  const tools = new Map();
  promptIsolation({
    registerFlag(name, options) { flags.set(name, options); },
    getFlag() { return kind === "main" ? workspace : undefined; },
    on(name, handler) { handlers.set(name, handler); },
    registerTool(tool) { tools.set(tool.name, tool); },
  });
  const ctx = { cwd: workspace, agent: { kind, depth: kind === "main" ? 0 : 2, id: "SyntheticChild" } };
  return {
    handlers, flags, tools, ctx,
    emit: async (name, event = {}) => handlers.get(name)?.(event, ctx),
    async call(toolName, input, deliver = () => {}) {
      const decision = await handlers.get("tool_call")({ toolName, input }, ctx);
      if (!decision?.block) await deliver(input);
      return decision;
    },
  };
}
function fixture(native = false) {
  const workspace = mkdtempSync(path.join(os.tmpdir(), "parr0t-prompt-synthetic-"));
  temporary.push(workspace);
  const relative = native ? ".game-l10n/native/relay/synthetic-wave.json" : ".game-l10n/data-plane/synthetic/relay.json";
  const relay = path.join(workspace, relative);
  writeJson(relay, native
    ? { schema: "parr0t.native-wave-relay.v1", task_bindings: [] }
    : { schema: "parr0t.data-plane-relay.v4", task_id: "synthetic-task" });
  return { workspace, relay, marker: `PARR0T_OMP_RELAY=${relative}` };
}
const conflict = [
  "Host/system policy: preserve synthetic system safety rules.",
  "⁂ Role\nWorker agent: delegated tasks.",
  "⁂ Context\nSynthetic reusable-code context.",
  "⁂ Coop",
  "You are operating on a piece of work assigned to you by the main agent.",
  "# Peers",
  "Message peers via `write` with `path: \"agent://<id>\"` and `content`.",
  "- `Main` — main (main, running)",
  "- Messages: only a question that blocks you or a blocker/decision report, sent directly to the one agent that can answer. Short; never long-form content.",
  "- Change needed outside your assignment? Ask its owner or Main first.",
  "- Asked a question? Keep working on whatever it does not block; NEVER `wait` on a peer's or Main's progress.",
  "- Discovery: Read bare `history://` for registered agent transcripts and current peers.",
  "⁂ Completion\nReturn the synthetic task result normally.",
].join("\n");
async function child(f, systemPrompt = [conflict], sessionStart = false) {
  const result = host(f.workspace);
  if (sessionStart) await result.emit("session_start");
  result.prompt = await result.emit("before_agent_start", { prompt: `${f.marker}\nSynthetic task only.`, systemPrompt });
  return result;
}

describe("owned OMP prompt child communication boundary", () => {
  test("removes the conflicting Main-blocker allowance while preserving role, context, and host policy", async () => {
    const f = fixture();
    const session = await child(f);
    const prompt = session.prompt.systemPrompt.join("\n");
    expect(prompt).not.toContain("⁂ Coop");
    expect(prompt).not.toContain("# Peers");
    expect(prompt).not.toContain("only a question that blocks you or a blocker/decision report");
    expect(prompt).not.toContain("Ask its owner or Main first");
    expect(prompt).not.toContain("Read bare `history://`");
    expect(prompt).toContain("Host/system policy: preserve synthetic system safety rules.");
    expect(prompt).toContain("Worker agent: delegated tasks.");
    expect(prompt).toContain("Synthetic reusable-code context.");
    expect(prompt).toContain("Return the synthetic task result normally.");
    expect(prompt).toContain("Do not message Main");
    expect(prompt).toContain("even to report a blocker or ask a question");
    let delivered = false;
    const denial = await session.call("write", { path: "agent://Main", content: "SYNTHETIC_PRIVATE_ARGUMENT" }, () => { delivered = true; });
    expect(denial?.block).toBe(true);
    expect(delivered).toBe(false);
    expect(JSON.stringify(denial)).not.toContain("SYNTHETIC_PRIVATE_ARGUMENT");
    expect(JSON.stringify(denial)).not.toContain("Main");
  });

  test("strips collaboration sections without removing separately headed history safety policy", async () => {
    const f = fixture();
    const session = await child(f, ["System policy remains.", "# Coop\nSynthetic collaboration advice.\n## Peers\nSynthetic peer list.\n# Task\nSynthetic task retained.\n# History\nNever disclose private session data or credentials.\n# Context\nSynthetic context retained."]);
    const prompt = session.prompt.systemPrompt.join("\n");
    expect(prompt).not.toContain("Synthetic collaboration advice.");
    expect(prompt).not.toContain("Synthetic peer list.");
    expect(prompt).toContain("# History\nNever disclose private session data or credentials.");
    expect(prompt).toContain("System policy remains.");
    expect(prompt).toContain("Synthetic task retained.");
    expect(prompt).toContain("Synthetic context retained.");
  });

  test("initializes native/data children with or without replaying session_start, and registers no executor", async () => {
    for (const native of [false, true]) {
      for (const sessionStart of [false, true]) {
        const f = fixture(native);
        const session = await child(f, [conflict], sessionStart);
        expect((await session.call("write", { path: "agent://Main", content: "synthetic" }))?.block).toBe(true);
        expect(session.tools.size).toBe(0);
        expect(session.handlers.has("assistant_message")).toBe(false);
        expect(session.handlers.has("tool_result")).toBe(false);
      }
    }
  });

  test("rejects encoded/case/whitespace virtual routing and history/session/IRC aliases categorically", async () => {
    const session = await child(fixture());
    const targets = [
      "agent://Main", "history://", "session://synthetic", "sessions://synthetic", "irc://Main",
      "a G e N t : / / Main", "%61gent%3A%2F%2FMain", "%2561gent%253a%252f%252fMain",
      "a%E2%80%8Bgent%3a%2f%2fMain", "ＡＧＥＮＴ：／／Main", "local.txt;HiStOrY://synthetic",
      "xd://eval", "XD: / / builtin", "xd://irc", "xd://history", "peer://SyntheticPeer",
    ];
    let reason;
    for (const target of targets) {
      for (const tool of ["read", "write", "grep", "glob"]) {
        const denied = await session.call(tool, { path: target, content: "SYNTHETIC_SECRET" }, () => { throw new Error("must not deliver"); });
        expect(denied?.block).toBe(true);
        reason ??= denied.reason;
        expect(denied.reason).toBe(reason);
        expect(denied.reason).not.toContain(target);
      }
    }
    for (const name of ["irc", "irc.reply", "functions.send_message", "history", "session_history", "builtin.write", "functions.%65val"]) {
      expect((await session.call(name, { content: "SYNTHETIC_SECRET", code: "synthetic" }))?.block).toBe(true);
    }
  });

  test("blocks nested wrappers, JSON arguments, edit routing headers, and eval bridges", async () => {
    const session = await child(fixture());
    const calls = [
      ["multi_tool_use.parallel", { tool_uses: [{ recipient_name: "functions.write", parameters: { path: "agent://Main", content: "synthetic" } }] }],
      ["parallel", { tool_uses: [{ recipient_name: "multi_tool_use.parallel", parameters: { tool_uses: [{ recipient_name: "functions.read", parameters: { path: "%68istory%3a//synthetic" } }] } }] }],
      ["call_tool", { name: "write", arguments: JSON.stringify({ path: "irc://Main", content: "synthetic" }) }],
      ["invoke_tool", { tool: "functions.eval", input: { code: "synthetic ordinary-looking code" } }],
      ["eval", { language: "py", code: "await tool.write({'path': 'agent://' + 'Main', 'content': 'synthetic'})" }],
      ["edit", { input: "[agent://Main#ABCD]\nPUT >1:\n+synthetic" }],
    ];
    for (const [name, input] of calls) {
      expect((await session.call(name, input, () => { throw new Error("must not deliver"); }))?.block).toBe(true);
    }
  });

  test("denies parent-history task selectors even when they are not virtual URLs", async () => {
    const session = await child(fixture());
    for (const key of ["sourceSession", "history", "parentHistory", "inheritHistory", "session", "sessionId"]) {
      const task = { task: "Synthetic bound task", model: "fixture/model", [key]: "synthetic-parent-session" };
      expect((await session.call("task", { tasks: [task] }))?.block).toBe(true);
      expect((await session.call("task", task))?.block).toBe(true);
    }
    expect((await session.call("task", { tasks: [{ agent: "seance", task: "synthetic" }] }))?.block).toBe(true);
  });

  test("allows local writes/edits, wrapper local calls, and ordinary Python/bash without scanning semantic data", async () => {
    const f = fixture();
    const session = await child(f);
    const target = path.join(f.workspace, "synthetic-result.txt");
    const content = "Synthetic data containing agent://Main, history://, and IRC is not routing.";
    expect(await session.call("write", { path: target, content }, input => writeFileSync(input.path, input.content))).toBeUndefined();
    expect(readFileSync(target, "utf8")).toBe(content);
    const localCalls = [
      ["read", { path: target }],
      ["edit", { input: `[${target}#ABCD]\nPUT >1:\n+${content}` }],
      ["bash", { command: "python -c \"print('synthetic')\"", cwd: f.workspace }],
      ["python", { code: "print('agent:// is synthetic data, not a routing argument')" }],
      ["task", { task: "Synthetic task text mentions history://, but is not a routing field." }],
      ["multi_tool_use.parallel", { tool_uses: [{ recipient_name: "functions.write", parameters: { path: target, content } }] }],
    ];
    for (const [name, input] of localCalls) expect(await session.call(name, input)).toBeUndefined();
  });

  test("root and unbound diagnostic children remain unaffected even with localization-like names", async () => {
    const f = fixture();
    const root = host(f.workspace, "main");
    await root.emit("session_start");
    expect(await root.emit("before_agent_start", { prompt: f.marker, systemPrompt: [conflict] })).toBeUndefined();
    expect(await root.call("write", { path: "agent://SyntheticChild", content: "synthetic root routing" })).toBeUndefined();
    expect(await root.call("eval", { code: "synthetic" })).toBeUndefined();
    const diagnostic = host(f.workspace);
    diagnostic.ctx.agent.name = "Parr0tSemanticWorker";
    writeJson(path.join(f.workspace, ".game-l10n/framework-session.json"), { mode: "analysis" });
    expect(await diagnostic.emit("before_agent_start", { prompt: `Synthetic diagnosis mentions ${f.marker} inline, not as a marker.`, systemPrompt: [conflict] })).toBeUndefined();
    expect(await diagnostic.call("eval", { code: "synthetic diagnosis" })).toBeUndefined();
    expect(await diagnostic.call("write", { path: "agent://Main", content: "synthetic diagnostic report" })).toBeUndefined();
  });

  test("a mode switch or marker omission never upgrades an existing localization child", async () => {
    const f = fixture();
    const session = await child(f);
    writeJson(path.join(f.workspace, ".game-l10n/framework-session.json"), { mode: "analysis" });
    const continuation = await session.emit("before_agent_start", { prompt: "Synthetic diagnostic identity request without a relay marker.", systemPrompt: [conflict] });
    expect(continuation.systemPrompt.join("\n")).not.toContain("Ask its owner or Main first");
    expect((await session.call("write", { path: "agent://Main", content: "synthetic" }))?.block).toBe(true);
    expect((await session.call("eval", { code: "synthetic" }))?.block).toBe(true);
  });

  test("rejects changed, malformed, and non-core relay claims without disclosing target arguments", async () => {
    const f = fixture();
    const session = await child(f);
    const otherRelative = ".game-l10n/native/relay/synthetic-other.json";
    writeJson(path.join(f.workspace, otherRelative), { schema: "parr0t.native-wave-relay.v1", task_bindings: [] });
    await expect(session.emit("before_agent_start", { prompt: `PARR0T_OMP_RELAY=${otherRelative}`, systemPrompt: [] })).rejects.toThrow("child relay binding cannot change");
    for (const marker of ["PARR0T_OMP_RELAY=synthetic-private.json", `${f.marker}\n${f.marker}`, "PARR0T_OMP_RELAY=../synthetic-private.json"]) {
      const invalid = host(f.workspace);
      try {
        await invalid.emit("before_agent_start", { prompt: marker, systemPrompt: [conflict] });
        throw new Error("expected rejection");
      } catch (error) {
        expect(error.message).toBe("PARR0T_OMP_PROMPT_ISOLATION: invalid core relay binding");
        expect(error.message).not.toContain("synthetic-private");
      }
      expect((await invalid.call("write", { path: "agent://Main", content: "synthetic" }))?.block).toBe(true);
    }
  });
});
