# OMP Discord bot

Sandbox coding, browser, Gmail, Codex, memory and timers through Discord.

- Requires the existing EVO services, OMP, bwrap, pinned dependencies and private credentials.
- Configuration: `config.yml`, `models.yml`, `instructions.txt`; service: `evo-omp.service`.
- Hermes is retired and masked. The `evo-hermes` Unix account and workspace name are reused by OMP; do not start the old service.
- Entry point: `python -m evo_omp.gateway`; status: `/run/evo-omp/status.json`.
- One active task; 2GiB RAM ceiling, no swap. Restart clears conversation context.
- `/compact` compresses context; `/new` clears it; `/reset` also clears shared workspace.
- `/ignore` toggles note mode for the current channel: ordinary text and attachments never reach the model. Repeat to resume, or use `/ignore on|off|status`. The setting persists; explicit slash commands and scheduled jobs still work.
- Memory and recurring alarms persist. Root needs [per-command approval](../host-tools/README.md).
- Scheduled Codex checks can list/read chats, answer ordinary pending questions and continue existing work. One cron job can include an end time, quiet intermediate runs and a short next-run note. Execution approvals remain with the owner.
- Owner Discord `codex_task`: `file_list`/`file_read` access Windows reports and source; `exec` runs short argv commands with the Codex server's configured sandbox. Uses the existing authenticated connection, without SSH or a model turn.
- Existing Codex chats: `read` returns a recent tail, `send` asks or replies, `answer` answers a pending GUI question. GUI `send`/`answer` can opt into `wake: true` for EVO callbacks; default off. Execution approvals remain with the owner.
- `user_input`: Flush / Skip buttons or automatic reply collection (5-minute idle timeout, extended by typing; 15 seconds quiet after replies and inferred typing expiry). Skip/timeout grants no permission; unclear tasks stop. No inference while waiting. `/stop` cancels it.
- Browser starts on demand and saves login state. bwrap provides isolation; Chromium's internal sandbox is disabled.
- OMP 18.11.3 validates summaries and reconciles history plus unfinished work into one handoff. Recent user requests are copied separately (up to 8, within 2048 tokens); corrections override stale interpretations. [Patch](patches/compaction.patch), [build details](runtime-build.json).
- Flash compaction keeps thinking enabled, caps the final handoff at 8192 tokens and reports progress every 30 seconds. Replies end with used context, e.g. `[context 80%]`.
- Automatic maintenance starts at 60,000 tokens: remove old tool output first, then summarize if needed. New messages interrupt once and preserve a reusable conversation.
- Both models use the Qwen tokenizer for context and summary budgets.
- Long host/Codex/browser/mail outputs show a small preview; exact returned text is available to native `read`/`grep` in the session's RAM files (64 files / 16MiB maximum). Reset/close clears them; the existing RAM maintenance handoff preserves them. Command status and images are retained. No extra model call or tool schema.
- Shell output keeps 1KiB head + 1KiB tail, with the full capture in RAM (existing 4MiB cap). The [native patch](patches/shell-output-budget.patch) wires the configured budget into the streaming collector. Other native results spill above 12KiB; search/read pagination stays separate.
- EVO requests omit reasoning from successfully completed earlier turns. Current tool-loop and unfinished reasoning stay; user requests, answers, tool results and the RAM transcript remain. This can replay part of the latest prefix. [Checks and token counts](context-budget-verification.json).
- Native compaction counts and summarizes that same completed-turn view (`EVO_COMPLETED_REASONING_CONTEXT=1`); current/unfinished thinking and original RAM history remain. Threshold/capacity stay unchanged. [Regression checks](compaction-accounting-verification.json).
- Native search descriptions only mention active task tools. [Checks](tool-inventory-verification.json).
- Tool descriptions use reviewed short prose; types, required fields and limits stay intact. Full current specs live in RAM at `/home/agent/tool-help/TOOL_NAME.json` for native `read`. Changed schemas or failed help writes keep the original inline spec. [Checks and token tradeoff](tool-schema-verification.json).
- [Measurements](output-budget-verification.json). `verify_shell_output.py BINARY REPORT_DIR` checks actual OMP/bwrap output, status and recovery; `verify_shell_model.py` checks Flash follow-up reading. Apply patches in `runtime-build.json` order. No Discord posts.

This targets an existing installation, not a standalone installer.
