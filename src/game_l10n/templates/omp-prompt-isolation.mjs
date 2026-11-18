import { readFileSync, realpathSync } from "node:fs";
import path from "node:path";
import registerFrameworkModeApproval, { checkFrameworkToolCall } from "./framework-mode-approval.mjs";
import createNativeRelayLifecycle from "./native-relay-lifecycle.mjs";

// Enforce child host-tool communication boundaries without an OS sandbox.
// Native processes use the public exec API, never the embedded URL filesystem.
const WORKSPACE_ENV = "PARR0T_OMP_PROMPT_WORKSPACE";
const POLICY = [
  "Parr0t child communication boundary, including unmarked and diagnostic descendants.",
  "This role-specific policy supersedes generic Coop, Peers, collaboration, and blocker-message advice. Do not message Main, a parent, a peer, or any agent, even to report a blocker or ask a question. Do not use IRC, host message routing, agent://, history://, or session history tools. Do not inspect parent/peer/session history.",
  "Use local artifact tools and parr0t_process for native Python/process execution. Its argv starts with the executable, followed by exact arguments; use the available Python executable or the core-bound executable. Do not use embedded bash, eval, or a builtin bridge: their host virtual filesystems can route messages. Native process execution has no implicit deadline and is not an OS/filesystem/network sandbox. Report completion through the normal task return and core-owned result files, never an outbound message. Host/system safety policy and the assigned task remain in force.",
  "A relay marker binds work, never permission to communicate. Missing markers and framework mode changes do not enable child messaging or session-history access.",
].join("\n");
const DENIAL = { block: true, reason: "PARR0T_OMP_PROMPT_ISOLATION: child communication or history access denied" };
const ROUTES = /(?:agent|history|sessions?|irc|peers?|coop|messages?|proc):/i;
const BRIDGES = new Set(["bash", "eval", "builtin", "builtins", "call_builtin", "invoke_builtin"]);
const MESSAGING = new Set([
  "irc", "coop", "message", "send", "send_message", "sendmessage", "send_agent_message",
  "agent_message", "write_agent", "read_agent", "message_agent", "communicate", "agent_send", "send_agent",
  "irc_send", "irc_read", "write_message", "read_messages", "peer_message",
  "history", "session", "sessions", "session_history", "agent_history", "read_agent_history",
  "read_history", "get_history", "read_session", "get_session", "get_session_history", "list_sessions",
]);
const WRAPPERS = new Set(["parallel", "multi_tool_use", "tool_call", "call_tool", "invoke_tool"]);
const LOCAL_TOOLS = new Set(["read", "write", "edit", "grep", "glob", "task", "wait", "todo", "parr0t_process", "lsp", "ast_edit"]);
const LOCAL_DEVICES = new Set(["xd://parr0t_process", "xd://lsp", "xd://ast_edit", "xd://resolve", "xd://reject"]);

