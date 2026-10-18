# Discord task waits and callbacks

- `wait({"seconds": 10})`: cancellable sleep, then the model checks the relevant tool again. No inference request while sleeping. Integer 1–300 seconds, **300 seconds cumulative per turn**, including concurrent calls. Prefer polling for work expected within a minute.
- `schedule({"action":"defer","after_seconds":600,"prompt":"Check job ABC and report its result."})`: save a one-shot continuation and finish the current turn. Integer 1–86,400 seconds; at most eight pending callbacks. Include the task, target and identifiers in the prompt.
- The scheduler checks every five seconds. A due callback waits for the current task to finish; it does not interrupt interactive work. The same channel conversation and model are reused when available. If its process was evicted or restarted, the saved task description starts a fresh conversation. No additional model or KV snapshot is allocated for a timer.
- Ordinary messages do not cancel pending timers. A due callback waits in the task queue if another message is being processed. `/stop`, `/new`, or `/model` cancels that channel's pending continuations; `/reset` cancels all continuations. Normal alarms and recurring briefings remain intact. Use `/alarms` or `schedule(action=list)` to inspect IDs; `pause`/`delete` revoke queued callbacks; `/stop` also interrupts running work. A short `wait` remains part of the active turn and can be interrupted by a new message; use `defer` when the follow-up must survive other messages.
- An unclaimed timer survives a bot restart. A callback already claimed or executing at restart is marked `interrupted_by_restart` and **not replayed**, because its actions might already have run. This deliberately favors avoiding duplicate actions over automatic retry.
- Continuations retain the tools authorized for the original owner task. Root commands still require a new Discord approval for each command. Existing recurring briefings keep their separate read-only tool restrictions and cannot create these continuations.
- Schedules are stored in the existing private `jobs.json`, with writes on state changes only. Completed callback records are discarded when another task is scheduled; paused callbacks are retained. Conversation contents remain subject to the existing RAM session lifetime.

## Reproduction

Ask the bot: “이 작업은 1분 정도 걸릴 테니 잠깐씩 기다렸다 상태를 확인해줘.” For a longer job: “10분 뒤 이 작업을 다시 확인해서 알려줘.” The model chooses `wait` or `schedule/defer` through their tool descriptions; no extra system prompt was added.

Isolated verification, no real Discord messages:

```sh
PYTHONPATH=/opt/evo-omp PYTHONDONTWRITEBYTECODE=1 \
  /opt/evo-omp/venv/bin/python /opt/evo-omp/verify_timing.py \
  --state /run/evo-timing-verification/state --live
```

Run as `evo-hermes` while the real bot is idle. The `--live` option uses the actual OMP binary and model API through the production queue/scheduler, captures Discord output, and restricts host tools to timing tools. Without `--live`, it exercises timing, cancellation, restart and queue contracts without inference. Evidence and source diff: `review/timing/`.
