import registerFrameworkModeApproval, { registerParr0tSession } from "./framework-mode-approval.mjs";
import { captureOperatorConfigs, observeOperatorPolicy, withObservedOperatorPolicy } from "./omp-host-policy.mjs";

// Native argv execution is separate from Parr0t worker isolation and readiness.
export function registerNativeProcess(pi, { beforeExecute, defaultCwd } = {}) {
  const z = pi.zod;
  if (!z || typeof pi.exec !== "function") throw new Error("PARR0T_OMP_PROMPT_ISOLATION: native process API required");
  pi.registerTool({
    name: "parr0t_process",
    label: "Parr0t Native Process",
    description: "Execute native argv (executable first), including ordinary Python, without OMP virtual-URL or IRC routing. No shell interpolation, implicit timeout, execution guard registration, or OS sandbox. Availability alone does not attest Parr0t worker isolation; use the official session launcher for workers.",
    loadMode: "essential",
    parameters: z.object({ argv: z.array(z.string()).min(1), cwd: z.string().optional() }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      if (!Array.isArray(params.argv) || !params.argv.length
          || params.argv.some(arg => typeof arg !== "string" || arg.includes("\0"))
          || !params.argv[0].trim()) throw new Error("PARR0T_OMP_PROMPT_ISOLATION: native argv required");
      await beforeExecute?.(params, ctx);
      const argv = withObservedOperatorPolicy(params.argv, ctx);
      const result = await pi.exec(argv[0], argv.slice(1), {
        cwd: params.cwd ?? defaultCwd?.(ctx) ?? ctx.cwd, signal,
      });
      return {
        content: [{ type: "text", text: result.stdout + result.stderr }],
        details: result,
        isError: result.code !== 0 || result.killed,
      };
    },
  });
}

export default function bootstrapNativeProcess(pi) {
  // The host binds availability APIs before session_start and waits for dynamic
  // registrations before exposing tools. Explicit helpers/guards take priority.
  pi.on("session_start", (_event, ctx) => {
    captureOperatorConfigs(ctx);
    const tools = pi.getAllTools();
    const names = new Set(tools.map(tool => tool.name));
    if (!names.has("parr0t_process") && !names.has("parr0t_exec") && typeof pi.exec === "function") registerNativeProcess(pi);
    if (!names.has("parr0t_framework_mode")) registerFrameworkModeApproval(pi);
    if (!names.has("parr0t_session")) registerParr0tSession(pi);
  });
  pi.on("tool_result", observeOperatorPolicy);
}