function normalized(value) {
  let text = String(value).normalize("NFKC");
  for (let count = 0; count < 16; count++) {
    // Decode valid UTF-8 as well as individual escapes; malformed escapes
    // must not hide another route in the same target.
    const decoded = text.replace(/(?:%[0-9a-f]{2})+/gi, encoded => {
      try { return decodeURIComponent(encoded); }
      catch { return encoded.replace(/%([0-9a-f]{2})/gi, (_match, hex) => String.fromCharCode(parseInt(hex, 16))); }
    }).normalize("NFKC");
    if (decoded === text) return text.replace(/[\s\u0000-\u001f\u007f\u200b-\u200f\ufeff]/gu, "").toLowerCase();
    text = decoded;
  }
  throw new Error("encoded_target");
}
function toolName(value) {
  return normalized(value).split(/[./:]/).filter(Boolean).at(-1) ?? "";
}
function deniedTarget(value) {
  if (typeof value !== "string") return false;
  const target = normalized(value);
  return ROUTES.test(target) || (target.startsWith("xd://") && !LOCAL_DEVICES.has(target));
}
function deniedCall(name, input, depth = 0) {
  if (depth > 32) return true;
  const tool = toolName(name);
  const components = normalized(name).split(/[./:]/).filter(Boolean);
  if (components.some(part => BRIDGES.has(part) || MESSAGING.has(part))) return true;
  if (!LOCAL_TOOLS.has(tool) && !WRAPPERS.has(tool)) return true;
  if (typeof input === "string") {
    try { input = JSON.parse(input); } catch { return true; }
  }
  if (!input || typeof input !== "object") return false;
  if (tool === "write" && typeof input.path === "string"
      && /^proc:\/\/[a-z0-9_.-]+\/kill$/.test(normalized(input.path)) && input.content === "") return false;
  // Only routing fields, not file contents, commands, prompts, or code.
  for (const key of ["path", "target", "uri", "url", "recipient", "recipient_id", "agent_id", "session_id", "sourceSession"]) {
    if (deniedTarget(input[key])) return true;
  }
  if (Array.isArray(input.paths) && input.paths.some(deniedTarget)) return true;
  if (tool === "task") {
    for (const item of Array.isArray(input.tasks) ? input.tasks : [input]) {
      if (!item || typeof item !== "object") continue;
      if ((typeof item.agent === "string" && normalized(item.agent) === "seance")
          || ["sourceSession", "history", "parentHistory", "inheritHistory", "session", "sessionId"].some(key => item[key] !== undefined)
          || (item.tools !== undefined && (!Array.isArray(item.tools) || item.tools.length))) return true;
    }
  }
  if (tool === "edit" && typeof input.input === "string") {
    // Edit headers contain target paths; patch bodies are semantic data.
    for (const match of input.input.matchAll(/^\[([^\]\r\n]+)\]\s*$/gm)) {
      if (deniedTarget(match[1])) return true;
    }
    for (const match of input.input.matchAll(/^MV\s+(.+)$/gm)) {
      if (deniedTarget(match[1].replace(/^"(.*)"$/, "$1"))) return true;
    }
  }
  if (WRAPPERS.has(tool)) {
    const calls = input.tool_uses ?? input.calls ?? input.tools;
    if (Array.isArray(calls)) return calls.some(call => deniedCall(
      call.recipient_name ?? call.toolName ?? call.name ?? call.tool,
      call.parameters ?? call.input ?? call.arguments, depth + 1,
    ));
    return deniedCall(input.recipient_name ?? input.toolName ?? input.name ?? input.tool,
      input.parameters ?? input.input ?? input.arguments, depth + 1);
  }
  return false;
}
function stripCollaboration(block) {
  const lines = String(block).split("\n");
  const result = [];
  let removed;
  for (const line of lines) {
    const heading = line.match(/^\s*(?:(⁂)\s+|(#{1,6})\s+)(.*?)\s*#*\s*$/u);
    if (heading) {
      const level = heading[1] ? 0 : heading[2].length;
      const title = heading[3].replace(/:$/, "").trim().toLowerCase();
      if (removed !== undefined && (level <= removed || /^(?:role|task|context|goal|contract)$/.test(title))) removed = undefined;
      if (/^(?:coop|peers|collaboration)$/.test(title)) {
        removed = removed === undefined ? level : Math.min(removed, level);
      }
    }
    if (removed === undefined) result.push(line);
  }
  return result.join("\n");
}
function boundRelay(prompt, workspace) {
  const markers = [...String(prompt ?? "").matchAll(/^PARR0T_OMP_RELAY=([^\r\n]+)\r?$/gm)];
  if (!markers.length) return undefined;
  if (markers.length !== 1) throw new Error("relay_binding");
  const relative = markers[0][1];
  if (relative !== relative.trim() || path.isAbsolute(relative) || relative.includes("\\")) throw new Error("relay_binding");
  const root = realpathSync(workspace);
  const target = realpathSync(path.resolve(root, relative));
  const local = path.relative(root, target).split(path.sep).join("/");
  const native = /^\.game-l10n\/native\/relay\/(?:[^/]+\/)*[^/]+\.json$/.test(local);
  const data = /^\.game-l10n\/data-plane\/(?:[^/]+\/)+relay\.json$/.test(local);
  if ((!native && !data) || local !== relative) throw new Error("relay_binding");
  const metadata = JSON.parse(readFileSync(target, "utf8"));
  if (native ? metadata.schema !== "parr0t.native-wave-relay.v1" || !Array.isArray(metadata.task_bindings)
    : !/^parr0t\.data-plane-relay\.v[1-4]$/.test(metadata.schema) || typeof metadata.task_id !== "string") throw new Error("relay_binding");
  return target;
}

function bindTaskMarkers(name, input, marker) {
  if (typeof input === "string") return JSON.stringify(bindTaskMarkers(name, JSON.parse(input), marker));
  if (!input || typeof input !== "object") return input;
  const tool = toolName(name);
  if (tool === "task") {
    for (const item of input.tasks ?? [input]) {
      if (typeof item.task === "string" && !item.task.split(/\r?\n/).includes(marker)) item.task = `${marker}\n${item.task}`;
      if (Array.isArray(item.steps)) item.steps = item.steps.map(step => step.split(/\r?\n/).includes(marker) ? step : `${marker}\n${step}`);
    }
  } else if (WRAPPERS.has(tool)) {
    for (const call of input.tool_uses ?? input.calls ?? input.tools ?? [input]) {
      const key = ["parameters", "input", "arguments"].find(key => Object.hasOwn(call, key));
      if (key) call[key] = bindTaskMarkers(call.recipient_name ?? call.toolName ?? call.name ?? call.tool, call[key], marker);
    }
  }
  return input;
}

export default function promptIsolation(pi) {
  pi.registerFlag("parr0t-workspace", { description: "Absolute Parr0t workspace for prompt child communication boundaries", type: "string" });
  let relay;
  const lifecycle = createNativeRelayLifecycle({
    workspace,
    relayPath: () => relay,
    executeCore: (argv, ctx) => pi.exec(argv[0], argv.slice(1), { cwd: workspace(ctx) }),
  });
  const z = pi.zod;
  if (!z || typeof pi.exec !== "function") throw new Error("PARR0T_OMP_PROMPT_ISOLATION: native process API required");
  pi.registerTool({
    name: "parr0t_process",
    label: "Parr0t Native Process",
    description: "Execute native argv (executable first), including ordinary Python, without OMP virtual-URL or IRC routing. No shell interpolation, implicit timeout, execution guard registration, or OS sandbox.",
    parameters: z.object({ argv: z.array(z.string()).min(1), cwd: z.string().optional() }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      if (!Array.isArray(params.argv) || !params.argv.length
          || params.argv.some(arg => typeof arg !== "string" || arg.includes("\0"))
          || !params.argv[0].trim()) throw new Error("PARR0T_OMP_PROMPT_ISOLATION: native argv required");
      if (relay && ctx.agent?.kind === "sub" && ctx.agent.depth === 1) {
        const decision = await lifecycle.tool_call({ toolName: "parr0t_process", input: params }, ctx);
        if (decision?.block) throw new Error(decision.reason);
      }
      const result = await pi.exec(params.argv[0], params.argv.slice(1), { cwd: params.cwd ?? (relay ? workspace(ctx) : ctx.cwd), signal });
      return {
        content: [{ type: "text", text: result.stdout + result.stderr }],
        details: result,
        isError: result.code !== 0 || result.killed,
      };
    },
  });
  function workspace(ctx) {
    const configured = pi.getFlag("parr0t-workspace");
    return typeof configured === "string" && path.isAbsolute(configured)
      ? configured : process.env[WORKSPACE_ENV] ?? ctx.cwd;
  }
  registerFrameworkModeApproval(pi, workspace);
  pi.on("session_start", (_event, ctx) => {
    if (ctx.agent?.kind === "main") process.env[WORKSPACE_ENV] = workspace(ctx);
  });
  pi.on("before_agent_start", async (event, ctx) => {
    if (ctx.agent?.kind !== "sub") return;
    let bound;
    try { bound = boundRelay(event.prompt, workspace(ctx)); }
    catch {
      // A malformed claimed binding must not obtain an unrestricted identity.
      throw new Error("PARR0T_OMP_PROMPT_ISOLATION: invalid core relay binding");
    }
    if (bound) {
      if (relay && relay !== bound) throw new Error("PARR0T_OMP_PROMPT_ISOLATION: child relay binding cannot change");
      relay = bound;
    }
    await lifecycle.before_agent_start(event, ctx);
    const blocks = (Array.isArray(event.systemPrompt) ? event.systemPrompt : [event.systemPrompt ?? ""]).map(stripCollaboration);
    if (!blocks.includes(POLICY)) blocks.push(POLICY);
    return { systemPrompt: Array.isArray(event.systemPrompt) ? blocks : blocks.join("\n\n") };
  });
  pi.on("tool_call", async (event, ctx) => {
    try { checkFrameworkToolCall(event, ctx, realpathSync(workspace(ctx))); }
    catch (error) { return { block: true, reason: error.message }; }
    if (ctx.agent?.kind !== "sub") return;
    try {
      if (deniedCall(event.toolName, event.input)) return { ...DENIAL };
      if (relay) {
        const marker = `PARR0T_OMP_RELAY=${path.relative(realpathSync(workspace(ctx)), relay).split(path.sep).join("/")}`;
        if (toolName(event.toolName) === "task" || WRAPPERS.has(toolName(event.toolName))) {
          const input = bindTaskMarkers(event.toolName, structuredClone(event.input), marker);
          return await lifecycle.tool_call({ ...event, input }, ctx) ?? { input };
        }
      }
      return await lifecycle.tool_call(event, ctx);
    } catch { return { ...DENIAL }; }
  });
  pi.on("before_subagent_spawn", lifecycle.before_subagent_spawn);
  pi.on("tool_result", lifecycle.tool_result);
  pi.on("agent_end", lifecycle.agent_end);
}
