import { createHash } from "node:crypto";
import { realpathSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ENTRY = fileURLToPath(new URL("./native-python.py", import.meta.url));

const CONFIGS = "PARR0T_OMP_OPERATOR_CONFIGS";
const POLICY = "PARR0T_OMP_MODEL_POLICY_FINGERPRINT";
const KEYS = new Set(["cfg://modelRoles", "cfg://task/perCallModelAllow", "cfg://task/agentModelOverrides"]);
const observations = new Map();

// Preserve only config files the operator actually supplied to this host CLI.
// Project/job model choices are not promoted into operator approval here.
export function captureOperatorConfigs(ctx, argv = process.argv) {
  if (ctx?.agent?.kind !== "main" || ctx.agent.depth !== 0) return;
  const configs = [];
  for (let index = 2; index < argv.length; index++) {
    let value;
    if (argv[index] === "--config") value = argv[++index];
    else if (argv[index].startsWith("--config=")) value = argv[index].slice(9);
    else continue;
    if (typeof value !== "string" || !value || value.includes("\0")) throw new Error("PARR0T_OMP_HOST_POLICY: invalid operator config");
    const file = realpathSync(path.resolve(value));
    if (!statSync(file).isFile()) throw new Error("PARR0T_OMP_HOST_POLICY: operator config must be a file");
    configs.push(file);
  }
  if (configs.length) process.env[CONFIGS] = JSON.stringify(configs);
}

// This is a retry identity, never an approval token. The real task host still
// checks its live operator grants before every worker can start.
export function observeOperatorPolicy(event, ctx) {
  if (ctx?.agent?.kind !== "main" || ctx.agent.depth !== 0 || event.isError
      || event.toolName?.split(/[./:]/).at(-1) !== "read" || !KEYS.has(event.input?.path)) return;
  const session = ctx.sessionManager?.getSessionId?.();
  if (typeof session !== "string" || !session) return;
  const text = (event.content ?? []).filter(part => part.type === "text").map(part => part.text).join("\n");
  if (!text) return;
  let observation = observations.get(session);
  if (!observation) { observation = { values: new Map() }; observations.set(session, observation); }
  observation.values.set(event.input.path, createHash("sha256").update(text).digest("hex"));
  const entries = [...observation.values].sort(([left], [right]) => left.localeCompare(right));
  observation.fingerprint = createHash("sha256").update(JSON.stringify(entries)).digest("hex");
  process.env[POLICY] = observation.fingerprint;
}

// The selected host's native executor need not inherit post-start JS env changes.
// Preserve its execution/cancellation backend; pass only verified policy metadata
// to this exact Core entrypoint, without rewriting unrelated native argv.
export function withObservedOperatorPolicy(argv, ctx) {
  if (ctx?.agent?.kind !== "main" || ctx.agent.depth !== 0) return argv;
  const fingerprint = observations.get(ctx.sessionManager?.getSessionId?.())?.fingerprint;
  const command = argv.indexOf("native-runtime");
  if (!fingerprint || command < 2 || argv.some(arg => arg === "--host-policy-fingerprint"
      || arg.startsWith("--host-policy-fingerprint="))) return argv;
  const args = argv.slice(command + 1);
  if (!args.some((arg, index) => arg === "--host=omp" || (arg === "--host" && args[index + 1] === "omp"))) return argv;
  try { if (realpathSync(argv[command - 1]) !== realpathSync(ENTRY)) return argv; }
  catch { return argv; }
  return [...argv, "--host-policy-fingerprint", fingerprint];
}
