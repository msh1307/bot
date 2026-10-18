# Host commands

`host_command(command, reason, cwd?, run_as?, timeout?)` runs through a local Unix
socket broker and a transient systemd service. Discord owner sessions only.
OMP's ordinary coding tools stay in bwrap.

- Default account: `evo-hermes`; protected paths are masked and privilege escalation
  is blocked. Downloads use ordinary sandbox bash.
- `run_as=root`: fresh Allow Once / Deny for the exact command, directory and reason.
  Approved execution gets the real host filesystem, host `/tmp`, `/root` and normal
  root capabilities. APT can use its normal `_apt` worker.
- Denial, expiry, interruption and disconnect block execution. No permanent or
  session-wide root grant; WebUI/API and cron cannot use this tool.
- One host job at a time. Default 60s, maximum 120s; 512MiB RAM, no swap, one CPU,
  64 tasks, bounded output. Background children are removed when the job ends.
- Approval pins the command and directory inode; referenced scripts remain mutable.
  Host changes persist after the job exits.

Integration: `../omp-migration/evo_omp/integrations/host.py`.
Broker: `evo-host.service`; socket `/run/evo-host/broker.sock`, no TCP listener.
Install broker/runner code under `/usr/local/lib/evo-host/`.
Adapt `policy.example.json` into private `/etc/evo-host/policy.json`.
Audit: `/run/evo-host/audit.jsonl`, at most 128 metadata entries, no command/output;
RAM only. Real policy and credentials are excluded from Git.
