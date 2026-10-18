# EVO OMP Discord bot

A small owner-only Discord gateway around Oh My Pi 18.11.3. OMP owns the model
and coding-tool loop; this project owns Discord, task scheduling, approvals and
the existing EVO integrations. The inference server and WebUI remain separate.

## Implemented

| Feature | Current behavior |
|---|---|
| Coding | OMP read/grep/glob/edit/write/bash inside bwrap; shared workspace and network |
| Interruption | A new owner message interrupts work in that channel and continues the conversation |
| Context | RAM conversations, two channels by LRU, automatic compression and `/compact` |
| Memory | Explicit document list/read/write/delete; no automatic profile injection or review |
| Reminders | Natural-language tool calls, one-shot reminders and recurring cron briefings |
| Follow-up | Interruptible short waits or a saved one-shot continuation; queue behind active work |
| Browser | Lazy Chrome Headless Shell, persistent login profile, DOM actions and screenshots |
| Integrations | Read-only Gmail, Windows Codex tasks/chats, sensors and timed API/Web access |
| Host commands | Owner Discord only; ordinary host commands remain restricted |
| Root | Fresh Allow Once for the exact command; normal host root rights for that command |

Daily reminders already work without generating a skill. A scheduled job stores
its task prompt, schedule and destination in `jobs.json`; it survives restart.
Recurring briefings deliberately have only web/browser/status tools. They cannot
use Gmail, run host commands or perform unattended root maintenance. One-shot
task continuations keep ordinary task tools, with a new approval for every root
command. Pending callbacks and recurring jobs have different stop/reset rules;
see [timing behavior](OMP-TIMING.md).

Skill discovery and automatic skill generation are disabled. Memory documents
are read only when relevant and updated when the owner asks. An explicitly saved
procedure can be read as a document; it is not an automatically loaded skill.

## Boundaries

- Native coding tools always stay in bwrap. `host_command` starts a separate
  host job; approving root does not switch the ongoing shell to root or disable
  bwrap for later tool calls.
- Root approval is per command, owner Discord only. No session-wide root grant,
  WebUI/API root tool, cron root command or automatic root consent exists.
- Root gets the actual host filesystem, `/root`, host `/tmp` and normal root
  capabilities. Ordinary jobs mask credentials and control state, including the
  current OMP paths. Passwords/tokens remain in private files outside workspace.
- Each host job retains a 512 MiB RAM ceiling, no swap and at most 120 seconds.
  Large builds/package installations may need a different execution budget.
- The bot has a 2 GiB service limit with swap disabled. The model is a separate
  service. Browser/login state and explicit memory/schedules persist; chat
  context is transient.
- `/new` clears conversation context; `/reset` also clears shared workspace,
  preserving Git home, durable documents and recurring alarms.

## Source layout

- `evo_omp/`: active gateway, OMP transport, integrations, browser and Python bridge.
- `turn-limit.ts`: provider retry ownership and 40-dispatch bound.
- `config.yml`, `models.yml`, `instructions.txt`: model metadata and short instructions.
- `evo-omp.service`, `evo-web-python.service`: runtime service definitions.
- `../host-tools/{evo-host-broker.py,command-runner.py,evo-host.service,policy.example.json}`:
  host execution and its ordinary/root permission split. Keep these with the bot source.
- `stage.py`, `cutover.py`, `install_browser_runtime.py`: this EVO installation's
  preparation/migration helpers, not a general fresh-machine installer.

## Git scope

Version-control the active source, configuration templates, service definitions,
dependency pins and operation documents. Keep the real `/etc/evo-omp` credentials,
SSH keys, browser profile/cookies, `jobs.json`, personal memory, workspace files,
network maps, captured conversations, old server snapshots, archives and downloaded runtimes
out of the repository. `.gitignore` provides common exclusions; it does not make
the entire surrounding EVO experiment workspace safe to publish.

The parent repository uses an allowlist for these source folders; the surrounding
infrastructure notes and experiments are excluded. The real host `policy.json`
is also excluded: adapt `policy.example.json` privately for the destination host.
Check the staged diff before every commit. Account IDs currently embedded in the
source are deployment-specific, non-secret values; this is not a generic bot package.

## Deployment dependencies and limitations

This source targets the already configured EVO host. It needs OMP's official
binary, bwrap, the pinned Python dependencies, Headless Shell, a model API/router,
private Discord configuration and the existing service accounts/directories.
Search/page/image tools use the separate EVO HTTP tool service. Web Python loads
`/opt/evo-python/bridge.py`; Codex and external access use their existing bridges.
Those service runtimes/credentials are not created simply by cloning this folder.
Retired Hermes agent code is not imported by the active bot; the frozen public
reader/Python dependencies still use legacy paths.

Operation: `systemctl status evo-omp evo-host`; bot status is
`/run/evo-omp/status.json`. Stop the active task before any restart. Restarting
the bot clears RAM conversations. Historical checks do not establish long-term
reliability, model quality or every login site's compatibility.
