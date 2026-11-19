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
  // Register ordinary entry tools before the host builds its initial registry.
  // Late session_start registration also awaits prompt rediscovery in OMP.
  // The official bound launcher supplies this helper-owned flag; its dedicated
  // helper/guard registers the tools instead, including guarded Python execution.
  const end = process.argv.indexOf("--", 2);
  const args = process.argv.slice(2, end < 0 ? undefined : end);
  const dedicated = args.some(arg => arg === "--parr0t-workspace" || arg.startsWith("--parr0t-workspace="));
  if (!dedicated) {
    registerNativeProcess(pi);
    registerFrameworkModeApproval(pi);
    registerParr0tSession(pi);
  }
  pi.on("session_start", (_event, ctx) => captureOperatorConfigs(ctx));
  pi.on("tool_result", observeOperatorPolicy);
}
