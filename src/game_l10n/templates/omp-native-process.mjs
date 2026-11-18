// Native argv execution is separate from Parr0t worker isolation and readiness.
export function registerNativeProcess(pi, { beforeExecute, defaultCwd } = {}) {
  const z = pi.zod;
  if (!z || typeof pi.exec !== "function") throw new Error("PARR0T_OMP_PROMPT_ISOLATION: native process API required");
  pi.registerTool({
    name: "parr0t_process",
    label: "Parr0t Native Process",
    description: "Execute native argv (executable first), including ordinary Python, without OMP virtual-URL or IRC routing. No shell interpolation, implicit timeout, execution guard registration, or OS sandbox. Availability alone does not attest Parr0t worker isolation; use the official session launcher for workers.",
    parameters: z.object({ argv: z.array(z.string()).min(1), cwd: z.string().optional() }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      if (!Array.isArray(params.argv) || !params.argv.length
          || params.argv.some(arg => typeof arg !== "string" || arg.includes("\0"))
          || !params.argv[0].trim()) throw new Error("PARR0T_OMP_PROMPT_ISOLATION: native argv required");
      await beforeExecute?.(params, ctx);
      const result = await pi.exec(params.argv[0], params.argv.slice(1), {
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
  pi.on("session_start", () => {
    const tools = pi.getAllTools();
    if (tools.some(tool => tool.name === "parr0t_process" || tool.name === "parr0t_exec")) return;
    registerNativeProcess(pi);
  });
}
