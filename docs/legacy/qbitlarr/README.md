# qBitlarr

**Language:** English | [中文](README.zh-CN.md) | [Français](README.fr.md)

**A Prowlarr → qBittorrent bridge with REST, MCP, and CLI support.**

For people who already run Plex, Jellyfin, or Emby and want a lightweight way to let friends, family, or an LLM agent request movies and TV — without giving them qBittorrent access and without running the full Sonarr + Radarr stack.

qBitlarr is one small FastAPI service that:

- Takes a natural-language title, an IMDb ID, or an IMDb / Douban / AlloCine link.
- Identifies the title, then searches your Prowlarr indexers.
- Ranks releases with opinionated, configurable quality preferences and shows you the top few to pick from.
- Queues your pick in your existing qBittorrent (or auto-picks the best one in `auto` mode).
- Exposes everything as REST, MCP, and a small CLI so it drops into Claude Desktop, Cursor, ChatGPT custom tools, Telegram bots, shell scripts, cron jobs, or your own agent.

Works with any HTTP client, Claude/Cursor/ChatGPT via MCP, or the `qbitlarr` CLI.

## Architecture

![qBitlarr architecture: a friend, family member, shell script, or LLM agent talks REST, MCP, or CLI to the qbitlarr FastAPI service, which uses Prowlarr and FlareSolverr to search torrent indexers and then drives your own qBittorrent Web UI, which saves files into your Plex/Jellyfin/Emby library.](docs/architecture.png)

Editable source for the REST / MCP / CLI diagram: [docs/architecture.svg](docs/architecture.svg).

## What Runs In Docker Compose

- `qbitlarr` — the FastAPI service on `http://localhost:8000`
- `prowlarr` — bundled Prowlarr on `http://localhost:9696`
- `flaresolverr` — bundled FlareSolverr on `http://localhost:8191`

qBittorrent is **not** bundled. Point qBitlarr at any existing qBittorrent — desktop, NAS, seedbox, separate container — via `QBIT_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD`.

## qBittorrent Setup

qBitlarr needs an existing qBittorrent install because everyone saves media in different places: a desktop app, a NAS, a seedbox, or a separate container. qBitlarr only talks to qBittorrent through its Web UI API.

Before starting qBitlarr:

1. Install qBittorrent wherever your downloads should run.
2. In qBittorrent, open **Preferences / Options → Web UI** and enable the Web User Interface.
3. Set or confirm the Web UI username and password.
4. Put those values in `.env`:

```sh
QBIT_URL=http://host.docker.internal:8080
QBIT_USERNAME=your-webui-username
QBIT_PASSWORD=your-webui-password
```

Use `http://host.docker.internal:8080` when qBittorrent runs on the same machine as Docker Compose. If qBittorrent runs on a NAS, seedbox, or another computer, use that machine's LAN URL instead, such as `http://192.168.1.50:8080`. Do not use `localhost` in `.env` for a host-installed qBittorrent; from inside Docker, `localhost` means the qBitlarr container itself.

## Quick Start

```sh
cp .env.example .env
# edit .env: set QBIT_URL, QBIT_USERNAME, QBIT_PASSWORD from your qBittorrent Web UI

# 1. Start Prowlarr first so you can grab its API key
docker compose up -d prowlarr flaresolverr

# 2. Open http://localhost:9696, finish first-run setup, add indexers,
#    then copy the API key from Settings -> General -> Security
# 3. Put the key in .env as PROWLARR_API_KEY

# 4. Start the rest
docker compose up -d --build

# 5. Try it
curl -X POST http://localhost:8000/handle \
  -H 'Content-Type: application/json' \
  -d '{"user_message":"tt0045877"}'
```

For a dependency check that also pings Prowlarr and qBittorrent:

```sh
curl 'http://localhost:8000/health?deep=true'
```

## What It Feels Like

