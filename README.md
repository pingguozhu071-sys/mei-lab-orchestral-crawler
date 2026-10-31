# mei-lab-orchestral-crawler

[简体中文](README.zh-CN.md) ｜ English ｜ [日本語](README.ja-JP.md)

An experimental web collection engine. One run takes a set of URLs through
sniff → route → fetch → extract → deduplicate → store, and writes the result to
local disk.

Design rationale in depth: [Technical Whitepaper](docs/WHITEPAPER.md) ·
[Architecture](docs/ARCHITECTURE.md)

CLI + embeddable library · Python 3.12+ · v0.1.0 · Runs on Windows / Linux / macOS

---

## At a glance

<!-- AUTO:NUMBERS -->

| Metric | Value |
|---|---|
| Package modules | 33 |
| Package lines | 11119 |
| Test files | 11 |
| Test lines | 1905 |
| Test functions | 140 |
| Site rules | 8 |

<!-- /AUTO:NUMBERS -->

| | |
|---|---|
| Channels | HTTP (curl_cffi, browser-grade TLS fingerprint) and Browser (one Chromium per worker) |
| Extraction | HTML (density heuristic), JSON API, JSON-LD, `__NEXT_DATA__`, `window.__NUXT__`, PDF |
| Media | Generic yt-dlp channel, m3u8 with AES-128, five platform resolvers |
| Anti-detection | 21 self-consistent device profiles, fingerprint injection, network guard, behaviour simulation |
| Queue | SQLite WAL, lease + compare-and-swap, idempotent enqueue, resumable |
| Safety | Hop-by-hop SSRF gate (26 adversarial regression samples), three-layer redaction |
| Site rules | Declarative YAML — add a site without touching code |
| Release gate | 6 checks, static baseline can only decrease |

## Fetching model

The engine is built around one rule: **use the cheapest channel that works**.

A static heuristic inspects the URL offline and decides which channel a task
deserves. When a page looks like plain HTML it goes to the HTTP channel; when the
URL pattern or content suggests JavaScript rendering it goes to the browser. The
heuristic is deliberately allowed to be wrong in the cheap direction: if the HTTP
channel returns a shell page whose extracted text is nearly empty, the task is
requeued and retried once through the browser channel.

Measured on `https://quotes.toscrape.com/js/`:

| Channel | Extracted text |
|---|---|
| HTTP | 29 characters (navigation only) |
| Browser (after escalation) | 1,429 characters |

The escalation retry does not consume the `--max` page budget — it is the same
task taking a second channel, not a new page.

## Architecture

```
   seed URLs
       │
       ▼
 ┌───────────────┐   claim (lease + CAS)   ┌──────────────────┐
 │ SQLite queue  │ ──────────────────────► │  LayeredRouter   │
 │ tasks/media   │ ◄────────────────────── │  (offline sniff) │
 └───────────────┘   result / requeue      └────┬────────┬────┘
                                                 │        │
                                    HTTP worker  │        │  browser worker
                                 (curl_cffi)     ▼        ▼  (own Chromium)
                                          ┌─────────────────────────┐
                                          │   ExtractionPipeline    │
                                          │ HTML / JSON / PDF / media│
                                          └───────────┬─────────────┘
                                                      ▼
                                     ┌────────────────────────────────┐
                                     │ Vault (SQLite) → Export        │
                                     │ SHA-256 delta · SimHash · AES  │
                                     │ JSONL · CSV · MD · XLSX · SQLite│
                                     └────────────────────────────────┘
```

Module-level detail, the page-processing sequence and the database schema are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
# pip install -e ".[dev]"                              # Linux / macOS

patchright install chromium                             # browser channel
pip install -e ".[pdf]"                                 # optional PDF support
```

Browser binaries are not installed by pip. If you skip that step the browser
channel is disabled and dynamic pages fall into the dead-letter queue; every
other channel keeps working.

```bash
mel crawl --url https://quotes.toscrape.com/ --max 20
mel shell                       # paste a link, press Enter
mel stats
mel export --format markdown --output ./data
```

Or as a library:

```python
from meilab.engine import Engine, EngineConfig

engine = Engine(EngineConfig(output_dir="data", max_per_domain=10))
try:
    print(engine.crawl(["https://quotes.toscrape.com/"], max_tasks=20).as_dict())
finally:
    engine.close()
```

## Commands

| Command | Purpose |
|---|---|
| `mel crawl --url URL [--max N]` | Collect pages; honours robots.txt and Crawl-delay by default |
| `mel shell` | Interactive window: paste a link, press Enter |
| `mel resume [--max N]` | Continue the pending tasks in the queue |
| `mel stats` | Task / episode / vault statistics with channel split |
| `mel export --format <fmt> --output DIR` | Export: `jsonl` `csv` `markdown` `xlsx` `sqlite` (FTS5) |
| `mel media --url URL [--audio] [--cookies F]` | Download media: platform resolver → generic yt-dlp → m3u8 |
| `mel resolve --url URL` | Resolve media streams without downloading |
| `mel comments --url URL [--cookies F]` | Bilibili comments |
| `mel rule validate <file>` | Validate a site rule |
| `mel rule test <file> --url URL` | Run a rule against a live page |

## Measurements

Windows 11, Python 3.14.6, single machine.

```
mel crawl --url https://quotes.toscrape.com/ --max 5
  queued 21 · dispatched 5 · succeeded 5 · failed 0 · 22.5 s

