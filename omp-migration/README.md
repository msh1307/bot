# OMP Discord bot

Sandbox coding, browser, Gmail, Codex, memory and timers through Discord.

- Requires the existing EVO services, OMP, bwrap, pinned dependencies and private credentials.
- Configuration: `config.yml`, `models.yml`, `instructions.txt`; service: `evo-omp.service`.
- Entry point: `python -m evo_omp.gateway`; status: `/run/evo-omp/status.json`.
- One active task; 2GiB RAM ceiling, no swap. Restart clears conversation context.
- `/compact` compresses context; `/new` clears it; `/reset` also clears shared workspace.
- Memory and recurring alarms persist. Root needs [per-command approval](../host-tools/README.md).
- Browser starts on demand and saves login state. bwrap provides isolation; Chromium's internal sandbox is disabled.

This targets an existing installation, not a standalone installer.