Once qBitlarr is wired up to your agent (or you're using the CLI), you talk to it the way you'd talk to a friend who knows your media setup:

For movie requests, qBitlarr accepts IMDb links and IDs directly, and it can also resolve supported Douban movie links or IDs and AlloCine film links or IDs into the same IMDb-based flow. If a Douban or AlloCine movie cannot be resolved safely, qBitlarr asks for IMDb instead rather than guessing.

The examples below use [The Hitch-Hiker (1953)](https://www.imdb.com/title/tt0045877/), a public-domain film listed by the Library of Congress in its [Public Domain Films from the National Film Registry](https://www.loc.gov/free-to-use/public-domain-films-from-the-national-film-registry/) set. Rights can still vary by jurisdiction and by specific restoration, soundtrack, subtitles, or edition.

<table>
  <tr>
    <td width="42.5%" align="center" valign="middle">
      <img src="docs/screenshots/telegram-imdb-release-picker.jpg" height="430" alt="Telegram example: sharing the IMDb page for The Hitch-Hiker, choosing a release, and watching qBitlarr download progress.">
    </td>
    <td width="57.5%" align="center" valign="middle">
      <img src="docs/screenshots/telegram-title-release-picker.jpg" height="430" alt="Telegram example: searching by title, selecting the right movie, choosing a release, and watching qBitlarr download progress.">
    </td>
  </tr>
</table>

*Screenshots for reference only. Left: an IMDb link or ID goes directly to release choices and download progress. Right: the title search `The Hitchhiker 1953` returns four title choices first; selecting The Hitch-Hiker (1953) opens the same release picker and progress view. The demo title is a Public Domain example; rights can vary by jurisdiction and by specific restoration, soundtrack, subtitles, or edition.*

> **You:** *Download The Hitchhiker 1953.*
> **Agent:** Which title do you mean?
>   1. The Hitchhiker's Guide to the Galaxy (2005)
>   2. The Hitch-Hiker (1953)
>   3. An American Hippie in Israel (1972)
>   4. The Hitch Hiker (2004)

> **You:** *Tap The Hitch-Hiker (1953).*
> **Agent:** Choose the version to download:
>   1. WEB-DL · H.264 · 5.3 GB
>   2. 1.2 GB option
>   3. WEB-DL · H.264 · 5.3 GB
>   4. BluRay · H.264 · 5 GB
>   5. BluRay · H.264 · 8.7 GB

> **You:** *Tap the 1.2 GB release.*
> **Agent:** Got it, I'll add the download.
> **Agent:** ⬇️ The Hitch-Hiker (1953) [1080p]<br>
> 🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜ 36%<br>
> 💾 421.9 MB / 1.1 GB<br>
> ⚡ Speed: 558.4 KB/s<br>
> ⏱️ ETA: 5m 24s

> **You:** *Download tt0045877*
> **Agent:** Because the IMDb ID already identifies the film, qBitlarr skips the title picker and opens the release buttons directly.

Behind the scenes, every request resolves to one title first: an IMDb / Douban / AlloCine link or ID locks the title directly, while a plain keyword is matched against Wikidata. If several titles match, qBitlarr returns title choices that chat adapters can render as buttons; selecting one continues to the release picker. If nothing matches, qBitlarr asks for an IMDb link rather than guessing. Once the title is fixed it ranks releases and returns the top few choices as structured buttons and tables; in `auto` mode it queues the best one outright. Say *"4K"*, *"Remux"*, or *"720p HEVC"* any time to override the default quality. Status comes back as raw data (`qbitlarr_list_downloads` / `qbitlarr_get_download_status`) or as chat-ready emoji progress cards (`qbitlarr_render_*`); see [Connect To An Agent](#connect-to-an-agent) for the refresh and completion-notification details.

### Pro tip: share straight from the movie app or website

The fastest way to use qBitlarr is to skip typing the title:

1. In IMDb, Douban, AlloCine, or any site that shares one of those supported movie URLs, find what you want.
2. Tap the share icon → pick the chat app where your agent lives (Telegram, WhatsApp, Discord, Signal, iMessage, etc.).
3. The agent receives a URL like `https://www.imdb.com/title/tt0045877/`, resolves it to the canonical IMDb flow, and auto-identifies the title — no typing, no spelling traps, no ambiguity.

A raw IMDb ID like `tt0045877` works the same way if you have one handy. qBitlarr also accepts `douban:1292052` and `allocine:25801` for supported movie IDs. It resolves those IDs and jumps straight to the ranked release choices for that exact title — no title-matching step.

## When To Use This vs Sonarr / Radarr

Use **Sonarr/Radarr** if you want a library manager: episode tracking, upgrade policies, automatic monitoring of new releases, quality profiles with 30 knobs.

Use **qBitlarr** if you just want: *"a friend says a movie name → it appears on Plex an hour later."* No library, no monitoring, no profile UI. One service, four env vars for preferences, done.

## Responsible Use

qBitlarr is an automation bridge. It does not provide content, indexers, trackers, or legal advice. Use it only with indexers and media you are allowed to access in your jurisdiction.

## Setting Up Indexers In Prowlarr

If this is your first time meeting **Prowlarr**: it's an *indexer aggregator*. It connects to a bunch of torrent sites (called "indexers") and gives qBitlarr one unified search API. Without it, qBitlarr would have to know how to talk to dozens of different sites with their own quirks — Prowlarr is the layer that hides all that. Add indexers once, and every qBitlarr search hits all of them in parallel.

**Adding an indexer:**

1. Open `http://localhost:9696` and go to **Indexers → + Add Indexer**.
2. Type the indexer name in the filter.
3. **Public indexer**: usually just click **Save** — no login needed.
4. **Private tracker**: paste the cookie / API key / passkey from your account on that tracker. Each tracker has slightly different fields and Prowlarr's form tells you what's needed.
5. Hit **Test** to confirm Prowlarr can reach it, then **Save**.
6. The indexer now has a numeric ID, discoverable via `curl http://localhost:8000/prowlarr/indexers`.

For any indexer behind Cloudflare, also tag it with the `flaresolverr` proxy — see [Why FlareSolverr Is Bundled](#why-flaresolverr-is-bundled) just below.

**Public vs private trackers:**

- **Public indexers** are usually quick to add but often have lower signal-to-noise: more dead torrents, spam, and fake releases.
- **Private trackers** require an account and often have stricter access rules. Their setup fields vary; follow the requirements of trackers you are allowed to use.

**Recommendations:**

- **Start with 2–4 indexers, not 20.** Every indexer adds latency to every search — one slow site can bottleneck the whole thing, and stacking public indexers mostly stacks noise, not signal.
- **Mix coverage with quality.** One or two broad public indexers as a safety net, plus any private trackers you have access to, is a solid baseline.
- **Skip `Sync Profiles`** unless you also run Sonarr or Radarr — qBitlarr doesn't need them.

Once indexers are in place, optionally set primary vs fallback IDs in [Indexer Selection](#indexer-selection) so qBitlarr searches your fast trusted indexers first and only falls back to slower or noisier ones when needed.

## Why FlareSolverr Is Bundled

Some popular indexers sit behind **Cloudflare's anti-bot challenge**. A plain HTTP request — what Prowlarr makes by default — gets an HTML challenge page instead of search results, and the indexer effectively returns nothing.

**FlareSolverr** is a tiny headless-Chrome proxy that solves those challenges for Prowlarr. When Prowlarr is configured to route certain indexers through it, FlareSolverr opens the page in a real browser, waits for Cloudflare to pass, and hands the cookies back to Prowlarr so the actual search API call succeeds.

qBitlarr bundles it because the moment a user adds a Cloudflare-protected indexer to Prowlarr, they hit this wall — and the official fix is "install FlareSolverr separately." Shipping it in the compose file removes that footgun.

**How to wire it up in Prowlarr** (one-time, after first start):

1. Open Prowlarr at `http://localhost:9696`.
2. Go to **Settings → Indexers → Indexer Proxies**.
3. Click the **+** and pick **FlareSolverr**.
4. Set **Host** to `http://flaresolverr:8191` (the internal compose hostname) and give it a **Tag** like `flaresolverr`.
5. Save. Then on any Cloudflare-protected indexer, open it, add that same `flaresolverr` tag, and save.

Indexers without the tag bypass FlareSolverr entirely — there's no performance penalty for non-protected sites. If you don't use any CF-protected indexers, you can stop the container (`docker compose stop flaresolverr`) and qBitlarr keeps working.

## Quality Preferences

By default qBitlarr targets **1080p WEB-DL H.264** with at least 5 seeders. Change the defaults via env:

```sh
QBITLARR_PREFER_RESOLUTION=1080p   # 480p | 720p | 1080p | 2160p
QBITLARR_PREFER_SOURCE=WEB-DL      # WEB-DL | WEBRip | BluRay | HDTV
QBITLARR_PREFER_CODEC=H.264        # H.264 | H.265
QBITLARR_MIN_SEEDERS=5
```

End users override per-request just by saying so in natural language:

- `"The Hitch-Hiker 4K"` → forces 2160p
- `"The Hitch-Hiker Remux"` → forces a Remux release
- `"The Hitch-Hiker 720p HEVC"` → 720p H.265

## How A Request Is Resolved

Every `/handle` request follows the same path, so a keyword and an IMDb link end up in the same place:

1. **Identify the title.** An IMDb ID/URL or a supported Douban/AlloCine link resolves directly. A plain keyword is matched against Wikidata (no API key, no extra account). If several titles match, qBitlarr returns a `choose_title` list (title + year) and waits for the user to pick one; if nothing matches, it returns `needs_imdb` and asks for an IMDb link.
2. **Rank releases** for that one title using your quality preferences.
3. **Return the top 4 release choices by default** to pick from — or, in `auto` mode, queue the best one outright.

Wikidata keyword matching is intentionally lightweight, so obscure titles may not resolve; that is when qBitlarr asks for an IMDb link instead of guessing.

### Output Modes

`POST /handle` accepts an optional `mode` field:

- `manual` *(default)* — return ranked release choices, never queue anything.
- `auto` — queue the best release outright. Best for "set and forget" friends/family use; the response includes an `alternatives` list of 2–3 runner-ups for "or did you mean...".
- `confirm` — return the top pick plus runner-ups, but do **not** queue.

Override the server-wide default with `QBITLARR_DEFAULT_MODE=manual|auto|confirm`.

Choice display is transport-neutral for both title disambiguation (`choose_title`) and release picking (`show_results`). The REST response includes compact `label` values for generic clarify/picker tools, plus rendered choice fields for richer chat adapters. `choice_rich_message` is Telegram Bot API 10.1-friendly rich HTML: adapters can pass its `html` value as `sendRichMessage.rich_message.html` and render `choice_buttons` below it. If rich messages are unavailable, send `choice_display` by itself and do not append `choices_table`, `results`, or `label` values. The MCP wrapper returns an agent-facing `agent_clarify` object instead: Hermes-style flows should put `agent_clarify.display_table` in a fenced text/code block, append `agent_clarify.display_notice` after the block when present, pass `agent_clarify.choices` as short numeric button labels, and map the selected number through `agent_clarify.response_mapping`. The zero-config release default is stock Hermes-friendly: `QBITLARR_MANUAL_RESULT_LIMIT=4` and `QBITLARR_CHOICE_STYLE=hermes-default`, matching Hermes-style clarify surfaces that show four rows without leaking duplicate numbered lists. If your local Telegram/Hermes adapter can render a rich table plus a closed row of five buttons, set:

```sh
QBITLARR_MANUAL_RESULT_LIMIT=5
QBITLARR_CHOICE_STYLE=telegram-rich
```

That changes qBitlarr's structured response only; the actual horizontal button layout still belongs in your local chat adapter or Hermes profile. In `telegram-rich` mode, qBitlarr omits raw `choices_table` from the response and returns a plain-text `choice_display` fallback so Telegram bots do not show Markdown fences or duplicate numbered lists.

## Completed Task Cleanup

qBitlarr can optionally remove completed qBittorrent tasks that it manages while keeping the actual downloaded files on disk. New qBitlarr downloads receive the `qbitlarr.managed` tag; older tasks with `requester.*` tags can also be included for compatibility.

Disabled by default for open-source users. Enable and tune it with env vars:

```sh
QBITLARR_CLEANUP_ENABLED=false
QBITLARR_CLEANUP_COMPLETED_AFTER_SECONDS=259200
QBITLARR_CLEANUP_INTERVAL_SECONDS=21600
QBITLARR_CLEANUP_INCLUDE_LEGACY_REQUESTER_TAGS=true
```

Notes:

- `QBITLARR_CLEANUP_COMPLETED_AFTER_SECONDS=259200` means clean up tasks completed for at least 3 days.
- `QBITLARR_CLEANUP_INTERVAL_SECONDS=21600` checks every 6 hours.
- Cleanup calls qBittorrent with `delete_files=false`, so it deletes only the qBittorrent task, not media files.
- Unmanaged torrents without `qbitlarr.managed` or legacy `requester.*` tags are ignored.

Query snapshots used by manual result picking are pruned independently of qBittorrent task cleanup. The maintenance loop runs snapshot pruning even when `QBITLARR_CLEANUP_ENABLED=false`; tune the default 7-day retention with:

```sh
QBITLARR_QUERY_SNAPSHOT_RETENTION_SECONDS=604800
```

## Connect To An Agent

qBitlarr ships as an **MCP server**, so any agent that speaks the [Model Context Protocol](https://modelcontextprotocol.io) — Claude Desktop, Cursor, Cline, Hermes, OpenClaw, ChatGPT via an MCP bridge, your own custom agent — can use it.

The MCP tools are language-neutral. Users can ask in English, Chinese, French, or any language your agent's LLM handles; the agent can answer in the same language you use. That multilingual behavior depends on the LLM behind your agent, not on qBitlarr itself.

Two transports are available:

- **stdio MCP** — what most desktop agent apps want. They launch `bin/qbitlarr-mcp` as a subprocess.
- **HTTP MCP** — served at `http://localhost:8000/mcp` for hosts that prefer HTTP.

Tools exposed by both: `qbitlarr_handle`, `qbitlarr_search`, `qbitlarr_download`, `qbitlarr_list_downloads`, `qbitlarr_get_download_status`, `qbitlarr_render_downloads_status`, `qbitlarr_render_download_status`, `qbitlarr_pause_download`, `qbitlarr_resume_download`, `qbitlarr_delete_download`, `qbitlarr_watch_download`, `qbitlarr_get_query_snapshot`, `qbitlarr_list_prowlarr_indexers`, `qbitlarr_health`.

The stdio MCP wrapper also sends **one-time completion notifications** to Hermes-style targets:

- Pass `notification_target` (e.g. `telegram:123456789`) when queueing a torrent. qBitlarr watches the hash, posts one progress message, refreshes it on the watch interval, and messages that target at 100%. If `user_id` / `requester_id` is already a Hermes target, it is reused automatically — multi-user bots rarely need `notification_target` separately.
- The same per-user `user_id` / `requester_id` scopes status checks to that user's tagged torrents.
- For manual flows, call `qbitlarr_watch_download` with a known hash; pass `completion_followup_message` to append a "what starts next" line (e.g. subtitle processing).
- Telegram progress editing reads `QBITLARR_TELEGRAM_BOT_TOKEN`, then `QBITLARR_HERMES_ENV_PATH`, `HERMES_HOME/.env`, `~/.hermes/.env` — point `QBITLARR_HERMES_ENV_PATH` at a profile `.env` when running multiple bots.
- Watch state uses `QBITLARR_NOTIFICATION_WATCHES_PATH` when set; otherwise it defaults to `$XDG_DATA_HOME/qbitlarr/download-notification-watches.json`, or `~/.local/share/qbitlarr/download-notification-watches.json` when `XDG_DATA_HOME` is unset.
- `QBITLARR_COMPLETION_HOOK_COMMAND` runs a local command after a watched download completes or is removed; qBitlarr sends the user message first, then writes a `download_complete` / `download_removed` JSON event to the command's stdin. Hook failures are retried without hiding the user notice.

If `QBITLARR_API_KEY` is set, both transports require an `X-API-Key` header. The stdio MCP picks it up from the same env var.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "qbitlarr": {
      "command": "/absolute/path/to/qbitlarr/bin/qbitlarr-mcp",
      "env": {
        "QBITLARR_API_URL": "http://localhost:8000",
        "QBITLARR_API_KEY": ""
      }
    }
  }
}
```

Restart Claude Desktop. The qbitlarr tools appear in the tool list and Claude uses them when you ask about movies or TV.

### Cursor

Settings → **MCP** → **Add new MCP server**:

```json
{
  "mcpServers": {
    "qbitlarr": {
      "command": "/absolute/path/to/qbitlarr/bin/qbitlarr-mcp"
    }
  }
}
```

### Any other MCP host (Hermes, OpenClaw, Cline, custom agents)

The pattern is the same — they all support one or both transports:

- **Stdio path**: configure the host to launch `bin/qbitlarr-mcp` as a subprocess (with env vars for the API URL and optional API key).
- **HTTP path**: point the host at `http://localhost:8000/mcp` with the `X-API-Key` header if you set one.

For `choose_title` and `show_results`, MCP hosts should ask a picker question with `agent_clarify.display_table` inside a monospace block, append `agent_clarify.display_notice` after the block when present, pass `agent_clarify.choices` as short numeric button labels, and map the selected number through `agent_clarify.response_mapping`. Direct REST Telegram adapters that support Bot API `sendRichMessage` should render `choice_rich_message` first and place `choice_buttons` below it. If that is not available, send `choice_display` alone. Plain REST hosts using the default `hermes-default` response can render `choices_table` inside a monospace block.

### Tell your agent when to use qBitlarr

If your agent supports a system prompt or "tool instructions" field, add a short pointer so it reaches for qBitlarr at the right moment:

> *When the user asks to download a movie, TV show, or anime that they are allowed to access, use the qbitlarr MCP tools. Default to `qbitlarr_handle` — it accepts IMDb IDs, IMDb URLs, supported Douban movie links or IDs, supported AlloCine film links or IDs, and free-text titles. By default it returns ranked release choices to pick from (or a short title picker first when a keyword matches several titles); if it returns `needs_imdb`, ask the user for an IMDb link. Only fall back to `qbitlarr_search` + `qbitlarr_download` for manual power-user control.*

This nudges agents that wouldn't otherwise know your downloader is now an option.

### Quick sanity check

After wiring it up, ask the agent: *"Use qbitlarr_health to check that the service is up."* If it returns `{"status": "ok"}`, you're connected. Add `--deep` (or pass `deep: true`) to verify Prowlarr and qBittorrent are reachable too.

## CLI

The CLI is a thin client for the same REST API used by MCP. It reads `QBITLARR_API_URL`, `QBITLARR_API_KEY`, and `QBITLARR_API_TIMEOUT_SECONDS` from the environment, with flags available for overrides.

`handle` prints a friendly human response by default. Add `--json` when you want the raw structured response. Other subcommands print JSON by default for use with `jq`.

```sh
bin/qbitlarr handle "tt0045877"
bin/qbitlarr handle "douban:1292052"
bin/qbitlarr handle "https://www.allocine.fr/film/fichefilm_gen_cfilm=25801.html"
bin/qbitlarr handle "The Hitch-Hiker" --mode manual
bin/qbitlarr handle "The Hitch-Hiker" --user-id telegram:123456789
bin/qbitlarr handle "The Hitch-Hiker" --mode manual --json
bin/qbitlarr search --query "The Hitch-Hiker 1953 1080p" | jq '.[0]'
bin/qbitlarr download 'magnet:?xt=urn:btih:...' --user-id telegram:123456789
bin/qbitlarr downloads --watch --user-id telegram:123456789
bin/qbitlarr downloads --render --user-id telegram:123456789
bin/qbitlarr download-status abcdef1234567890 --user-id telegram:123456789
bin/qbitlarr download-status abcdef1234567890 --render --user-id telegram:123456789
bin/qbitlarr health --deep
bin/qbitlarr indexers
```

Quote magnet links in your shell because they often contain `&`.

Inside the Docker container, run the same CLI module with `docker compose exec qbitlarr python -m app.cli health --deep`. The `bin/qbitlarr` launcher is for host checkout use.

## Authentication

For deployments beyond localhost, set `QBITLARR_API_KEY`. Every REST and MCP request then needs the `X-API-Key` header:

```sh
curl -H 'X-API-Key: change-this' http://localhost:8000/health
```

Leave blank for unauthenticated local-only use.

## Prowlarr URLs

`PROWLARR_URL` is the URL qBitlarr uses for Prowlarr API calls. In Docker Compose it defaults to `http://prowlarr:9696`, the internal service hostname — most users don't need to change this.

`PROWLARR_DOWNLOAD_URL` is optional. Set it only when Prowlarr returns proxy download URLs that qBitlarr must rewrite before fetching the `.torrent` file, for example when qBitlarr must reach Prowlarr through a LAN address instead of the internal Docker hostname.

## Indexer Selection

`PROWLARR_PRIMARY_INDEXER_IDS` and `PROWLARR_FALLBACK_INDEXER_IDS` are optional comma-separated indexer IDs.

- Leave both blank to let Prowlarr search every applicable indexer.
- Set primary IDs to prefer a trusted subset first.
- Set fallback IDs for broader or slower indexers to try only when primary results are missing or unsuitable.

Discover IDs after Prowlarr is configured:

```sh
curl http://localhost:8000/prowlarr/indexers
```

## Save Paths

`/handle` routes each queued download to a save path based on media type and resolution:

- `QBITLARR_SAVE_PATH_MOVIE=/downloads/movies`
- `QBITLARR_SAVE_PATH_MOVIE_4K=/downloads/movies-4k`
- `QBITLARR_SAVE_PATH_TV=/downloads/tv`

TV downloads create a show folder under the TV base path, for example `/downloads/tv/Example Show`.

Both `/handle` and `/download` also accept an optional `save_path` field for one-off overrides. Overrides must be inside one of the configured roots above or inside a comma-separated `QBITLARR_EXTRA_SAVE_PATHS` entry, such as `/media/Kids`.

When `save_path` is omitted, `/handle` and `/download` use qBitlarr's configured defaults. `/download` infers the target from the torrent metadata or magnet display name, so manual selections from search results still land in the movie, 4K movie, or TV path instead of qBittorrent's global default download folder.

## REST API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Service liveness |
| GET | `/health?deep=true` | Liveness + Prowlarr/qBittorrent reachability |
| POST | `/handle` | Main entry point: search and (optionally) queue |
| POST | `/search` | Raw Prowlarr search |
| POST | `/download` | Queue a known download link |
| GET | `/downloads` | List torrents in qBittorrent |
| GET | `/downloads/status-message` | Render all matching downloads as a chat progress message |
| GET | `/downloads/{info_hash}` | Read one torrent by info hash |
| GET | `/downloads/{info_hash}/status-message` | Render one torrent as a chat progress message |
| POST | `/downloads/{info_hash}/pause` | Pause one requester-owned torrent |
| POST | `/downloads/{info_hash}/resume` | Resume one requester-owned torrent |
| POST | `/downloads/{info_hash}/delete` | Delete one requester-owned qBittorrent task without deleting files |
| GET | `/queries/{query_id}` | Re-read a saved search snapshot |
| GET | `/prowlarr/indexers` | List Prowlarr indexers with IDs |

Example: queue a known link to a specific folder.

```sh
curl -X POST http://localhost:8000/download \
  -H 'Content-Type: application/json' \
  -d '{"download_link":"magnet:?xt=urn:btih:...","save_path":"/media/Kids"}'
```

## Project Structure

```
qbitlarr/
├── app/            FastAPI service — REST API, CLI, and the canonical logic
│   ├── api/        REST endpoint handlers (handle, search, download, ...)
│   ├── domain/     Pure logic: ranking, save paths, choice tables, progress cards
│   └── services/   External clients: prowlarr, qbittorrent, wikidata
├── mcp_server/     stdio MCP server (thin wrappers around app/client.py)
├── bin/            `qbitlarr` and `qbitlarr-mcp` launchers
├── tests/          pytest suite
├── docs/           architecture diagram + README screenshots
└── docker-compose.yml, Dockerfile, .env.example, README*.md
```

The REST API is the canonical surface; the CLI and stdio MCP are thin clients of `app/client.py`. Most logic lives in `app/api/handle.py` (orchestration: identify → rank → queue) and `app/domain/quality.py` (pure ranking, no network).

## Pair With Babelarr For Subtitles

qBitlarr handles acquisition; pair it with [Babelarr](https://github.com/davezfr/babelarr) to prepare subtitles after a download finishes. When both MCP servers are available to one agent, *"Download The Hitch-Hiker and add Chinese-English subtitles"* becomes: qBitlarr queues the movie, and once it has a local path Babelarr finds or downloads a source subtitle, translates it, and writes the SRT/ASS sidecar. For a durable queue, also expose Babelarr's Runtime MCP server — it remembers the download and dispatches Babelarr when the path is ready.

<p>
  <img src="docs/screenshots/telegram-qbitlarr-babelarr-one-shot.jpg" alt="Telegram example: one request downloads His Girl Friday with qBitlarr and then prepares Chinese-English subtitles with Babelarr.">
</p>

*Combined workflow screenshot for reference only. The demo uses a public-domain title; rights can vary by jurisdiction and by specific restoration, soundtrack, subtitles, or edition.*

## Third-Party Projects

qBitlarr integrates with these third-party projects:

- **[Prowlarr](https://github.com/Prowlarr/Prowlarr)** — GPL-3.0. qBitlarr can run Prowlarr as a separate Docker Compose service and talks to it through its HTTP API.
- **[qBittorrent](https://github.com/qbittorrent/qBittorrent)** — GPL-2.0. qBitlarr expects you to provide qBittorrent separately and talks to it through its Web UI API.
- **[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)** — MIT. qBitlarr's Docker Compose setup includes it as an optional challenge proxy for Prowlarr indexers that need it.

qBitlarr is not affiliated with, endorsed by, or sponsored by Prowlarr,
qBittorrent, FlareSolverr, or their maintainers.

## License

MIT.
