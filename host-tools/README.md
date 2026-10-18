# Host commands

`host_command(command, reason, cwd?, run_as?, timeout?)`: Discord owner only.

- Ordinary coding stays in bwrap; host commands use a separate Unix-socket broker.
- `run_as=root` needs fresh approval for the exact command. It gets real host root access; changes persist.
- One job at a time; 60s default, 120s maximum, 512MiB RAM, no swap. Children stop with the job.

Install broker/runner under `/usr/local/lib/evo-host/`; use `evo-host.service`.
Copy `policy.example.json` to private `/etc/evo-host/policy.json` and adapt locally.
Integration: [host.py](../omp-migration/evo_omp/integrations/host.py).
