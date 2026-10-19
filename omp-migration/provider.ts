// Bound HTTP retries and preserve thinking during compaction.
import { streamSimple } from "@oh-my-pi/pi-ai";

export default function (pi: any) {
  // Keep retry ownership in the agent layer. The default HTTP transport and
  // replay-safe wrapper otherwise multiply three agent attempts into 36 calls.
  pi.on("session_start", async (_event: any, ctx: any) => {
    const selected = ctx.model;
    if (selected?.provider !== "evo") return;
    const api = "evo-bounded-completions";
    const models = ctx.models.list().filter((m: any) => m.provider === "evo")
      .map((m: any) => ({ ...m, api }));
    pi.registerProvider("evo", {
      api, models, baseUrl: selected.baseUrl, apiKey: "evo-local", authHeader: false,
      streamSimple: (model: any, messages: any, options: any) => {
        const compaction = options?.metadata?.omp_operation === "compaction";
        return streamSimple({ ...model, api: "openai-completions" }, messages, {
          ...options, maxHttpAttempts: 1,
          ...(compaction ? { onPayload: async (payload: any, info: any, signal: any) => {
            const body = await options?.onPayload?.(payload, info, signal) ?? payload;
            const thinking = options.metadata.omp_compaction_thinking !== false;
            // Strata's opt-in limit counts only final-answer tokens. A zero
            // total cap lets reasoning use the remaining physical context.
            const separateLimit = model.id === "sc117-flash-next-iq3_xxs" ? {
              max_tokens: 0, final_output_max_tokens: Math.min(8192, options.maxTokens ?? 8192),
              reasoning_loop_recovery: "stop",
            } : {};
            return { ...body, ...separateLimit, reasoning_effort: thinking ? "high" : "none",
              reasoning_budget_tokens: 0, chat_template_kwargs: {
              ...(body.chat_template_kwargs ?? {}), enable_thinking: thinking,
            } };
          } } : {}),
        });
      },
    });
    if (!await pi.setModel({ ...selected, api })) throw new Error("EVO bounded provider setup failed");
  });
}
