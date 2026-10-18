# OMP Discord browser

## Runtime

The interactive `browser` host tool uses official **Chrome Headless Shell
155.0.8059.84**, controlled by Playwright 1.62.0 over a private pipe. There is
no listening DevTools port and no third-party page conversion service.

Production path:

`evo-omp.service → OmpSession.host_call → Registry.call("browser") → integrations/browser.py → bwrap → browser_worker.py → Headless Shell`

The previous Lightpanda MCP launcher and its node-ID/wait aliases are removed
from this tool. The separate WebUI `web_read` service is outside this replacement
and still depends on Lightpanda; do not uninstall that shared binary.

## Operation

One shared owner browser launches on the first page operation, then closes after
15 idle minutes. `status` does not launch it. Normal `close` preserves cookies,
localStorage and IndexedDB; `reset` closes it and deletes its dedicated profile.
It is independent of the shared coding workspace and conversation deletion.

- `open`, `tree`: title, visible controls and observed CSS selectors.
- `read`: visible text, default 6,000 characters, maximum 12,000 per response.
- `find`, `inspect`, `links`: inspect current controls, elements or links.
- `offset`: continue the same bounded RAM snapshot; actions invalidate it.
- `fill`, `click`, `press`, `select`, `check`, `scroll`, `wait`: normal page actions.
  `click` accepts either an observed selector or viewport `x,y` coordinates.
- `evaluate`: page JavaScript, available only in the live owner Discord context.
- `screenshot`: 960×640 viewport JPEG returned directly as an image tool result.
  Capture stays in RAM; production does not create a screenshot file. Tool-result
  images can remain in the current conversation's RAM history until it is reset,
  compacted or closed. Coordinates use pixels from the viewport's top-left.
- `status`, `close`, `reset`: browser lifetime and profile control.

Use `open` for HTTP(S) navigation. Other actions do not silently navigate when
given a URL. HTTP 4xx/5xx are explicit unsuccessful results. Timeouts and failed
submissions are not automatically replayed. Default action timeout is 15 seconds,
maximum 60; interruption tears down an unresponsive worker.

`open` waits for the page's `load` event by default, rather than exposing the
initial DOM before scripts load. `state=domcontentloaded` remains an explicit
faster option. Neither event guarantees every SPA has finished hydration.
`wait duration_ms` is an explicit pause capped at five seconds; `timeout_ms`
sets a deadline and does not itself request a pause. Element selectors prefer
unique public IDs/names/labels/roles before long structural CSS paths.

Image requests are blocked by default to reduce memory. `open images=true` loads
page images for that navigation when visual content is needed. The next `open`
defaults to blocking images again. Media remains blocked. JavaScript, CSS and
fonts remain available. Screenshots render the page; blocked site images will
be absent. Downloads are not accepted. Browser extensions are not supported by
Headless Shell. There is no separate CAPTCHA solver or automatic retry loop;
the model chooses its ordinary browser operations within existing session limits.

## Profile and isolation

- Persistent profile: `/var/lib/evo-omp/browser/chromium`.
- Session-cookie persistence: `/var/lib/evo-omp/browser/session-cookies.json`, 0600.
- Profile directory: 0700, owned by `evo-hermes`.
- Browser-only `/tmp` and `/dev/shm`: private tmpfs.
- Browser mounts exclude host homes, SSH keys, Discord credentials and workspace.
- Network is shared so the browser can visit the internet and authorized LAN URLs.
- Scheduled runs can navigate/read/take screenshots but cannot fill, click or
  evaluate scripts. The live tool requires the configured owner Discord identity.

**Chromium's internal sandbox is disabled in this deployment.** Ubuntu's
`unpriv_bwrap` AppArmor profile denies the nested SYS_ADMIN operation required
by Chromium. The outer bwrap mount/PID/IPC/user isolation remains active. Host
AppArmor, capabilities and user-namespace sysctls were not relaxed. This is a
material limitation; it is not equivalent to Chromium's internal process sandbox.

The service retains its 2GiB shared limit, 1.5GiB high watermark and zero swap
allowance. TasksMax is 256 for renderer threads. This budget includes the gateway,
OMP processes and browser; it does not include the separate inference service.

An additional real Flash vision check passed three canvas screenshots plus one
canvas screenshot with the real Gemini app still loaded underneath. Minimum host
MemAvailable was 7.839GiB; additional swap-out and OOM events were zero. OMP,
browser and test controller peak PSS was 908.76MiB in the Gemini case, excluding
the separate inference service. The CPU image encoder was observed starting and
unloading afterward. This is a short single-tab test, not a full 128K-context or
high-resolution photo capacity guarantee. See the report's vision-memory section
and `verify_browser_vision.py` for reproduction. The 2GiB limit remains unchanged.

Cookie/profile persistence performs normal filesystem writes. Disabling the
disk cache and using RAM temporaries does not make the browser write-free.

## Installation and reproduction

On EVO, as root:

```sh
python3 /opt/evo-omp/install_browser_runtime.py
```

The installer pins the official CfT archive, records its URL, reported version
and locally computed SHA-256 in `browser-runtime/installation.json`, and installs
the pinned Playwright dependency. The digest is not an independently published
Chrome checksum. Headless Shell does not update itself.

The accompanying verification scripts use isolated `/run` profiles. Run them
as `evo-hermes`, with `OUT` set to a dedicated writable directory, in a constrained
transient systemd unit. Exact commands, actual outputs, production deployment
checks and limitations are recorded in
`review/browser-shell-migration/REPORT.md`.

Never package the profile or cookie files with test results. `--owner-cookies`
copies existing authentication privately into the isolated profile; it never
prints cookie values. Real Gemini tests submit a short arithmetic question to
the external Gemini service.

Camoufox is not an automatic fallback. A Headless Shell block alone does not
establish a fingerprint cause or demonstrate that Camoufox succeeds. See the
same-IP access comparison in the report.
