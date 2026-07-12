# MPilot Migration Guide

MPilot replaces the separate qBitlarr and Babelarr repositories. The old
repositories are archived for history; new integrations should target MPilot.

## Repository Move

| Old project | New home |
|---|---|
| `davezfr/qbitlarr` | `davezfr/mpilot` acquisition toolset |
| `davezfr/babelarr` | `davezfr/mpilot` subtitle and runtime toolsets |

The old repos remain readable so existing links, issues, and screenshots do not
disappear. They are not the live development home.

## Package Names

Use these imports for new code:

| Area | New import |
|---|---|
| Acquisition API/client/domain | `mpilot.acquisition`, `mpilot.api` |
| Unified MCP server | `mpilot.mcp.server` |
| Subtitle workflow | `mpilot.subtitles` |
| Runtime workflow store | `mpilot.runtime` |
| Shared helpers | `mpilot.core` |

Older top-level import shims are not part of the MPilot public package. Update
new code to import from `mpilot.*` directly.

## Commands

Use these commands for new deployments:

| Purpose | New command |
|---|---|
| Unified CLI | `mpilot` |
| Acquisition CLI | `mpilot acquisition ...` |
| Subtitle CLI | `mpilot subtitles ...` |
| Runtime CLI | `mpilot runtime ...` |
| Unified MCP server | `mpilot-mcp` |
| Unified background daemon | `mpilot-daemon` |

Old per-project launchers are not shipped from MPilot. Replace them with the
commands above.

## Environment Variables

Prefer `MPILOT_*` names for new deployments. Provider names such as
`PLEX_*`, `OPENSUBTITLES_*`, and `SUBDL_API_KEY` keep their existing upstream
names.

| Area | Preferred names |
|---|---|
| Toolset gates | `MPILOT_ENABLE_ACQUISITION_TOOLS`, `MPILOT_ENABLE_SUBTITLE_TOOLS` |
| Acquisition API client | `MPILOT_ACQUISITION_API_URL`, `MPILOT_ACQUISITION_API_KEY`, `MPILOT_ACQUISITION_API_TIMEOUT_SECONDS` |
| Prowlarr | `MPILOT_PROWLARR_URL`, `MPILOT_PROWLARR_DOWNLOAD_URL`, `MPILOT_PROWLARR_API_KEY`, `MPILOT_PROWLARR_PRIMARY_INDEXER_IDS`, `MPILOT_PROWLARR_FALLBACK_INDEXER_IDS`, `MPILOT_PROWLARR_IMDB_NATIVE_INDEXER_IDS`, `MPILOT_PROWLARR_IMDB_KEYWORD_INDEXER_IDS`, `MPILOT_PROWLARR_IMDB_DISABLED_INDEXER_IDS` |
| qBittorrent | `MPILOT_QBIT_URL`, `MPILOT_QBIT_USERNAME`, `MPILOT_QBIT_PASSWORD` |
| Save paths | `MPILOT_ACQUISITION_SAVE_PATH_MOVIE`, `MPILOT_ACQUISITION_SAVE_PATH_MOVIE_4K`, `MPILOT_ACQUISITION_SAVE_PATH_TV`, `MPILOT_ACQUISITION_EXTRA_SAVE_PATHS` |
| Subtitle defaults | `MPILOT_SUBTITLE_SOURCE_LANGUAGE`, `MPILOT_SUBTITLE_TARGET_LANGUAGE`, `MPILOT_SUBTITLE_OUTPUT_MODE`, `MPILOT_SUBTITLE_BACKEND`, `MPILOT_SUBTITLE_MODEL` |
| Subtitle jobs | `MPILOT_SUBTITLE_JOB_STORE_DIR`, `MPILOT_SUBTITLE_JOB_NOTIFICATION_WATCHES_PATH` |
| Runtime workflows | `MPILOT_RUNTIME_STORE_DIR`, `MPILOT_RUNTIME_CONTENT_PATH_PREFIX`, `MPILOT_RUNTIME_LOCAL_CONTENT_PATH_PREFIX` |
| Plex/path mapping | `PLEX_BASE_URL`, `PLEX_TOKEN`, `MPILOT_PLEX_PATH_PREFIX`, `MPILOT_LOCAL_PATH_PREFIX` |
| Providers | `OPENSUBTITLES_API_KEY`, `OPENSUBTITLES_USER_AGENT`, `OPENSUBTITLES_USERNAME`, `OPENSUBTITLES_PASSWORD`, `OPENSUBTITLES_TOKEN`, `SUBDL_API_KEY` |
| Notifications | `MPILOT_TELEGRAM_BOT_TOKEN`, `MPILOT_HERMES_*`, `MPILOT_ACQUISITION_NOTIFICATION_*`, `MPILOT_SUBTITLE_JOB_NOTIFICATION_*` |
| Daemon | `MPILOT_DAEMON_LOCK_PATH` |

Compatibility env names from qBitlarr, Babelarr, MST, and MWR are still accepted
where practical so users can migrate incrementally. New examples should use the
MPilot names above.

## MCP Configuration

If your host previously configured separate acquisition, subtitle, and runtime
MCP servers, remove those entries and use one MPilot server instead:

```json
{
  "mpilot": {
    "command": "/absolute/path/to/mpilot/bin/mpilot-mcp",
    "env": {
      "MPILOT_ENABLE_ACQUISITION_TOOLS": "true",
      "MPILOT_ENABLE_SUBTITLE_TOOLS": "true",
      "MPILOT_PROWLARR_URL": "http://127.0.0.1:9696",
      "MPILOT_PROWLARR_API_KEY": "replace-with-key",
      "MPILOT_QBIT_URL": "http://127.0.0.1:8080",
      "MPILOT_QBIT_USERNAME": "replace-with-user",
      "MPILOT_QBIT_PASSWORD": "replace-with-password",
      "PLEX_BASE_URL": "http://127.0.0.1:32400",
      "PLEX_TOKEN": "replace-with-token"
    }
  }
}
```

Use these MPilot tool names in new prompts and adapters:

- `media_request` for combined download plus subtitle intent.
- `acquisition_handle`, `acquisition_download`, and
  `acquisition_render_downloads_status` for acquisition-only workflows.
- `plex_search`, `subtitle_plan`, `job_create_video`, `job_start`, and
  `job_show` for subtitle workflows.
- `queue_status` and `workflow_show` for long-running workflow state.

## Daemon Migration

Run one supervisor instead of separate notification/watch loops:

```bash
mpilot-daemon
```

Use `docs/deploy/launchd.plist` or `docs/deploy/systemd.service` as deployment
templates.

## Data Locations

MPilot still uses JSON stores for this release:

- Download notification watches:
  `MPILOT_ACQUISITION_NOTIFICATION_WATCHES_PATH`, defaulting to
  `~/.local/share/mpilot/acquisition/download-notification-watches.json`.
- Subtitle job notifications:
  `MPILOT_SUBTITLE_JOB_NOTIFICATION_WATCHES_PATH`.
- Subtitle jobs:
  `MPILOT_SUBTITLE_JOB_STORE_DIR`, defaulting under
  `~/.local/share/mpilot/subtitles/`.
- Runtime workflows:
  `MPILOT_RUNTIME_STORE_DIR`.

If you have existing qBitlarr/Babelarr state files, either keep the old env
pointing at those paths for one migration run or copy the files into the MPilot
locations above.
