import { spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { accessSync, constants, lstatSync, mkdirSync, readFileSync, realpathSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ENTRY = fileURLToPath(new URL("./native-python.py", import.meta.url));
const LIMIT = 1024 * 1024;
const TOOL = "parr0t_framework_mode";
const SESSION_TOOL = "parr0t_session";
const PREFIX = "PARR0T_FRAMEWORK_APPROVAL: ";
const WRAPPERS = new Set(["parallel", "multi_tool_use", "tool_call", "call_tool", "invoke_tool"]);
const MUTATORS = new Set(["write", "edit", "ast_edit", "rename", "move", "mv", "remove", "delete", "rm", "unlink", "rmdir"]);
function fail(message) { throw new Error(PREFIX + message); }
function decode(value) {
  let text = String(value).normalize("NFKC");
  for (let i = 0; i < 16; i++) {
    const next = decodeURIComponent(text);
    if (next === text) return text.replace(/[\u0000-\u001f\u007f\u200b-\u200f\ufeff]/gu, "");
    text = next;
  }
  fail("ambiguous target denied");
}
function name(value) { return decode(value).toLowerCase().split(/[./:]/).filter(Boolean).at(-1); }
function resolvedTarget(value, cwd) {
  let target = path.resolve(cwd, value);
  let ancestor = target;
  for (;;) {
    try { return path.resolve(realpathSync(ancestor), path.relative(ancestor, target)); }
    catch (error) {
      if (!["ENOENT", "ENOTDIR"].includes(error.code)) throw error;
      const parent = path.dirname(ancestor);
      if (parent === ancestor) throw error;
      ancestor = parent;
    }
  }
}
function protectedTarget(value, workspace, cwd, destructive) {
  if (typeof value !== "string") return false;
  let target = decode(value).replace(/\\/g, "/").replace(/^\/\/[?.]\//, "");
  // Host read selectors and hashline anchors are not part of the filename.
  target = target.replace(/#[a-f0-9]{4}$/i, "").replace(/:(?:raw(?::.*)?|[-\d,+:]+(?::raw)?)$/i, "");
  if (/^file:\/\//i.test(target)) target = fileURLToPath(target);
  else if (/^[a-z][a-z0-9+.-]*:\/\//i.test(target)) {
    target = target.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "");
  }
  const wildcard = target.search(/[*?\[]/);
  if (wildcard >= 0) {
    const prefix = resolvedTarget(target.slice(0, wildcard).replace(/[^/]*$/, "") || ".", cwd);
    const relative = path.relative(prefix, path.join(workspace, ".game-l10n")).replace(/\\/g, "/");
    if (relative === "" || (!relative.startsWith("../") && relative !== ".." && !path.isAbsolute(relative))) return true;
  }
  const candidates = [path.resolve(cwd, target), resolvedTarget(target, cwd)];
  for (const candidate of candidates) {
    const relative = path.relative(workspace, candidate).replace(/\\/g, "/").toLowerCase();
    if (relative === ".game-l10n/framework-session.json" || relative.startsWith(".game-l10n/framework-session.json/")
        || relative === ".game-l10n/framework-approvals" || relative.startsWith(".game-l10n/framework-approvals/")) return true;
    if (destructive && ["", ".game-l10n"].includes(relative)) return true;
  }
  return false;
}
function authority(ctx) {
  if (ctx?.agent?.kind !== "main" || ctx.agent.depth !== 0
      || typeof ctx.agent.id !== "string" || !ctx.agent.id.trim()
      || typeof ctx.sessionManager?.getSessionId !== "function") fail("actual root host identity required");
  const session = ctx.sessionManager.getSessionId();
  if (typeof session !== "string" || !session.trim()) fail("actual root session required");
  return { agent: ctx.agent.id, session };
}
function workspaceFor(input, bound, allowOtherWorkspace = false) {
  const supplied = input.workspace;
  if (supplied !== undefined && (typeof supplied !== "string" || !path.isAbsolute(supplied) || supplied.includes("\0"))) fail("absolute workspace required");
  if (bound !== undefined && (typeof bound !== "string" || !path.isAbsolute(bound))) fail("absolute bound workspace required");
  if (bound === undefined && supplied === undefined) fail("explicit absolute workspace required");
  const workspace = realpathSync(allowOtherWorkspace ? supplied ?? bound : bound ?? supplied);
  if (!statSync(workspace).isDirectory()) fail("workspace directory required");
  if (!allowOtherWorkspace && supplied !== undefined && realpathSync(supplied) !== workspace) fail("workspace does not match bound helper");
  return workspace;
}
function exactSubagent(value) {
  return typeof value === "string" && value.length <= 200 &&
    /^[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)*$/.test(value) &&
    value.trim() === value;
}
function sessionFile(ctx) {
  const file = ctx.sessionManager.getSessionFile?.();
  if (typeof file !== "string" || !path.isAbsolute(file) || file.includes("\0")
      || path.extname(file) !== ".jsonl") fail("current persisted OMP session required for subagent transcripts");
  return path.resolve(file);
}
function parameters(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)
      || Object.keys(input).some(key => !["mode", "logs", "omp_subagents", "user_requested", "workspace"].includes(key))
      || !["localization", "pause", "analysis", "shutdown"].includes(input.mode)
      || input.user_requested !== true
      || (input.logs !== undefined && (!Array.isArray(input.logs) || input.logs.some(log => typeof log !== "string" || !log.trim() || log.includes("\0"))))
      || (input.omp_subagents !== undefined && (!Array.isArray(input.omp_subagents)
        || !input.omp_subagents.every(exactSubagent) || new Set(input.omp_subagents).size !== input.omp_subagents.length))
      || (input.mode !== "analysis" && (input.logs?.length || input.omp_subagents?.length))) fail("invalid lifecycle request: explicit user_requested assertion and exact analysis scope required");
}
function sessionParameters(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)
      || Object.keys(input).some(key => !["workspace", "model", "mode", "user_requested"].includes(key))
      || input.user_requested !== true
      || (input.mode !== undefined && !["auto", "workspace", "rotation"].includes(input.mode))
      || (input.model !== undefined && !exactModel(input.model))) fail("invalid session request: exact model and explicit user_requested assertion required");
}
function exactModel(value) {
  return typeof value === "string" && /^[^\s\0/]+\/[^\s\0]+$/.test(value) && !/[*?]/.test(value);
}
function checkRequest(tool, input, ctx, workspace) {
  authority(ctx);
  if (tool === TOOL) parameters(input);
  else sessionParameters(input);
  workspaceFor(input, workspace, tool === SESSION_TOOL);
}
// Called before either extension's role-specific tool policy, including root.
export function checkFrameworkToolCall(event, ctx, workspace, depth = 0) {
  if (depth > 32) fail("nested tool limit exceeded");
  const tool = name(event.toolName);
  let input = event.input;
  if (typeof input === "string") {
    if (tool === "edit") input = { input };
    else { try { input = JSON.parse(input); } catch { if (WRAPPERS.has(tool) || MUTATORS.has(tool) || tool === TOOL || tool === SESSION_TOOL) fail("uninspectable mutation denied"); return; } }
  }
  if (tool === TOOL || tool === SESSION_TOOL) { checkRequest(tool, input, ctx, workspace); return; }
  if (WRAPPERS.has(tool)) {
    const calls = input?.tool_uses ?? input?.calls ?? input?.tools;
    for (const call of Array.isArray(calls) ? calls : [input]) {
      checkFrameworkToolCall({ toolName: call?.recipient_name ?? call?.toolName ?? call?.name ?? call?.tool,
        input: call?.parameters ?? call?.input ?? call?.arguments }, ctx, workspace, depth + 1);
    }
    return;
  }
  if (!MUTATORS.has(tool) || !input || typeof input !== "object") return;
  const targets = [];
  for (const key of ["path", "target", "file", "filename", "file_path", "filePath", "source", "destination", "from", "to", "src", "dst", "oldPath", "newPath", "old_path", "new_path"]) {
    if (typeof input[key] === "string") targets.push(input[key]);
  }
  if (Array.isArray(input.paths)) targets.push(...input.paths);
  if (tool === "edit" && typeof input.input === "string") {
    for (const line of input.input.split(/\r?\n/)) {
      const header = /^\[(.+)#[a-f0-9]{4}\]$/i.exec(line);
      const move = /^MV\s+(.+)$/.exec(line);
      const diff = /^(?:\*\*\* (?:Update|Add|Delete) File: |\*\*\* Move to: )(.+)$/.exec(line);
      if (header || diff) targets.push((header ?? diff)[1]);
      if (move) targets.push(move[1].replace(/^"(.*)"$/, "$1"));
    }
  }
  for (const target of targets) {
    for (const part of String(target).split(";")) {
      if (protectedTarget(part, workspace, ctx.cwd, !["write", "edit", "ast_edit"].includes(tool))) fail("lifecycle metadata requires the approved root tool");
    }
  }
  if (tool === "write" && typeof input.path === "string" && /^xd:\/\//i.test(decode(input.path))) {
    const device = name(input.path);
    if (device === TOOL || device === SESSION_TOOL) {
      if (decode(input.path).toLowerCase() !== `xd://${device}`) fail("invalid lifecycle device target");
      checkFrameworkToolCall({ toolName: device, input: input.content }, ctx, workspace, depth + 1);
      return;
    }
    if (MUTATORS.has(device) || WRAPPERS.has(device)) {
      checkFrameworkToolCall({ toolName: device, input: input.content }, ctx, workspace, depth + 1);
    }
  }
}

function privateFrameworkTarget(target, workspace, cwd) {
  const decoded = decode(target);
  if (/^(?:agent|history|sessions?|irc):\/\//i.test(decoded)) return true;
  let local = decoded;
  if (/^file:\/\//i.test(decoded)) {
    // Preserve escaped filename characters until URL conversion.
    const url = /^file:\/\//i.test(target) ? target : decoded;
    local = fileURLToPath(url.replace(/:(?:raw(?::.*)?|[-\d,+:]+(?::raw)?)$/i, ""));
  } else if (/^[a-z][a-z0-9+.-]*:\/\//i.test(decoded)) return false;
  const relative = path.relative(workspace, resolvedTarget(local.replace(/:(?:raw(?::.*)?|[-\d,+:]+(?::raw)?)$/i, ""), cwd)).replace(/\\/g, "/");
  return /^\.game-l10n\/(?:data-plane|native\/(?:tasks|results)|diagnostics\/logs)(?:\/|$)/.test(relative)
    && !/(?:^|\/)(?:relay|receipt|control-envelope(?:\.[^/]*)?)\.json$/.test(relative);
}

export function hasFrameworkExposure(event, ctx, workspace) {
  if (ctx.agent?.kind !== "main" || event.isError || !event.input) return false;
  const tool = name(event.toolName);
  // This root tool reports controller registration, not session history.
  if (tool === SESSION_TOOL) return false;
  if (/(?:history|sessions?|messages?|irc)/.test(tool)) return true;
  const input = event.input;
  if (tool === "task") return (input.tasks ?? [input]).some(item =>
    item.agent === "seance" || (typeof item.sourceSession === "string" && item.sourceSession.trim()));
  // Glob returns filenames only. A public grep zero-match receipt contains no
  // semantic source; never infer exposure from its searched directory alone.
  if (tool === "glob" || (tool === "grep" && event.details?.matchCount === 0)) return false;
  if (!["read", "grep"].includes(tool)) return false;
  if (tool === "grep") {
    const files = event.details?.files ?? event.details?.fileMatches?.map(file => file.path);
    if (Array.isArray(files) && files.length) {
      const bases = [ctx.cwd];
      for (const key of ["cwd", "searchPath"]) {
        if (typeof event.details[key] === "string") bases.push(event.details[key]);
      }
      // Use public returned-file provenance, including a broad "." search;
      // do not inspect source snippets or mistake metadata-only hits for leaks.
      return files.some(file => typeof file === "string" && bases.some(base =>
        privateFrameworkTarget(file, workspace, base)));
    }
  }
  for (const field of ["path", "target", "uri", "url"]) {
    if (typeof input[field] === "string" && input[field].split(";").some(target =>
      privateFrameworkTarget(target, workspace, ctx.cwd))) return true;
  }
  return false;
}

export async function recordFrameworkExposure(event, ctx, workspace) {
  if (!hasFrameworkExposure(event, ctx, workspace)) return;
  const result = await invoke(pythonFromPath(), [
    "record-control-plane-leak", "--workspace", workspace,
    "--incident-code", "parent_opened_data_plane",
  ], workspace);
  if (result.isError) fail("source/history exposure recorded incompletely; stop localization and repair control state");
}
function pythonFromPath() {
  for (const executable of process.platform === "win32" ? ["python.exe", "python3.exe"] : ["python3", "python"]) {
    for (const directory of (process.env.PATH ?? "").split(path.delimiter)) {
      if (!directory || !path.isAbsolute(directory)) continue;
      try {
        const candidate = path.join(directory, executable);
        accessSync(candidate, process.platform === "win32" ? constants.F_OK : constants.X_OK);
        if (statSync(candidate).isFile()) return realpathSync(candidate);
      } catch { /* Follow the native guard's shell-free discovery convention. */ }
    }
  }
  fail("native Python unavailable");
}
function revision(workspace) {
  const file = path.join(workspace, ".game-l10n/framework-session.json");
  try { if (!lstatSync(file).isFile()) fail("invalid lifecycle metadata"); }
  catch (error) { if (error.code === "ENOENT") return 0; throw error; }
  let state;
  try { state = JSON.parse(readFileSync(file, "utf8")); }
  catch { fail("invalid lifecycle metadata"); }
  if (!state || typeof state !== "object" || state.schema !== "parr0t.framework-session.v1" || !Number.isSafeInteger(state.revision) || state.revision < 1) fail("invalid lifecycle metadata");
  return state.revision;
}
function privateDirectory(workspace) {
  let directory = workspace;
  for (const component of [".game-l10n", "framework-approvals"]) {
    directory = path.join(directory, component);
    try { mkdirSync(directory, { mode: 0o700 }); } catch (error) { if (error.code !== "EEXIST") throw error; }
    if (!lstatSync(directory).isDirectory() || lstatSync(directory).isSymbolicLink() || realpathSync(directory) !== directory) fail("invalid approval directory");
  }
  return directory;
}
async function invoke(executable, argv, workspace, proof, signal) {
  if (signal?.aborted) fail("request cancelled");
  return await new Promise((resolve, reject) => {
    const env = { ...process.env, PYTHONDONTWRITEBYTECODE: "1" };
    delete env.PYTHONSTARTUP;
    delete env.PYTHONINSPECT;
    const child = spawn(executable, ["-I", "-B", ENTRY, ...argv], { cwd: workspace, env, shell: false, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    const chunks = { stdout: [], stderr: [] };
    const sizes = { stdout: 0, stderr: 0 };
    let truncated = false;
    for (const stream of ["stdout", "stderr"]) child[stream].on("data", chunk => {
      const remaining = LIMIT - sizes[stream];
      if (chunk.length > remaining) truncated = true;
      if (remaining > 0) { chunks[stream].push(chunk.subarray(0, remaining)); sizes[stream] += Math.min(chunk.length, remaining); }
    });
    const cancel = () => child.kill();
    signal?.addEventListener("abort", cancel, { once: true });
    if (signal?.aborted) cancel();
    child.once("error", () => { signal?.removeEventListener("abort", cancel); reject(new Error(PREFIX + "native invocation failed")); });
    child.stdin.on("error", () => {}); // Early Python rejection may close stdin.
    child.stdin.end(proof === undefined ? undefined : JSON.stringify(proof));
    child.once("close", (code, terminationSignal) => {
      signal?.removeEventListener("abort", cancel);
      const clean = stream => {
        const text = Buffer.concat(chunks[stream]).toString("utf8");
        return proof === undefined ? text : text.replaceAll(proof.token, "[redacted]");
      };
      const details = { exitCode: code, signal: terminationSignal, stdout: clean("stdout"), stderr: clean("stderr"), cancelled: Boolean(signal?.aborted), outputComplete: !truncated };
      resolve({ content: [{ type: "text", text: JSON.stringify(details) }], details, isError: code !== 0 || details.cancelled || truncated });
    });
  });
}
export default function registerFrameworkModeApproval(pi, getWorkspace) {
  const z = pi.zod;
  pi.registerTool({
    name: TOOL,
    label: "Parr0t Framework Mode (Root User Request)",
    description: "Root-only lifecycle change bound to an explicit user request. Supply an absolute workspace in ordinary sessions; bound helpers default to their workspace. In analysis, omp_subagents selects only the exact OMP child IDs the user approved for diagnostic snapshots. Set user_requested:true only for the exact user-requested action and scope.",
    loadMode: "essential",
    parameters: z.object({ workspace: z.string().optional(), mode: z.string(), user_requested: z.boolean(),
      logs: z.array(z.string()).optional(), omp_subagents: z.array(z.string()).optional() }).strict(),
    async execute(_id, input, signal, _onUpdate, ctx) {
      const identity = authority(ctx);
      parameters(input);
      if (signal?.aborted) fail("request cancelled");
      const workspace = workspaceFor(input, getWorkspace?.(ctx));
      const logs = (input.logs ?? []).map(log => path.resolve(workspace, log));
      const subagents = input.omp_subagents?.slice() ?? [];
      const ompTranscripts = subagents.length ? { session_file: sessionFile(ctx), subagents } : undefined;
      const current = revision(workspace);
      const request = { mode: input.mode };
      const after = authority(ctx);
      if (identity.agent !== after.agent || identity.session !== after.session || workspaceFor(input, getWorkspace?.(ctx)) !== workspace
          || revision(workspace) !== current || (ompTranscripts && sessionFile(ctx) !== ompTranscripts.session_file)) fail("host session or lifecycle changed during authorization");
      if (signal?.aborted) fail("request cancelled");
      const python = pythonFromPath();
      const id = randomBytes(16).toString("hex");
      const token = randomBytes(32).toString("hex");
      const record = { schema: "parr0t.framework-mode-approval.v1", workspace, session_id: identity.session, agent_id: identity.agent,
        agent_kind: "main", agent_depth: 0, ...request, revision: current, logs,
        ...(ompTranscripts && { omp_transcripts: ompTranscripts }),
        token_sha256: createHash("sha256").update(token, "ascii").digest("hex"), expires_at: Math.floor(Date.now() / 1000) + 120 };
      const file = path.join(privateDirectory(workspace), `${id}.json`);
      writeFileSync(file, JSON.stringify(record), { flag: "wx", mode: 0o600 });
      const argv = ["framework-mode", "--workspace", workspace, "--mode", request.mode, "--user-requested", "--approval-stdin"];
      for (const log of logs) argv.push("--log", log);
      for (const id of subagents) argv.push("--omp-subagent", id);
      try { return await invoke(python, argv, workspace, { id, token }, signal); }
      finally { try { unlinkSync(file); } catch (error) { if (error.code !== "ENOENT") throw error; } }
    },
  });
}

function currentRegistration(pi, ctx, workspace, identity, model, boundWorkspace) {
  const launch = process.env.PARR0T_OMP_CONTROL_DIRECTORY;
  if (launch === undefined || boundWorkspace === undefined || realpathSync(boundWorkspace) !== workspace) return;
  if (!path.isAbsolute(launch)) fail("invalid current control launch");
  const directory = realpathSync(launch);
  if (path.dirname(directory) !== path.join(workspace, ".game-l10n", "omp-control")
      || !/^[a-f0-9]{32}$/.test(path.basename(directory))
      || realpathSync(ctx.cwd) !== workspace) fail("current control launch workspace mismatch");
  const receipt = JSON.parse(readFileSync(path.join(directory, "ready.json"), "utf8"));
  const registered = pi.getAllTools?.();
  const active = pi.getActiveTools?.();
  const guarded = typeof receipt?.guard_sha256 === "string";
  const resource = new URL(guarded ? "./omp-isolation-guard.mjs" : "./omp-prompt-isolation.mjs", import.meta.url);
  const digest = createHash("sha256").update(readFileSync(resource)).digest("hex");
  if (receipt.schema !== "parr0t.omp-control-ready.v1" || receipt.launch_id !== path.basename(directory)
      || receipt.session_id !== identity.session || receipt.process_id !== process.pid
      || receipt.framework_tool !== TOOL || receipt.process_tool !== (guarded ? "parr0t_exec" : "parr0t_process")
      || (guarded ? receipt.guard_sha256 : receipt.helper_sha256) !== digest
      || !Array.isArray(registered) || !Array.isArray(active)
      || ![receipt.process_tool, TOOL].every(tool => registered.some(item => item.name === tool) && active.includes(tool))) fail("current root readiness is unavailable; refusing duplicate controller launch");
  if (model !== `${ctx.model?.provider}/${ctx.model?.id}`) fail("current root already registered with a different model");
  const details = { status: guarded ? "guard_registered" : "helper_registered", existing: true, workspace, registration: receipt };
  return { content: [{ type: "text", text: JSON.stringify(details) }], details, isError: false };
}

export function registerParr0tSession(pi, getWorkspace) {
  const z = pi.zod;
  pi.registerTool({
    name: SESSION_TOOL,
    label: "Parr0t Session (Root User Request)",
    description: "Launch the official fresh Parr0t controller for the user's requested automation, preserving its request, checkpoints and actual readiness/error result. Supply an absolute workspace to select the target; bound roots default to their own workspace. Defaults to this root's exact provider/model and thinking; when the host model is unavailable supply an exact model. Never launches a semantic worker from this caller.",
    loadMode: "essential",
    parameters: z.object({ workspace: z.string().optional(), model: z.string().optional(), mode: z.string().optional(), user_requested: z.boolean() }).strict(),
    async execute(_id, input, signal, _onUpdate, ctx) {
      const identity = authority(ctx);
      sessionParameters(input);
      const boundWorkspace = getWorkspace?.(ctx);
      const workspace = workspaceFor(input, boundWorkspace, true);
      const currentModel = ctx.model;
      const model = input.model ?? (typeof currentModel?.provider === "string" && typeof currentModel?.id === "string"
        ? `${currentModel.provider}/${currentModel.id}` : undefined);
      if (!exactModel(model)) fail("host model unavailable: explicit exact model required");
      if (signal?.aborted) fail("request cancelled");
      const existing = currentRegistration(pi, ctx, workspace, identity, model, boundWorkspace);
      if (existing) return existing;
      const argv = ["omp-session", "--workspace", workspace, "--model", model, "--mode", input.mode ?? "auto"];
      if (input.model === undefined) {
        const thinking = pi.getThinkingLevel?.();
        if (thinking !== undefined) {
          if (typeof thinking !== "string" || !thinking.trim() || thinking.includes("\0")) fail("invalid host thinking level");
          argv.push("--thinking", thinking);
        }
      }
      const after = authority(ctx);
      if (identity.agent !== after.agent || identity.session !== after.session || workspaceFor(input, getWorkspace?.(ctx), true) !== workspace) fail("host session changed during authorization");
      return await invoke(pythonFromPath(), argv, workspace, undefined, signal);
    },
  });
}