mel crawl --url https://quotes.toscrape.com/js/ --max 3
  channels: http 3 · browser 3
  /js/ page: http 0 chars → escalate → browser 1,429 chars

mel resolve --url https://www.bilibili.com/video/BV1Dvpg6BEt5/
  bilibili native API · 720P · mp4 · 453,914,132 bytes · duration 6,972 s
  CDN Range support: 206 Partial Content

fingerprint_probe.py
  10 checks · 0 flagged
```

The fingerprint probe runs the production worker, device library and injection
chain against a local page and reports each signal that bot-detection pages
typically flag: `navigator.webdriver`, `cdc_*` residue, `window.chrome.runtime`,
`navigator.platform`, `languages`, `hardwareConcurrency`, `maxTouchPoints`,
plugin count, masked and unmasked WebGL values.

## Honest limits

These are measured on this machine, not inferred.

1. **Patchright 1.61.2 does not execute `add_init_script`, and does not honour
   HTML rewritten through `route.fulfill`.** The same code works under stock
   Playwright. Fingerprint injection therefore happens *after* load, not before
   the first script. The engine probes this at startup and degrades explicitly
   instead of assuming.
2. **Bilibili comments cannot be read anonymously.** The current endpoint needs a
   WBI signature plus a logged-in session; browser cookies alone still return
   `-403`. `mel comments` reports the platform reason and accepts `--cookies`.
3. **Douyin, Xiaohongshu, Kuaishou and NetEase Cloud are handed to yt-dlp.**
   Their direct-link signatures change without notice, so a hand-written signer
   is a maintenance liability. Resolvers fall back to the generic channel rather
   than failing outright.
4. **No GUI and no installer.** This is a CLI and a library. `mel shell` is the
   interactive front end.
5. **`news.ycombinator.com` and `bot.sannysoft.com` are unreachable from this
   network** (TCP timeout), so smoke tests use other sites and the local
   fingerprint probe replaces the external fingerprint check.
6. **Site rules cover three sites** (Bilibili, Zhihu, plus a template). The
   mechanism is complete; the rule library is not.
7. **Coverage is reported, not enforced.** Accident regressions say more about
   quality here than line coverage does.
8. **Seven files exceed 300 lines.** They are frozen; new capability goes into new
   modules rather than deeper into them.

## Safety boundaries

- `robots.txt` is fetched and parsed before crawling; `Disallow` and `Crawl-delay`
  are both honoured.
- Global minimum delay 1.5 s with jitter, per-domain concurrency cap, and
  `Retry-After` backoff with domain cooldown.
- Only public pages. Login walls, paywalls and CAPTCHAs are not bypassed. Link
  discovery skips `/login`, `/signup`, `/register`, `/account`.
- Every outbound request — including each redirect hop — passes a scheme
  whitelist and a post-DNS address check. Loopback, private, link-local and
  cloud-metadata addresses are refused.
- Phone numbers, e-mail addresses, IPs and 19 classes of sensitive URL parameters
  are redacted before anything is written to disk. Queue URLs are deliberately
  **not** redacted: they carry the signature the next request needs.

## Project layout

```
meilab/
  core/      types, SQLite queue (state machine, lease, CAS, migrations)
  router/    sniff, layered router, RSS/sitemap seeding
  workers/   base (process skeleton + SSRF), http, browser, pool
  extract/   html, jsondata, pdf, pipeline, site_rules
  stealth/   profiles, inject, behavior, netguard, captcha
  media/     ytdlp_channel, m3u8_downloader, resolvers, wbi, cookies, downloader
  privacy/   sanitizer
  storage/   vault, export, quality, observability
  engine.py  main loop
  cli.py     command line
rules/sites/ declarative site rules
tools/       release_check, doc_numbers
tests/       10 files, 123 test functions
```

## Development

```bash
python -m pytest tests/ -v        # 178 cases, offline
python tools/release_check.py     # 6 checks, any failure blocks release
python tools/doc_numbers.py --check
```

The release gate verifies version consistency, a clean working tree, the full
test suite, a static-analysis baseline that may only decrease, dependency
vulnerabilities and leaked secrets. A check that cannot reach a conclusion is
marked skipped, never passed.

## Related project

[kiana-crawler](https://github.com/pingguozhu071-sys/kiana-crawler) is the other
collection engine from this work. It is a Windows desktop product — asyncio core,
PySide6 window, 622 MB installer, four platform resolvers, LLM enrichment. This
project solves the same problem differently: multiprocessing instead of asyncio,
CLI instead of GUI, a narrower dependency set.

No code is shared between them. What did carry over is engineering practice —
lease + CAS queue semantics, hop-by-hop SSRF checking, three-layer redaction, and
a list of failure modes worth avoiding. Where this project learned something the
hard way, it is written down in
[docs/ENGINEERING-LESSONS.md](docs/ENGINEERING-LESSONS.md).

## License

MIT. See [LICENSE](LICENSE).
