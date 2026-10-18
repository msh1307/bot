// Bound provider dispatch inside OMP, before HTTP starts. No prompt rewriting.
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
      streamSimple: (model: any, messages: any, options: any) => streamSimple(
        { ...model, api: "openai-completions" }, messages,
        { ...options, maxHttpAttempts: 1 },
      ),
    });
    if (!await pi.setModel({ ...selected, api })) throw new Error("EVO bounded provider setup failed");
  });
  let requests = 0;
  pi.on("before_agent_start", () => { requests = 0; });
  pi.on("before_provider_request", (_event: any, ctx: any) => {
    if (++requests > 40) ctx.abort();
  });
}
