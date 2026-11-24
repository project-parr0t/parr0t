import { createHash } from "node:crypto";
import { linkSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import registerFrameworkModeApproval, { checkFrameworkToolCall, recordFrameworkExposure, registerParr0tSession } from "./framework-mode-approval.mjs";
import createNativeRelayLifecycle, { publishHostModels } from "./native-relay-lifecycle.mjs";
import { registerNativeProcess } from "./omp-native-process.mjs";
import { captureOperatorConfigs, observeOperatorPolicy } from "./omp-host-policy.mjs";

// Enforce child host-tool communication boundaries without an OS sandbox.
// Native processes use the public exec API, never the embedded URL filesystem.
const WORKSPACE_ENV = "PARR0T_OMP_PROMPT_WORKSPACE";
const CONTROL_ENV = "PARR0T_OMP_CONTROL_DIRECTORY";
const HELPER_SHA256 = createHash("sha256").update(readFileSync(fileURLToPath(import.meta.url))).digest("hex");
const POLICY = [
  "Parr0t child communication boundary, including unmarked and diagnostic descendants.",
  "This role-specific policy supersedes generic Coop, Peers, collaboration, and blocker-message advice. Do not message Main, a parent, a peer, or any agent, even to report a blocker or ask a question. Do not use IRC, host message routing, agent://, history://, or session history tools. Do not inspect parent/peer/session history.",
  "Use local artifact tools and parr0t_process for native Python/process execution. Its argv starts with the executable, followed by exact arguments; use the available Python executable or the core-bound executable. Do not use embedded bash, eval, or a builtin bridge: their host virtual filesystems can route messages. Native process execution has no implicit deadline and is not an OS/filesystem/network sandbox. Report completion through the normal task return and core-owned result files, never an outbound message. Host/system safety policy and the assigned task remain in force.",
  "A relay marker binds work, never permission to communicate. Missing markers and framework mode changes do not enable child messaging or session-history access.",
  "The host withholds free-form child replies. Write the assigned output artifact and return only its locator or the bound categorical result.",
].join("\n");
const DENIAL = { block: true, reason: "PARR0T_OMP_PROMPT_ISOLATION: child communication or history access denied" };
const WITHHELD_REPLY = "E_NATIVE_MESSAGE_ISOLATION_REQUIRED";
const ROUTES = /(?:agent|history|sessions?|irc|peers?|coop|messages?|proc):/i;
const BRIDGES = new Set(["bash", "eval", "builtin", "builtins", "call_builtin", "invoke_builtin", "exec", "python", "tool"]);
const MESSAGING = new Set([
  "irc", "coop", "message", "send", "send_message", "sendmessage", "send_agent_message",
  "agent_message", "write_agent", "read_agent", "message_agent", "communicate", "agent_send", "send_agent",
  "irc_send", "irc_read", "write_message", "read_messages", "peer_message",
  "history", "session", "sessions", "session_history", "agent_history", "read_agent_history",
  "read_history", "get_history", "read_session", "get_session", "get_session_history", "list_sessions",
]);
const WRAPPERS = new Set(["parallel", "multi_tool_use", "tool_call", "call_tool", "invoke_tool"]);
const LOCAL_DEVICES = new Set(["xd://parr0t_process", "xd://parr0t_exec", "xd://lsp", "xd://ast_edit", "xd://resolve", "xd://reject"]);

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
export function deniedCall(name, input, depth = 0, ownedChildren) {
  if (depth > 32) return true;
  const tool = toolName(name);
  if (tool === "yield" && depth !== 0) return true;
  const components = normalized(name).split(/[./:]/).filter(Boolean);
  if (components.some(part => BRIDGES.has(part) || MESSAGING.has(part)
      || /(?:history|sessions?|messages?|irc|(?:^|_)(?:agent|peer|coop)(?:_|$))/.test(part))) return true;
  if (typeof input === "string") {
    try { input = JSON.parse(input); } catch { return true; }
  }
  if (!input || typeof input !== "object") return false;
  // Unknown tool names are not intrinsically dangerous. Reject executable
  // bridges by capability-shaped input while leaving harmless local helpers.
  if (!["parr0t_process", "parr0t_exec", "task", "edit", "ast_edit"].includes(tool)
      && ["code", "command", "script", "argv"].some(key => input[key] !== undefined)) return true;
  if (["recipient", "recipient_id", "agent_id", "session_id", "sourceSession"].some(key =>
    input[key] !== undefined && input[key] !== "")) return true;
  if (tool === "write" && input.content === "" && typeof input.path === "string") {
    const cancellation = /^proc:\/\/([^/]+)\/kill$/.exec(input.path);
    if (cancellation && ownedChildren?.has(cancellation[1])
        && input.target === undefined && input.uri === undefined && input.url === undefined
        && input.paths === undefined) return false;
  }
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
      call.parameters ?? call.input ?? call.arguments, depth + 1, ownedChildren,
    ));
    return deniedCall(input.recipient_name ?? input.toolName ?? input.name ?? input.tool,
      input.parameters ?? input.input ?? input.arguments, depth + 1, ownedChildren);
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
  return { target, local, native, metadata };
}

