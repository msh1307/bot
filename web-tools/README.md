# Web tools

The existing EVO search/read/image module, with bounded search and reading responses.
OMP and WebUI share the router's `/tools` endpoint. No extra LLM or summarization service.

- Search: 5 results, 200-character snippets; whole reply at most 6,000 UTF-8 bytes. `next` returns more results.
- Read: 2,400 characters by default, at most 6,000; whole reply also capped at 4–10 KB.
- Web UI Python and shell return only output or errors, with an 8 KB head/tail limit. Full output needs a narrower rerun; interpreter variables persist.
- `query` finds matching passages, including when `action` is omitted. `links` lists navigation separately.
- Continue with `next_offset` and `version`; fetch options need not be repeated. Expired/changed snapshots fail explicitly.
- Source text stays in a 16-entry RAM cache. Reset, expiry or worker exit clears it. No new search disk cache.
- Default HTML rendering is preserved. `render=never` explicitly requests static HTML.

Measured with the installed Flash tokenizer: first responses for three long documents −35–51%, search responses −27–37%. An already short leaderboard changed by only −0.8%. These are public-fixture results, not a guarantee for every task. Reading an entire document still costs its full text plus pagination overhead. Context capacity and compaction thresholds are unchanged.

## Verification

Run with the installed worker environment and keep reports outside the repository:

```sh
python verify_search.py BASELINE.py evo_web.py /run/web-check
python verify_public.py BASELINE.py evo_web.py /run/web-check
python measure_tokens.py STRATA/tools PACK/tokenizer /run/web-check
python verify_live.py /run/web-check
python verify_model.py /run/web-check
```

`verify_live.py` follows OMP Registry → router → worker and WebUI HTTP. `verify_model.py` uses a private OMP/Flash session; it sends no Discord messages. The original source, large Unicode/URL fixtures, continuation, changed snapshots and public-address validation are checked. See [results](verification-20261026.json).

## Deployment

Live module: `/var/lib/evo-hermes/hermes/plugins/evo-web/__init__.py` (legacy path; Hermes remains stopped). `deploy.py CANDIDATE EXPECTED_LIVE_SHA256`, run as root, requires one confirmed idle OMP conversation. It backs that conversation up in tmpfs, replaces only the web module, restarts the web worker/bot and restores the conversation. Model processes stay running. WebUI clients with cached tool definitions should reload.
