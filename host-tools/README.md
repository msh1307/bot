# Host commands

`host_command(command, reason, cwd?, run_as?, timeout?)`: Discord owner only.

- Ordinary coding stays in bwrap; host commands use a separate Unix-socket broker.
- `run_as=root` needs fresh approval for the exact command. It gets real host root access; changes persist.
- One job at a time; 60s default, 120s maximum, 512MiB RAM, no swap. Children stop with the job.

Install broker/runner under `/usr/local/lib/evo-host/`; use `evo-host.service`.
Copy `policy.example.json` to private `/etc/evo-host/policy.json` and adapt locally.
Integration: [host.py](../omp-migration/evo_omp/integrations/host.py).

Windows Codex bridge sources are in `codex-bridge/`. They use the existing private server settings, token and certificate. `Update-DesktopBridge.ps1` updates an idle bridge without stopping the GUI. `Verify-Desktop-*.js` and `Verify-Snapshot-Tail.js` check protocol handling without sending live messages.

Install `text-file.js` beside the bridge. Authenticated file reads stream UTF-8 pages (200 lines / 12,000 characters, files up to 16MiB) without launching PowerShell. Check with `node codex-bridge/Verify-Text-File.js`.

The same authenticated Windows connection serves Codex, file reads and short commands. Add each machine to private `/etc/evo-omp/codex-remote.json` using [windows-servers.example.json](windows-servers.example.json). Each entry uses its own TLS certificate and token. The Windows listener still runs with its configured Codex/user permissions.

Temporary API/Web chat access uses `evo-access.py` under `/usr/local/lib/`. Its private access config supplies `public_url` and TLS settings. Keep installation credentials and certificates outside Git.
