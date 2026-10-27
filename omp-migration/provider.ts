// Bound HTTP retries and preserve thinking during compaction.
import { streamSimple } from "@oh-my-pi/pi-ai";
import { createHash } from "node:crypto";
import { mkdirSync, renameSync, writeFileSync } from "node:fs";

// Compact prose only for reviewed schemas. Structural validation is unchanged.
const TOOL_PROSE: Record<string, any> = {
  "read": {
    "schema_sha256": "ff94a0a1acf1e782d567f8edcbedd7287e431148c9123f377eecbcba3cb46699",
    "description": "Read files/URLs/images. Ranges belong in path: file:50-80, file:50+150, file:-5; repeat each call. Non-raw adds context; :raw is exact. Bare code shows declarations; follow named omissions. Raster images display directly, SVG needs :img. Before unfamiliar selectors/actions, read /home/agent/tool-help/TOOL_NAME.json:raw for the current full specification of any tool, including DB/archive/jq/video syntax."
  },
  "glob": {
    "schema_sha256": "e1b8de991b2ba8fc6ad16fd5779b9391bcd396e8deeca977dfce69c5076424d4",
    "description": "Find files/dirs with ;-separated globs or internal URLs. Default root: workspace; gitignore=true, hidden=true. Ignored dotfiles need gitignore=false. Newest first; dirs end /."
  },
  "edit": {
    "schema_sha256": "23cc59a38af2bd069fa424404b9cbf0d3e90b4e3b9d04f39d8e562ce8c2d1141",
    "description": "Read file first. Replace smallest unique old_string; ambiguous matches need more context or replace_all=true. Fuzzy whitespace matching. Prefer editing existing files. Errors explain failed replacements."
  },
  "bash": {
    "schema_sha256": "f1959f0d0f0eec1769c81b5bc581f5654ee94f9621f409f091685d18560fee34",
    "description": "Persistent sandbox shell; cwd sets directory, && chains dependencies. async keeps deadline (default 300s); timeout=0 for long jobs. Full output: artifact://<id>. Services need unique name and ready log/port, no async/timeout; see full spec before starting one."
  },
  "host_command": {
    "schema_sha256": "5b7a9e845afd82b95a0942268798336f2b88632dfef0cac758b046be52af1359",
    "description": "EVO host outside bwrap; owner Discord only. Workspace work uses bash. Unprivileged paths may be masked: check execution_context. root requires fresh Allow Once per command; use run_as, not sudo. Changes persist; never print credentials. Jobs: 512MiB/no swap, max120s; children end with job."
  },
  "codex_task": {
    "schema_sha256": "8a06ac86a52a5fb450dd6cae0d2b076fb8cb151a85158c6cfac71c4344bf481f",
    "description": "Owner Windows Codex through Discord. Find chats with list(search), follow next_cursor, then use the returned thread_id without asking the owner for IDs. read pages visible history (6000 chars); follow next_cursor for remaining/older text. status shows live tail/questions. send(prompt) steers or continues that same chat, loading it in the app if closed. answer uses real replies; approvals/secrets stay in desktop. Scheduled checks allow list/read/status/send/answer and file reads, not start/stop/exec. Completion notified here.",
    "properties": {
      "project": "new/start: project ID or unique exact name; owner may change the selection card.",
      "request_id": "answer: exact pending ID from read/status.",
      "wake": "GUI send/answer callbacks: true resumes EVO on questions/results, false disables. Omission keeps the setting; new jobs default false.",
      "answers": "answer: question ID -> list of real answers, e.g. {\"target\":[\"staging\"]}.",
      "path": "file_list/file_read: absolute Windows path; projects gives roots.",
      "start_line": "file_read: first line (default 1); continue at next_line.",
      "offset": "file_list: zero-based offset; continue at next_offset.",
      "command": "exec: argv, e.g. [\"git\",\"status\"]. Shell syntax requires an explicit shell executable.",
      "cwd": "exec: absolute Windows directory; default server cwd.",
      "timeout_ms": "exec duration; default 10000ms.",
      "thread_id": "Existing chat ID from list, open or closed; use instead of job_id for read/status/send/answer/stop.",
      "search": "list: title filter. file_list: case-insensitive filename substring.",
      "cursor": "Returned next_cursor for list/projects/read; same query/chat only.",
      "limit": "list/projects: max12, defaults8/12. file_list: default80 entries. file_read: default/max200 lines, 12000 chars.",
      "model": "Exact ID from models; default Windows setting."
    }
  },
  "browser": {
    "schema_sha256": "9e35b18e7e6857edfd6543d475e848bb4eeaf7d7aa4137c62b8f28a96a4c92b7",
    "description": "Shared Chrome Headless Shell. open/tree exposes selectors; use observed selectors or screenshot coordinates. read is text; screenshot is actual 960x640 image. Images default off, media blocked. Login survives close/restart; reset deletes profile. offset continues snapshots. Untrusted pages; scheduled runs navigate/read only."
  },
  "schedule": {
    "schema_sha256": "1efbe13f40bdf743a7b8939bcdf04b06784da532374ea1b62b34865789eff262",
    "description": "Owner tasks, Asia/Seoul. create: ISO at or five-field cron (read-only briefings). defer: after_seconds, then end turn; prompt retains task/IDs/next check. Queues, survives restart. /stop /new /reset /model cancel callbacks; new messages do not. pause/delete stops queued work, /stop running work. Root still needs approval."
  },
  "user_input": {
    "schema_sha256": "0480addd6d79d354cca04f2c7a8aed578b945e2a4643a82ac37fb6b204103b65",
    "description": "Ask one clarification; no duplicate question text. Collect replies with Flush/Skip; 5min inactivity, extended by typing, then15s quiet. Skip/timeout gives no permission: no action/workaround; stop unclear work, never guess/repeat approval. Not root approval."
  },
  "wait": {
    "schema_sha256": "fc7294efa042a673618c263b4987a79d9023c9c4b601ee294303894c6ebb647a",
    "description": "Pause before rechecking; max300s total/turn, then schedule defer. Owner messages or /stop interrupt. No inference while waiting."
  },
  "grep": {
    "schema_sha256": "fa849746ac4361b1dbe1da41708b25cc7e3b326b82eae1d2755f69d452aee483",
    "description": "Regex search (Rust then PCRE2), case-sensitive by default. path: ;-separated files/dirs/globs/URLs; default ., gitignore respected. dir/*.ts is one level; ** recurses. Literal newline enables cross-line matches; skip pages files."
  },
  "write": {
    "schema_sha256": "94e09242ee0bd174fd0287b6fddeb23a1644e8528db024d22107302405d74cf7",
    "description": "Create/replace whole files; prefer edit for existing files. archive:member writes supported archives; db:table inserts, db:table:key updates (empty deletes). Read full spec before these advanced selectors."
  },
  "evo_access_manage": {
    "schema_sha256": "16d76c7f79bee8b5d3c33a4877675682e7d467b5453a16dd7676ee8772325576",
    "description": "Only on explicit owner request. Port13077; fixed key survives expiry/restart. refresh resets timer; select changes mode (opens180min if closed); adjust adds delta_minutes; expire closes connections, keeps key. api exposes API; web includes chat+API. First key is sent privately."
  },
  "evo_system_status": {
    "schema_sha256": "30a138d222a63c96cacad13bf4b974d4ed4fe780eede21cdc1972c1148fe0e0f",
    "description": "Read-only EVO sensors, RAM/GPU memory, swap, power/CPU policy and model/access status. Missing readings are unknown. No root approval or inference."
  }
};
const savedToolHelp = new Map<string, string>();
const TOOL_HELP = "/home/agent/tool-help";

