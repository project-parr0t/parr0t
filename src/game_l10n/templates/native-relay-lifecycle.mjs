import { createHash } from "node:crypto";
import { appendFileSync, lstatSync, mkdirSync, readFileSync, realpathSync, renameSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";

const SCHEMA = "parr0t.omp-native-admission.v1";
const sha256 = value => createHash("sha256").update(value).digest("hex");
const denied = message => ({ block: true, reason: `PARR0T_NATIVE_LIFECYCLE: ${message}` });

function marker(prompt, name) {
  const matches = [...String(prompt ?? "").matchAll(new RegExp(`^${name}=([^\\r\\n]+)\\r?$`, "gm"))];
  if (matches.length > 1) throw new Error("duplicate binding marker");
  return matches[0]?.[1];
}
function admissionPath(workspace, fingerprint, task) {
  return path.join(workspace, ".game-l10n/native/omp-launch-admissions", `${sha256(`${fingerprint}\0${task}`)}.json`);
}
function writeRecord(file, record) {
  const temporary = `${file}.${process.pid}.tmp`;
  writeFileSync(temporary, JSON.stringify(record), { mode: 0o600 });
  renameSync(temporary, file);
}

function recordWaveTiming(current, event, startedAt, endedAt, task) {
  try {
    const logs = path.join(realpathSync(current.workspace), "logs");
    mkdirSync(logs, { recursive: true, mode: 0o700 });
    if (realpathSync(logs) !== logs) return;
    const directory = path.join(logs, "native-wave-timing");
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    if (realpathSync(directory) !== directory) return;
    const task_sha256 = task === undefined ? undefined : sha256(task);
    const key = task_sha256 === undefined ? current.fingerprint : `${current.fingerprint}-${task_sha256}`;
    // The event itself is the exclusive claim; no log scans or separate markers.
    writeFileSync(path.join(directory, `${key}.json`), JSON.stringify({
      schema: "parr0t.native-wave-timing.v1", relay_sha256: current.fingerprint, task_sha256, event,
      elapsed_ms: Math.round(endedAt - startedAt), provider_generation_ms: null,
    }) + "\n", { flag: "wx", mode: 0o600 });
  } catch {
    // Timing is diagnostic only and cannot change lifecycle outcomes.
  }
}

function isReconciliation({ relay }) {
  return relay.schema === "parr0t.data-plane-relay.v4" && relay.worker_role === "inventory_reconciler";
}

function relayTiming(current, event, fields) {
  try {
    const directory = path.join(realpathSync(current.workspace), "logs");
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    if (realpathSync(directory) !== directory) return;
    const file = path.join(directory, "inventory-reconciliation-timing.jsonl");
    if (lstatSync(file, { throwIfNoEntry: false })?.isFile() === false) return;
    appendFileSync(file, JSON.stringify({
      schema: "parr0t.inventory-reconciliation-timing.v1",
      relay_sha256: current.fingerprint, event, ...fields,
    }) + "\n", { mode: 0o600 });
  } catch {
    // Timing is diagnostic only and never changes admission or completion.
  }
}

function verifiesInventory(event) {
  let input = event.input;
  if (event.toolName === "write" &&
      ["xd://parr0t_process", "xd://parr0t_exec"].includes(input?.path)) {
    try { input = JSON.parse(input.content); } catch { return false; }
  } else if (!["parr0t_process", "parr0t_exec"].includes(event.toolName)) {
    return false;
  }
  const argv = input?.argv;
  if (!Array.isArray(argv)) return false;
  const flag = argv.indexOf("--workspace");
  return flag > 0 && argv[flag - 1] === "verify-inventory";
}

// Catalog metadata is resolution evidence, never a per-call model grant.
export function publishHostModels(ctx, workspace) {
  if (ctx.agent?.kind !== "main" || ctx.agent.depth !== 0) return;
  const root = realpathSync(workspace);
  const directory = path.join(root, ".game-l10n", "native");
  const target = path.join(directory, "host-models.json");
  try {
    const session = ctx.sessionManager?.getSessionId?.();
    const catalog = ctx.modelRegistry?.getAll?.();
    if (typeof session !== "string" || !session.trim() || !Array.isArray(catalog)) throw new Error("metadata unavailable");
    const models = [];
    const seen = new Set();
    for (const model of catalog) {
      const provider = model?.provider;
      const id = model?.id;
      if (typeof provider !== "string" || !provider || typeof id !== "string" || !id) continue;
      const selector = `${provider}/${id}`;
      if (seen.has(selector)) continue;
      seen.add(selector);
      models.push({ provider, id, selector });
    }
    const record = { schema: "parr0t.native-host-models.v1", host: "omp",
      session_id: session, process_id: process.pid, models };
    if (typeof ctx.model?.provider === "string" && typeof ctx.model?.id === "string") {
      record.current_model = `${ctx.model.provider}/${ctx.model.id}`;
    }
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    if (realpathSync(directory) !== directory) return;
    writeRecord(target, record);
  } catch {
    // A previous session's catalog must not masquerade as this host's metadata.
    try { if (realpathSync(directory) === directory) rmSync(target, { force: true }); } catch {}
  }
}

// Runs only in a trusted loaded host extension. It does not dispatch another
// provider, inspect semantic payloads, or turn a model-written status into proof.
export default function createNativeRelayLifecycle({ workspace, relayPath, executeCore }) {
  const handlers = {};
  let bound;
  let worker;
  const calls = new Map();
  let relayClock;
  let workerTiming;

  function load(ctx) {
    const file = relayPath(ctx);
    if (!file) return undefined;
    const bytes = readFileSync(file);
    const relay = JSON.parse(bytes);
    const dispatches = relay.launch_contract?.worker_dispatches;
    if (!dispatches) return undefined; // Historical immutable launches retain their original protocol.
    return { file, relay, dispatches, fingerprint: sha256(bytes), workspace: workspace(ctx) };
  }
  async function command(argv, ctx) {
    const result = await executeCore(argv, ctx);
    if (result.code !== 0 || result.killed) throw new Error("core lifecycle command failed");
    const metadata = JSON.parse(result.stdout);
    return metadata;
  }
  function recordFor(task, current = bound) {
    const file = admissionPath(current.workspace, current.fingerprint, task);
    return { file, record: JSON.parse(readFileSync(file, "utf8")) };
  }
  function requireRecord(record, current, task) {
    if (record.schema !== SCHEMA || record.relay_sha256 !== current.fingerprint || record.task !== task
        || !["reserved", "started", "ending", "ended", "failed"].includes(record.state)) throw new Error("admission binding changed");
  }
  function waveSettled(current) {
    let ended = false;
    for (const task of Object.keys(current.dispatches)) {
      let record;
      try { ({ record } = recordFor(task, current)); }
      catch (error) { if (error.code === "ENOENT") continue; throw error; }
      requireRecord(record, current, task);
      if (["reserved", "started", "ending"].includes(record.state)) return false;
      ended ||= record.state === "ended";
    }
    return ended;
  }

  function singleWorkerHandoff(current) {
    const { relay, dispatches } = current;
    const launch = relay.launch_contract;
    const dispatch = dispatches["worker-0"];
    if (relay.schema !== "parr0t.data-plane-relay.v4"
        || relay.execution_plan?.tracking_required !== false
        || launch.host !== "omp" || (launch.transport ?? "collaboration") !== "collaboration"
        || launch.tasks?.length !== 1 || launch.tasks[0] !== "worker-0"
        || Object.keys(dispatches).length !== 1 || dispatch?.slot !== 0
        || launch.model !== dispatch.model
        || typeof dispatch.model !== "string"
        || !/^[A-Za-z0-9][A-Za-z0-9_.:@/+%-]{0,254}$/.test(dispatch.model)
        || !Array.isArray(relay.command_argv?.["capability-available"])
        || !Array.isArray(relay.command_argv?.["worker-started"]?.["worker-0"])) return;
    try {
      const { record } = recordFor("worker-0", current);
      requireRecord(record, current, "worker-0");
      return; // Existing admission must be handled under the normal lifecycle.
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    const call = JSON.stringify({
      context: "Execute the exact core-bound data-plane task.",
      tasks: [{ task: "PARR0T_OMP_TASK=worker-0", model: dispatch.model, solutionSpace: "Exact bound task only." }],
    });
    return [
      "OMP single-worker relay: dispatch first, without opening the relay file or making a separate preflight call.",
      `Your first tool call is the built-in task with ${call}. Resolve xd://task only if task is deferred.`,
      "The loaded host helper records capability and the actual worker start; it supplies the exact worker prompt. Do not duplicate those calls or dispatch twice.",
      "Wait for actual terminal lifecycle, not a time limit. For sealing or an observed dispatch/runtime error, read the bound relay command_argv and follow its exact lifecycle. Do not read or forward child output. Return only the sealed receipt path.",
    ].join("\n");
  }

  async function flushContextEnds(current, ctx) {
    if (!current.relay.execution_plan?.tracking_required) return;
    const keys = Object.keys(current.dispatches).filter(key => key !== "finalizer");
    for (const task of keys) {
      let entry;
      try { entry = recordFor(task, current); }
      catch (error) { if (error.code === "ENOENT") continue; throw error; }
      const { file, record } = entry;
      requireRecord(record, current, task);
      if (record.state !== "ending") continue;
      const slot = current.dispatches[task].slot;
      await command(current.relay.command_argv["relay-events"][`worker-ended:${slot}`], ctx);
      writeRecord(file, { ...record, state: "ended" });
    }
  }

  handlers.before_agent_start = async (event, ctx) => {
    if (ctx.agent?.kind !== "sub") return;
    const current = load(ctx);
    if (!current) return;
    bound = current;
    const task = marker(event.prompt, "PARR0T_OMP_TASK");
    if (ctx.agent.depth === 1) {
      if (task) throw new Error("PARR0T_NATIVE_LIFECYCLE: relay cannot impersonate a worker");
      if ((isReconciliation(current) || current.relay.schema === "parr0t.native-wave-relay.v1")
          && (relayClock?.current.fingerprint !== current.fingerprint || relayClock.agentId !== ctx.agent.id)) {
        relayClock = { current, agentId: ctx.agent.id, startedAt: performance.now() };
      }
      return singleWorkerHandoff(current);
    }
    if (!task || !Object.hasOwn(current.dispatches, task)) throw new Error("PARR0T_NATIVE_LIFECYCLE: exact worker binding required");
    const dispatch = current.dispatches[task];
    const { file, record } = recordFor(task, current);
    requireRecord(record, current, task);
    if (record.parent_id !== ctx.agent.parentId || (record.worker_id !== null && record.worker_id !== ctx.agent.id)
        || !["reserved", "started"].includes(record.state)) throw new Error("PARR0T_NATIVE_LIFECYCLE: duplicate or ended worker");
    if (sha256(readFileSync(dispatch.task_path)) !== dispatch.task_sha256) throw new Error("PARR0T_NATIVE_LIFECYCLE: task digest changed");
    if (record.state === "reserved") {
      const argv = current.relay.command_argv["worker-started"][task].map(value => value === "<worker-id>" ? ctx.agent.id : value);
      const started = await command(argv, ctx);
      if (started.unstarted_tasks?.includes(task) || started.issue_codes?.some(code => code !== "E_NATIVE_WORKER_NOT_STARTED")) {
        throw new Error("PARR0T_NATIVE_LIFECYCLE: worker start was not admitted");
      }
      writeRecord(file, { ...record, worker_id: ctx.agent.id, state: "started" });
      const eventArgv = current.relay.command_argv["relay-events"]?.[`worker-started:${dispatch.slot}`]
        ?? (task === "finalizer" ? current.relay.command_argv["relay-events"]?.["finalizer-started"] : undefined);
      if (eventArgv) await command(eventArgv, ctx);
      await flushContextEnds(current, ctx);
    }
    const startedAt = worker?.current.fingerprint === current.fingerprint && worker.task === task
      ? worker.startedAt
      : record.state === "reserved" && current.relay.schema === "parr0t.native-wave-relay.v1"
        ? performance.now() : undefined;
    worker = { task, file, current, startedAt };
    if (isReconciliation(current) && !workerTiming) {
      workerTiming = {
        startedAt: performance.now(), tools: new Map(), toolWindowStart: 0,
        toolMs: 0, toolCalls: 0, verifyMs: 0, verifyCalls: 0,
      };
    }
  };

  handlers.before_subagent_spawn = (event, ctx) => {
    if (!bound || ctx.agent?.kind !== "sub") return;
    if (worker || ctx.agent.depth !== 1) return denied("semantic worker delegation is forbidden");
    if (event.invocationKind !== "task") return denied("use the native task tool, not eval agent/workpool");
    if (event.patterns?.length !== 1 || event.patterns[0] !== bound.relay.launch_contract.model) return denied("exact bound model required");
  };

  handlers.tool_call = async (event, ctx) => {
    if (workerTiming && event.toolCallId && !workerTiming.tools.has(event.toolCallId)) {
      const startedAt = performance.now();
      if (!workerTiming.tools.size) workerTiming.toolWindowStart = startedAt;
      workerTiming.tools.set(event.toolCallId, { startedAt, verify: verifiesInventory(event) });
      workerTiming.toolCalls++;
    }
    if (!bound || ctx.agent?.kind !== "sub" || ctx.agent.depth !== 1) return;
    const created = [];
    try {
      bound = load(ctx);
      if (!bound) throw new Error("launch binding disappeared");
      if (["parr0t_process", "parr0t_exec"].includes(event.toolName)) {
        const argv = event.input.argv;
        if (Array.isArray(argv) && argv.some(value => ["seal-data-plane-task", "seal-native-wave-relay"].includes(value))) {
          for (const task of Object.keys(bound.dispatches)) {
            let entry;
            try { entry = recordFor(task); } catch (error) { if (error.code === "ENOENT") continue; throw error; }
            requireRecord(entry.record, bound, task);
            if (["reserved", "started", "ending"].includes(entry.record.state)) throw new Error("worker is still active; do not seal");
          }
        }
        return;
      }
      if (event.toolName !== "task") return;
      const input = structuredClone(event.input);
      const items = input.tasks ?? [input];
      if (!Array.isArray(items) || !items.length) throw new Error("bound task batch required");
      const selected = new Set();
      const plans = items.map(item => {
        const text = item.task ?? item.steps?.join("\n") ?? "";
        const requested = marker(text, "PARR0T_OMP_TASK");
        const keys = Object.keys(bound.dispatches);
        const task = requested ?? (keys.length === 1 ? keys[0] : undefined);
        if (!task || !Object.hasOwn(bound.dispatches, task) || selected.has(task)) throw new Error("one exact binding per worker required");
        selected.add(task);
        const dispatch = bound.dispatches[task];
        if (item.model !== undefined && item.model !== dispatch.model) throw new Error("requested model differs from bound model");
        if (["sourceSession", "history", "parentHistory", "inheritHistory", "session", "sessionId"].some(key => item[key] !== undefined)
            || item.tools?.length) throw new Error("worker history and callback tools are forbidden");
        const name = `Parr0t${sha256(`${bound.fingerprint}\0${task}`).slice(0, 20)}`;
        Object.assign(item, { model: dispatch.model, name, task: dispatch.worker_prompt });
        delete item.agent;
        delete item.steps;
        return { task, file: admissionPath(bound.workspace, bound.fingerprint, task) };
      });
      if (selected.has("finalizer")) {
        for (const task of Object.keys(bound.dispatches).filter(key => key !== "finalizer")) {
          const { record } = recordFor(task);
          requireRecord(record, bound, task);
          if (record.state !== "ended") throw new Error("finalizer requires every worker to end");
        }
      }
      for (const plan of plans) {
        try {
          const record = JSON.parse(readFileSync(plan.file, "utf8"));
          requireRecord(record, bound, plan.task);
          if (record.state !== "failed" || record.worker_id !== null) throw new Error("worker binding already admitted; never replay it");
        } catch (error) { if (error.code !== "ENOENT") throw error; }
      }
      // Reserve before any await or host spawn. A retry cannot create a second
      // worker even when task output/handle delivery is lost.
      for (const plan of plans) {
        mkdirSync(path.dirname(plan.file), { recursive: true });
        try {
          const prior = readFileSync(plan.file);
          renameSync(plan.file, `${plan.file}.failed-${sha256(prior)}`);
        } catch (error) { if (error.code !== "ENOENT") throw error; }
        writeFileSync(plan.file, JSON.stringify({ schema: SCHEMA, relay_sha256: bound.fingerprint,
          task: plan.task, parent_id: ctx.agent.id, worker_id: null, state: "reserved" }), { flag: "wx", mode: 0o600 });
        created.push(plan);
      }
      calls.set(event.toolCallId, { plans, current: bound });
      const preflight = await command(bound.relay.command_argv["capability-available"], ctx);
      if (preflight.issue_codes?.length) throw new Error("dispatch preflight failed");
      if (plans.some(plan => !preflight.unstarted_tasks.includes(plan.task))) throw new Error("worker already started");
      return { input };
    } catch (error) {
      calls.delete(event.toolCallId);
      for (const plan of created) {
        const record = JSON.parse(readFileSync(plan.file, "utf8"));
        if (record.state === "reserved" && record.worker_id === null) writeRecord(plan.file, { ...record, state: "failed" });
      }
      return denied(error.code === "EEXIST" ? "worker binding already admitted; never replay it" : error.message);
    }
  };

  handlers.tool_result = (event) => {
    const timed = workerTiming?.tools.get(event.toolCallId);
    if (timed) {
      const endedAt = performance.now();
      workerTiming.tools.delete(event.toolCallId);
      if (!workerTiming.tools.size) workerTiming.toolMs += endedAt - workerTiming.toolWindowStart;
      if (timed.verify) {
        workerTiming.verifyCalls++;
        workerTiming.verifyMs += endedAt - timed.startedAt;
      }
    }
    const call = calls.get(event.toolCallId);
    if (!call) return;
    calls.delete(event.toolCallId);
    if (!event.isError) return;
    // Only a host-reported failure before the child entered its own session
    // may close a reservation. It never fabricates a semantic result/start.
    for (const plan of call.plans) {
      const record = JSON.parse(readFileSync(plan.file, "utf8"));
      requireRecord(record, call.current, plan.task);
      if (record.state === "reserved" && record.worker_id === null) writeRecord(plan.file, { ...record, state: "failed" });
    }
  };

  handlers.agent_end = async (event, ctx) => {
    if (event.willContinue || event.awaitingAsyncWork) return;
    if (!worker) {
      if (relayClock?.agentId !== ctx.agent?.id || ctx.agent?.depth !== 1) return;
      const endedAt = performance.now();
      const { current, startedAt } = relayClock;
      relayClock = undefined;
      try {
        if (load(ctx)?.fingerprint === current.fingerprint && waveSettled(current)) {
          if (isReconciliation(current)) {
            relayTiming(current, "relay_ended", { elapsed_ms: Math.round(endedAt - startedAt), provider_generation_ms: null });
          } else {
            recordWaveTiming(current, "relay_ended", startedAt, endedAt);
          }
        }
      } catch {
        // Missing or changed lifecycle evidence is not a settled wave.
      }
      return;
    }
    const { task, file, current, startedAt } = worker;
    const endedAt = startedAt === undefined && !workerTiming ? undefined : performance.now();
    const record = JSON.parse(readFileSync(file, "utf8"));
    requireRecord(record, current, task);
    if (record.worker_id !== ctx.agent.id || record.state !== "started") throw new Error("PARR0T_NATIVE_LIFECYCLE: terminal worker identity changed");
    if (current.relay.execution_plan?.tracking_required && task !== "finalizer") {
      writeRecord(file, { ...record, state: "ending" });
      await flushContextEnds(current, ctx);
    } else {
      const eventArgv = task === "finalizer" ? current.relay.command_argv["relay-events"]?.["finalizer-ended"] : undefined;
      if (eventArgv) await command(eventArgv, ctx);
      writeRecord(file, { ...record, state: "ended" });
    }
    if (startedAt !== undefined) recordWaveTiming(current, "worker_ended", startedAt, endedAt, task);
    if (workerTiming) {
      const { startedAt, tools, toolWindowStart, toolMs, toolCalls, verifyCalls, verifyMs } = workerTiming;
      relayTiming(current, "worker_ended", {
        elapsed_ms: Math.round(endedAt - startedAt),
        provider_generation_ms: null,
        observed_tool_ms: Math.round(toolMs + (tools.size ? endedAt - toolWindowStart : 0)),
        tool_calls: toolCalls, unfinished_tool_calls: tools.size,
        inventory_verify_calls: verifyCalls, inventory_verify_ms: Math.round(verifyMs),
      });
      workerTiming = undefined;
    }
    worker = undefined;
  };
  return handlers;
}
