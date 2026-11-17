import { readFileSync, realpathSync } from "node:fs";
import path from "node:path";

// This is a host prompt/communication boundary, not an executor or OS sandbox.
// Read only the core relay binding; never inspect task files or semantic text.
const WORKSPACE_ENV = "PARR0T_OMP_PROMPT_WORKSPACE";
const POLICY = [
  "Parr0t core-bound localization child: communication boundary.",
  "This role-specific policy supersedes generic Coop, Peers, collaboration, and blocker-message advice. Do not message Main, a parent, a peer, or any agent, even to report a blocker or ask a question. Do not use IRC, host message routing, agent://, history://, or session history tools. Do not inspect parent/peer/session history.",
  "Use only the bound task's local files and normal host Python/process tools for the assigned work. Report completion through the normal task return and core-owned result files, never an outbound message. Do not use eval or a builtin messaging bridge to reach host routing. Host/system safety policy and the assigned role, task, and context remain in force.",
  "This helper enforces selected host tool-call and prompt boundaries only; ordinary Python/bash are not sandboxed. A framework mode switch never grants this existing localization child diagnostic permissions.",
].join("\n");
const DENIAL = { block: true, reason: "PARR0T_OMP_PROMPT_ISOLATION: child communication or history access denied" };
const ROUTES = /(?:agent|history|sessions?|irc|peers?|coop|messages?):/i;
const BRIDGES = new Set(["eval", "builtin", "builtins", "call_builtin", "invoke_builtin"]);
const MESSAGING = new Set([
  "irc", "coop", "message", "send", "send_message", "sendmessage", "send_agent_message",
  "agent_message", "write_agent", "read_agent", "message_agent", "communicate", "agent_send", "send_agent",
  "irc_send", "irc_read", "write_message", "read_messages", "peer_message",
  "history", "session", "sessions", "session_history", "agent_history", "read_agent_history",
  "read_history", "get_history", "read_session", "get_session", "get_session_history", "list_sessions",
]);
const WRAPPERS = new Set(["parallel", "multi_tool_use", "tool_call", "call_tool", "invoke_tool"]);

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
  return ROUTES.test(target) || /^xd:\/\/(?:eval|builtin|builtins|irc|history|session|agent)(?:[/:?#]|$)/.test(target);
}
function deniedCall(name, input, depth = 0) {
  if (depth > 32) return true;
  const tool = toolName(name);
  const components = normalized(name).split(/[./:]/).filter(Boolean);
  if (components.some(part => BRIDGES.has(part) || MESSAGING.has(part))) return true;
  if (typeof input === "string") {
    try { input = JSON.parse(input); } catch { return false; }
  }
  if (!input || typeof input !== "object") return false;
  // Only routing fields, not file contents, commands, prompts, or code.
  for (const key of ["path", "target", "uri", "url", "recipient", "recipient_id", "agent_id", "session_id", "sourceSession"]) {
    if (deniedTarget(input[key])) return true;
  }
  if (tool === "task") {
    for (const item of Array.isArray(input.tasks) ? input.tasks : [input]) {
      if (!item || typeof item !== "object") continue;
      if (item.agent === "seance" || ["sourceSession", "history", "parentHistory", "inheritHistory", "session", "sessionId"].some(key => item[key] !== undefined)) return true;
    }
  }
  if (tool === "edit" && typeof input.input === "string") {
    // Edit headers contain target paths; patch bodies are semantic data.
    for (const match of input.input.matchAll(/^\[([^\]\r\n]+)\]\s*$/gm)) {
      if (deniedTarget(match[1])) return true;
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

export default function promptIsolation(pi) {
  pi.registerFlag("parr0t-workspace", { description: "Absolute Parr0t workspace for prompt child communication boundaries", type: "string" });
  let relay;
  let restricted = false;
  function workspace(ctx) {
    const configured = pi.getFlag("parr0t-workspace");
    return typeof configured === "string" && path.isAbsolute(configured)
      ? configured : process.env[WORKSPACE_ENV] ?? ctx.cwd;
  }
  pi.on("session_start", (_event, ctx) => {
    if (ctx.agent?.kind === "main") process.env[WORKSPACE_ENV] = workspace(ctx);
  });
  pi.on("before_agent_start", (event, ctx) => {
    if (ctx.agent?.kind !== "sub") return;
    let bound;
    try { bound = boundRelay(event.prompt, workspace(ctx)); }
    catch {
      // A malformed claimed binding must not obtain an unrestricted identity.
      restricted = true;
      throw new Error("PARR0T_OMP_PROMPT_ISOLATION: invalid core relay binding");
    }
    if (bound) {
      restricted = true;
      if (relay && relay !== bound) throw new Error("PARR0T_OMP_PROMPT_ISOLATION: child relay binding cannot change");
      relay = bound;
    }
    if (!restricted) return;
    const blocks = (Array.isArray(event.systemPrompt) ? event.systemPrompt : [event.systemPrompt ?? ""]).map(stripCollaboration);
    if (!blocks.includes(POLICY)) blocks.push(POLICY);
    return { systemPrompt: Array.isArray(event.systemPrompt) ? blocks : blocks.join("\n\n") };
  });
  pi.on("tool_call", (event, ctx) => {
    if (ctx.agent?.kind !== "sub" || !restricted) return;
    try { if (deniedCall(event.toolName, event.input)) return { ...DENIAL }; }
    catch { return { ...DENIAL }; }
  });
}