function deterministicInventoryWorker(bound, workspace) {
  if (bound.native || bound.metadata.worker_role !== "inventory_reconciler") return undefined;
  const taskPath = path.resolve(workspace, bound.metadata.task_path);
  if (path.dirname(taskPath) !== path.dirname(bound.target) || path.basename(taskPath) !== "task.json"
      || realpathSync(taskPath) !== taskPath) throw new Error("inventory task path changed");
  const bytes = readFileSync(taskPath);
  const digest = createHash("sha256").update(bytes).digest("hex");
  if (digest !== bound.metadata.task_sha256) throw new Error("inventory task digest changed");
  const task = JSON.parse(bytes.toString("utf8"));
  const command = task.worker_execution?.deterministic_worker;
  if (!command) return undefined;
  const script = path.join(workspace, "tools", "reconcile_inventory_worker.py");
  const cli = task.worker_execution.core_runtime?.cli_argv;
  if (!Array.isArray(cli) || cli.length !== 4 || !path.isAbsolute(cli[0]) || !path.isAbsolute(cli[3])
      || task.worker_execution.tool !== "parr0t_process"
      || typeof command.script_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(command.script_sha256)) {
    throw new Error("invalid deterministic inventory command binding");
  }
  const template = [...cli.slice(0, -1), script, "--workspace", workspace, "--core-entry", cli.at(-1),
    "--relay", "<relay>", "--task-sha256", "<task-sha256>"];
  if (!Array.isArray(command.argv) || JSON.stringify(command.argv) !== JSON.stringify(template)) {
    throw new Error("deterministic inventory argv changed");
  }
  return {
    script, sha256: command.script_sha256, used: false,
    argv: template.map(arg => arg === "<relay>" ? bound.local : arg === "<task-sha256>" ? digest : arg),
  };
}

function requireInventoryCommand(binding, input, workspace) {
  if (!Array.isArray(input?.argv) || JSON.stringify(input.argv) !== JSON.stringify(binding.argv)
      || (input.cwd !== undefined && realpathSync(input.cwd) !== workspace)) {
    throw new Error("PARR0T_OMP_PROMPT_ISOLATION: exact deterministic inventory argv required");
  }
  if (binding.used) throw new Error("PARR0T_OMP_PROMPT_ISOLATION: inventory command already invoked");
}

