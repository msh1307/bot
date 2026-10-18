# EVO owner Discord host tools

Normal OMP terminal and Python work keeps using the existing bwrap workspace.
The active integration is `../omp-migration/evo_omp/integrations/host.py`.
Unprivileged host commands execute directly. Every root host command requires
a fresh owner Discord approval.

## Downloads

The migrated bot uses ordinary sandbox bash for downloads. The legacy
`workspace_download` handler is not registered in the active OMP toolset.

## Host commands

`host_command(command, reason, cwd?, run_as?, timeout?)` validates an immutable
EVO command from the authenticated owner Discord session. The command is limited
to 1,000 characters.

The default account is `evo-hermes`. `run_as=root` explicitly selects administrator
execution and always opens an owner-only Allow Once / Deny card showing the full
command, working directory and reason. The card resolves its exact pending request
ID. Session/permanent grants and YOLO do not bypass this gate. Denial, timeout,
interruption and disconnect do not execute root commands. Ordinary host jobs
hide credential directories and control-service internals. An approved root job
has the real host view and normal root capabilities, including access to those
paths. Root approval authorizes that exact host command, not the next command.

Only the configured owner on Discord can execute host commands. The
handler requires a connected Discord client and a live gateway session. Other
users, Web UI, API, cron, interrupted turns and disconnected sessions are
rejected. Other tools' approval configuration and permissions are unchanged.

The root-managed local Unix socket broker runs that command in a
transient systemd service outside bwrap. This does not switch the normal terminal
or Python interpreter to a host backend. The service defaults to 60 seconds and
has a 120-second maximum, 512 MiB RAM maximum, zero swap, one CPU and 64 tasks.
The limit is a ceiling; the full 512 MiB is not reserved. Ordinary jobs use a
64 MiB private `/tmp`; approved root jobs use the host `/tmp` and `HOME=/root`.
Core dumps are disabled and command output is capped in RAM. Completion, timeout,
connection loss and broker restart remove background children. Only one host
command runs at a time; each subsequent root call asks again. No root password is
passed to the model. Unprivileged commands retain NoNewPrivileges, path masks,
read-only mounts and capability restrictions. Approved root jobs have none of
those restrictions and may use APT's normal `_apt` worker without
`APT::Sandbox::User=root`. They still have the resource/deadline limits above.
The broker itself remains hardened; PID 1 starts each job as a separate service.
Its `/tmp` view matches the host so approved working-directory inode validation
also works there. The normal OMP bwrap sandbox and WebUI tools are unchanged.

Use this tool carefully: it can modify host files or services. Its effects
are not undone when the process exits. Review script contents if the command
executes a file: the command and directory are pinned, while referenced files
remain ordinary mutable workspace files.

## Operations

- Broker: `evo-host.service`, socket `/run/evo-host/broker.sock`, no TCP listener.
- Policy: `/etc/evo-host/policy.json`, root only. The real host policy is not
  versioned. Start from `policy.example.json` and adjust private paths locally.
- Runtime audit: `/run/evo-host/audit.jsonl`, at most 128 metadata entries. No raw
  command, stdout or ticket token. RAM only; restart clears it.
- Installed source and checks: `/opt/evo-host-tools/`.
- Python/service code: `/usr/local/lib/evo-host/`.
- Discord integration: `../omp-migration/evo_omp/integrations/host.py`.
- Backup/rollback: `/var/backups/evo-host/<deployment timestamp>/rollback.py`.

Historical version 0.2.0 validation covers real systemd execution, permission and resource boundaries,
cleanup, public HTTPS download and real Hermes plugin discovery. Owner Discord
automatic execution and denial of other surfaces, stopped turns and disconnected
sessions use real Hermes session code with simulated identity/connectivity.
Version 0.3.0 source syntax and deployment hashes were checked; actual root execution
and live human approval were not tested during deployment. No human consent is
fabricated. No RDP connection or
Windows login is part of deployment.