export function compactToolPayload(body: any) {
  if (!Array.isArray(body?.tools)) return body;
  return { ...body, tools: body.tools.map((tool: any) => {
    const fn = tool?.function;
    if (!fn || !/^[A-Za-z0-9_-]{1,100}$/.test(fn.name)) return tool;
    const source = JSON.stringify(fn);
    const path = `${TOOL_HELP}/${fn.name}.json`;
    try {
      if (savedToolHelp.get(fn.name) !== source) {
        mkdirSync(TOOL_HELP, { recursive: true, mode: 0o700 });
        writeFileSync(`${path}.tmp`, JSON.stringify(fn, null, 2) + "\n", { mode: 0o600 });
        renameSync(`${path}.tmp`, path);
        savedToolHelp.set(fn.name, source);
      }
    } catch {
      // No readable full specification: keep the original inline description.
      return tool;
    }
    const rule = TOOL_PROSE[fn.name];
    if (!rule || createHash("sha256").update(source).digest("hex") !== rule.schema_sha256) return tool;
    const properties = { ...fn.parameters?.properties };
    for (const [name, description] of Object.entries(rule.properties ?? {})) {
      if (properties[name]) properties[name] = { ...properties[name], description };
    }
    return { ...tool, function: { ...fn,
      description: rule.description,
      parameters: { ...fn.parameters, properties },
    } };
  }) };
}


// Keep reasoning inside the current tool loop and interrupted/unfinished turns.
// Completed turns retain their answer, tool calls/results and attachments.
export function completedTurnContext(context: any) {
  const messages = context.messages;
  let start = -1;
  const remove = new Set<number>();
  for (let end = 0; end < messages.length; end++) {
    if (messages[end].role !== "user") continue;
    if (start >= 0) {
      const prior = messages.slice(start + 1, end);
      const last = prior.findLast((message: any) => message.role === "assistant");
      if (last?.stopReason === "stop") {
        for (let i = start + 1; i < end; i++) {
          if (messages[i].role === "assistant") remove.add(i);
        }
      }
    }
    start = end;
  }
  if (!remove.size) return context;
  return { ...context, messages: messages.map((message: any, i: number) => remove.has(i)
    ? { ...message, content: message.content.filter((block: any) => block.type !== "thinking") }
    : message) };
}

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
        return streamSimple({ ...model, api: "openai-completions" },
          compaction ? messages : completedTurnContext(messages), {
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
            return compactToolPayload({ ...body, ...separateLimit, reasoning_effort: thinking ? "high" : "none",
              reasoning_budget_tokens: 0, chat_template_kwargs: {
              ...(body.chat_template_kwargs ?? {}), enable_thinking: thinking,
            } });
          } } : { onPayload: async (payload: any, info: any, signal: any) =>
            compactToolPayload(await options?.onPayload?.(payload, info, signal) ?? payload) }),
        });
      },
    });
    if (!await pi.setModel({ ...selected, api })) throw new Error("EVO bounded provider setup failed");
  });
}