function childReply({ local, native, metadata }, depth) {
  if (local.split("/").some(part => !/^[A-Za-z0-9._-]+$/.test(part))) return WITHHELD_REPLY;
  if (depth === 1) {
    if (native) return local.replace(/\.json$/, "-receipt.json");
    // The receipt stays in the stage directory even when launch evidence
    // moves the active relay into a purpose-specific launch revision.
    const stage = local.split("/")[2];
    return `.game-l10n/data-plane/${stage}/receipt.json`;
  }
  if (native) return "worker_ended";
  const envelope = metadata.expected_envelope;
  if (typeof envelope !== "string" || path.posix.dirname(envelope) !== path.posix.dirname(local)
      || !/^control-envelope\.g\d+\.attempt-\d+(?:\.revision-\d+)?\.json$/.test(path.posix.basename(envelope))) {
    return WITHHELD_REPLY;
  }
  return envelope;
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
  let reply = WITHHELD_REPLY;
  let inventoryCommand;
  const ownedChildren = new Set();
  const lifecycle = createNativeRelayLifecycle({
    workspace,
    relayPath: () => relay,
    executeCore: (argv, ctx) => pi.exec(argv[0], argv.slice(1), { cwd: workspace(ctx) }),
  });
  registerNativeProcess(pi, {
    async beforeExecute(params, ctx) {
      if (inventoryCommand && ctx.agent?.kind === "sub" && ctx.agent.depth >= 2) {
        requireInventoryCommand(inventoryCommand, params, realpathSync(workspace(ctx)));
        inventoryCommand.used = true;
        if (realpathSync(inventoryCommand.script) !== inventoryCommand.script
            || createHash("sha256").update(readFileSync(inventoryCommand.script)).digest("hex") !== inventoryCommand.sha256) {
          throw new Error("PARR0T_OMP_PROMPT_ISOLATION: deterministic inventory script changed");
        }
      }
      if (relay && ctx.agent?.kind === "sub" && ctx.agent.depth === 1) {
        const decision = await lifecycle.tool_call({ toolName: "parr0t_process", input: params }, ctx);
        if (decision?.block) throw new Error(decision.reason);
      }
    },
    defaultCwd: ctx => relay ? workspace(ctx) : ctx.cwd,
  });
  function workspace(ctx) {
    const configured = pi.getFlag("parr0t-workspace");
    return typeof configured === "string" && path.isAbsolute(configured)
      ? configured : process.env[WORKSPACE_ENV] ?? ctx.cwd;
  }
  function registerReadiness(ctx) {
    const launchDirectory = process.env[CONTROL_ENV];
    if (launchDirectory === undefined) return;
    const fail = reason => { throw new Error(`PARR0T_OMP_PROMPT_ISOLATION: ${reason}`); };
    const configured = workspace(ctx);
    if (typeof configured !== "string" || !path.isAbsolute(configured)
        || typeof launchDirectory !== "string" || !path.isAbsolute(launchDirectory)) fail("invalid control launch workspace");
    const root = realpathSync(configured);
    const directory = path.resolve(launchDirectory);
    const launchId = path.basename(directory);
    if (!/^[a-f0-9]{32}$/.test(launchId)
        || path.dirname(directory) !== path.join(root, ".game-l10n", "omp-control")
        || realpathSync(directory) !== directory
        || realpathSync(ctx.cwd) !== root) fail("invalid control launch directory or workspace binding");
    const sessionId = ctx.sessionManager?.getSessionId?.();
    if (typeof sessionId !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(sessionId)) fail("host root session ID is unavailable");
    if (typeof pi.getAllTools !== "function" || typeof pi.getActiveTools !== "function") fail("host tool availability API required");
    const registered = pi.getAllTools();
    const active = pi.getActiveTools();
    if (!Array.isArray(registered) || !registered.some(tool => tool.name === "parr0t_process")) fail("parr0t_process is not registered in this host session");
    if (!Array.isArray(active) || !active.includes("parr0t_process")) fail("parr0t_process is not active in this host session");
    if (!registered.some(tool => tool.name === "parr0t_framework_mode")) fail("parr0t_framework_mode is not registered in this host session");
    if (!active.includes("parr0t_framework_mode")) fail("parr0t_framework_mode is not active in this host session");
    const receiptPath = path.join(directory, "ready.json");
    const receipt = `${JSON.stringify({
      schema: "parr0t.omp-control-ready.v1", launch_id: launchId, session_id: sessionId,
      process_id: process.pid, helper_sha256: HELPER_SHA256, process_tool: "parr0t_process",
      framework_tool: "parr0t_framework_mode",
    })}\n`;
    let previous;
    try { previous = readFileSync(receiptPath, "utf8"); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
    if (previous === receipt) return;
    if (previous !== undefined) fail("control launch is already registered to another host session");
    const temporary = `${receiptPath}.${process.pid}.tmp`;
    writeFileSync(temporary, receipt, { flag: "wx", mode: 0o600 });
    // Publish atomically without replacing a competing main session's proof.
    try { linkSync(temporary, receiptPath); }
    finally { rmSync(temporary, { force: true }); }
  }
  registerFrameworkModeApproval(pi, workspace);
  registerParr0tSession(pi, workspace);
  pi.on("session_start", (_event, ctx) => {
    if (ctx.agent?.kind === "main") {
      captureOperatorConfigs(ctx);
      registerReadiness(ctx);
      publishHostModels(ctx, workspace(ctx));
      process.env[WORKSPACE_ENV] = workspace(ctx);
    }
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
      if (relay && relay !== bound.target) throw new Error("PARR0T_OMP_PROMPT_ISOLATION: child relay binding cannot change");
      relay = bound.target;
      reply = childReply(bound, ctx.agent.depth);
    }
    const handoff = await lifecycle.before_agent_start(event, ctx);
    const blocks = (Array.isArray(event.systemPrompt) ? event.systemPrompt : [event.systemPrompt ?? ""]).map(stripCollaboration);
    if (!blocks.includes(POLICY)) blocks.push(POLICY);
    if (handoff) blocks.push(handoff);
    if (bound && ctx.agent.depth >= 2) {
      inventoryCommand = deterministicInventoryWorker(bound, realpathSync(workspace(ctx)));
      if (inventoryCommand) blocks.push([
        "Deterministic inventory worker. Do not inspect task, caches, source, or other artifacts.",
        `Your only process call is parr0t_process with ${JSON.stringify({argv: inventoryCommand.argv})}.`,
        "Do not replace it with an inline script, a verifier call, or a manually written envelope. Stop after it returns.",
      ].join("\n"));
    }
    return { systemPrompt: Array.isArray(event.systemPrompt) ? blocks : blocks.join("\n\n") };
  });
  pi.on("assistant_message", (event, ctx) => {
    if (ctx.agent?.kind !== "sub") return;
    const terminalReply = event.message.content.some(block => block.type === "toolCall") ? "" : reply;
    let emitted = false;
    return { content: event.message.content.map(block => {
      if (block.type !== "text") return block;
      const text = emitted ? "" : terminalReply;
      emitted = true;
      return { ...block, text };
    }) };
  });
  pi.on("tool_call", async (event, ctx) => {
    try { checkFrameworkToolCall(event, ctx, realpathSync(workspace(ctx))); }
    catch (error) { return { block: true, reason: error.message }; }
    if (ctx.agent?.kind !== "sub") return;
    try {
      if (deniedCall(event.toolName, event.input, 0, ownedChildren)) return { ...DENIAL };
      if (toolName(event.toolName) === "yield") return { input: { data: reply } };
      if (inventoryCommand && ctx.agent.depth >= 2) {
        if (toolName(event.toolName) !== "parr0t_process") {
          return { block: true, reason: "Only the bound deterministic inventory command is admitted." };
        }
        requireInventoryCommand(inventoryCommand, event.input, realpathSync(workspace(ctx)));
      }
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
  pi.on("tool_result", async (event, ctx) => {
    observeOperatorPolicy(event, ctx);
    if (toolName(event.toolName) === "task") {
      // Public task-result IDs attest actual caller-owned processes. A
      // before_subagent_spawn selection key is not a process ownership proof.
      const own = id => {
        if (typeof id === "string" && id && !/[/%?#\\\s\u0000-\u001f\u007f]/u.test(id)
            && id !== "." && id !== "..") ownedChildren.add(id);
      };
      if (event.details?.async?.type === "task" && event.details.async.state === "running") own(event.details.async.jobId);
      for (const child of event.details?.progress ?? []) {
        if (child.status === "running" || child.status === "pending") own(child.id);
      }
    }
    await lifecycle.tool_result(event, ctx);
    await recordFrameworkExposure(event, ctx, realpathSync(workspace(ctx)));
    if (!event.isError && toolName(event.toolName) === "write" && event.input?.content === "") {
      const cancellation = /^proc:\/\/([^/]+)\/kill$/.exec(event.input.path ?? "");
      if (cancellation) ownedChildren.delete(cancellation[1]);
    }
  });
  pi.on("agent_end", lifecycle.agent_end);
}
