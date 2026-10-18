# OMP Discord bot

Owner-only Discord gateway using official Oh My Pi 18.11.3. OMP runs the model
and coding loop; this code adds Discord, integrations, scheduling and approvals.

## Layout

- `evo_omp/gateway.py`: Discord, interruption, queue and scheduling.
- `evo_omp/rpc.py`: OMP process and bwrap sandbox.
- `evo_omp/tools.py`, `integrations/`: memory, timers, web, browser, Gmail and Codex.
- `config.yml`, `models.yml`, `instructions.txt`: model/tool configuration.
- `turn-limit.ts`: 40 provider dispatches per turn; turn deadline is 20 minutes.
- `*.service`: service definitions. `stage.py` and `cutover.py` are migration helpers.

## Behavior

- One active task, two RAM conversations retained by LRU. New owner messages
  interrupt the current turn in that channel. Automatic compression and `/compact`.
- `/new` clears context. `/reset` also clears shared workspace, preserving Git
  home, durable memory and recurring alarms. Restart clears RAM conversations.
- Memory is explicit document list/read/write/delete. Skills and automatic profile
  injection are disabled. Gmail is read-only; Codex controls an existing Windows bridge.
- Coding runs in shared `/workspace` inside bwrap with network access.
  [Host commands](../host-tools/README.md) use a separate broker and root approval.
- Bot + OMP + browser share a 2GiB service ceiling, 1.5GiB high watermark, zero
  swap and 256 tasks. Inference is a separate service.

## Browser

Chrome Headless Shell 155.0.8059.84 + Playwright 1.62.0, launched on demand over
a private pipe. DOM navigation/actions, JavaScript and RAM screenshots (960x640).
Images default off (`open images=true` enables them); media and downloads are blocked.
Read output defaults to 6,000 chars, maximum 12,000; use offset to continue.
Action timeout defaults to 15s, maximum 60s. Browser wait pauses at most 5s.

One persistent profile under `/var/lib/evo-omp/browser`. Close after 15 idle minutes;
`close` preserves login state, browser `reset` deletes it. Cookie persistence writes
to disk. Outer bwrap isolation is active; Chromium's internal sandbox is disabled
because of the host AppArmor restriction. No automatic Camoufox fallback.
Recurring jobs can browse/read/screenshot but cannot click, fill or evaluate.
The separate WebUI reader still uses Lightpanda.

## Timers

- `wait(seconds)`: interruptible, 1-300s, cumulative 300s per turn; no inference
  while asleep. `schedule(action=defer, after_seconds, prompt)` saves a continuation
  for 1s-24h later, at most eight pending. Include task/target IDs in its prompt.
- Due jobs queue behind active work; the scheduler checks every 5s. A saved prompt
  resumes in the retained conversation, or a fresh one after eviction/restart.
- Ordinary messages retain timers. `/stop`, `/new`, `/model` cancel that channel's
  continuations; `/reset` cancels all continuations. Recurring alarms remain.
- Unclaimed callbacks survive restart; already running callbacks are not replayed.
  Root always needs fresh approval. Recurring briefings use web/browser/status only.
- `/alarms` lists jobs; pause/delete revoke pending callbacks. Jobs persist privately
  in `jobs.json`; no extra KV snapshot is allocated for a timer.

## Deployment

Requires bwrap, the pinned Python dependencies, official OMP binary, Headless Shell,
model router, private Discord configuration and existing service accounts/paths.
Search and images use the existing EVO HTTP tool service; Web Python and Codex
need their separate bridges. This folder is not a fresh-machine installer.
Browser installer: `python3 /opt/evo-omp/install_browser_runtime.py` (root).
It records a local download digest; this is not an independently verified checksum.

Services: `evo-omp`, `evo-host`, `evo-web-python`.
Status: `/run/evo-omp/status.json`. Let active work finish before restarting.
`verify*.py` contains checks; some contact models/accounts, so inspect before running.
[Inference patches](../runtime/README.md) are maintained separately.
