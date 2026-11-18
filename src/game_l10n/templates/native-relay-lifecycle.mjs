import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
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

// Runs only in a trusted loaded host extension. It does not dispatch another
// provider, inspect semantic payloads, or turn a model-written status into proof.
export default function createNativeRelayLifecycle({ workspace, relayPath, executeCore }) {
  const handlers = {};
  let bound;
  let worker;
  const calls = new Map();

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
      return;
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
    worker = { task, file, current, dispatch };
  };

  handlers.before_subagent_spawn = (event, ctx) => {
    if (!bound || ctx.agent?.kind !== "sub") return;
    if (worker || ctx.agent.depth !== 1) return denied("semantic worker delegation is forbidden");
    if (event.invocationKind !== "task") return denied("use the native task tool, not eval agent/workpool");
    if (event.patterns?.length !== 1 || event.patterns[0] !== bound.relay.launch_contract.model) return denied("exact bound model required");
  };

  handlers.tool_call = async (event, ctx) => {
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
        Object.assign(item, { agent: "task", model: dispatch.model, name, task: dispatch.worker_prompt });
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
    if (!worker || event.willContinue || event.awaitingAsyncWork) return;
    const { task, file, current, dispatch } = worker;
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
    worker = undefined;
  };
  return handlers;
}
