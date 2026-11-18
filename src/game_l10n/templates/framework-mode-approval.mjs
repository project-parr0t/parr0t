import { spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { accessSync, constants, lstatSync, mkdirSync, readFileSync, realpathSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ENTRY = fileURLToPath(new URL("./native-python.py", import.meta.url));
const LIMIT = 1024 * 1024;
const TOOL = "parr0t_framework_mode";
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
function parameters(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)
      || Object.keys(input).some(key => !["mode", "logs"].includes(key))
      || !["analysis", "shutdown"].includes(input.mode)
      || (input.logs !== undefined && (!Array.isArray(input.logs) || input.logs.some(log => typeof log !== "string" || !log.trim() || log.includes("\0"))))
      || (input.mode !== "analysis" && input.logs?.length)) fail("invalid lifecycle request");
}
// Called before either extension's role-specific tool policy, including root.
export function checkFrameworkToolCall(event, ctx, workspace, depth = 0) {
  if (depth > 32) fail("nested tool limit exceeded");
  const tool = name(event.toolName);
  let input = event.input;
  if (typeof input === "string") {
    if (tool === "edit") input = { input };
    else { try { input = JSON.parse(input); } catch { if (WRAPPERS.has(tool) || MUTATORS.has(tool) || tool === TOOL) fail("uninspectable mutation denied"); return; } }
  }
  if (tool === TOOL) { authority(ctx); parameters(input); return; }
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
    if (MUTATORS.has(device) || WRAPPERS.has(device) || device === TOOL) {
      checkFrameworkToolCall({ toolName: device, input: input.content }, ctx, workspace, depth + 1);
    }
  }
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
    child.stdin.end(JSON.stringify(proof));
    child.once("close", (code, terminationSignal) => {
      signal?.removeEventListener("abort", cancel);
      const clean = stream => Buffer.concat(chunks[stream]).toString("utf8").replaceAll(proof.token, "[redacted]");
      const details = { exitCode: code, signal: terminationSignal, stdout: clean("stdout"), stderr: clean("stderr"), cancelled: Boolean(signal?.aborted), outputComplete: !truncated };
      resolve({ content: [{ type: "text", text: JSON.stringify(details) }], details, isError: code !== 0 || details.cancelled || truncated });
    });
  });
}
export default function registerFrameworkModeApproval(pi, getWorkspace) {
  const z = pi.zod;
  pi.registerTool({
    name: TOOL,
    label: "Parr0t Framework Mode (User Approval)",
    description: "Root-only lifecycle change. Requires an interactive host confirmation of the exact workspace, mode and diagnostic log paths; flags are not user consent.",
    parameters: z.object({ mode: z.string(), logs: z.array(z.string()).optional() }).strict(),
    async execute(_id, input, signal, _onUpdate, ctx) {
      const identity = authority(ctx);
      parameters(input);
      if (ctx.hasUI !== true || typeof ctx.ui?.confirm !== "function") fail("interactive user confirmation unavailable");
      if (signal?.aborted) fail("request cancelled");
      const workspace = realpathSync(getWorkspace(ctx));
      const logs = (input.logs ?? []).map(log => path.resolve(workspace, log));
      const current = revision(workspace);
      const request = { mode: input.mode };
      const message = [`Workspace: ${JSON.stringify(workspace)}`, `Mode: ${request.mode}`, "Selected diagnostic logs:", JSON.stringify(logs)].join("\n");
      if (await ctx.ui.confirm("Approve Parr0t framework mode change?", message) !== true) fail("user cancelled lifecycle change");
      const after = authority(ctx);
      if (identity.agent !== after.agent || identity.session !== after.session || realpathSync(getWorkspace(ctx)) !== workspace || revision(workspace) !== current) fail("host session or lifecycle changed during confirmation");
      if (signal?.aborted) fail("request cancelled");
      const python = pythonFromPath();
      const id = randomBytes(16).toString("hex");
      const token = randomBytes(32).toString("hex");
      const record = { schema: "parr0t.framework-mode-approval.v1", workspace, session_id: identity.session, agent_id: identity.agent,
        agent_kind: "main", agent_depth: 0, ...request, revision: current, logs,
        token_sha256: createHash("sha256").update(token, "ascii").digest("hex"), expires_at: Math.floor(Date.now() / 1000) + 120 };
      const file = path.join(privateDirectory(workspace), `${id}.json`);
      writeFileSync(file, JSON.stringify(record), { flag: "wx", mode: 0o600 });
      const argv = ["framework-mode", "--workspace", workspace, "--mode", request.mode, "--user-requested", "--approval-stdin"];
      for (const log of logs) argv.push("--log", log);
      try { return await invoke(python, argv, workspace, { id, token }, signal); }
      finally { try { unlinkSync(file); } catch (error) { if (error.code !== "ENOENT") throw error; } }
    },
  });
}
