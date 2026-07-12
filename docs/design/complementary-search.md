# Complementary Title Search

Status: approved for implementation
Date: 2026-07-12

## Summary

MPilot keeps IMDb IDs as the primary acquisition key. Every indexer that can
search an IMDb ID is queried in one pass. When that complete IMDb pass returns
zero raw results, MPilot offers a separate complementary title search using a
canonical `title + year` query.

The same complementary path can also be requested explicitly from Telegram.
The existing Telegram choice UI already allows free-text input, so no new
button or free-form search prompt is required. The exact input `补充搜索` is a
control phrase: qbot uses the active query ID to call the complementary
search tool. It is not used as a search query.

Complementary results are not directly verified by an IMDb identifier. They
must be title/year validated and shown as manual choices. Version one must
never auto-download them.

## Goals

- Keep IMDb as the normal and authoritative search key.
- Search all IMDb-capable indexers concurrently in one pass.
- Remove the unused primary/fallback indexer tiers.
- Automatically offer complementary search only when the completed IMDb pass
  returns exactly zero raw results.
- Allow a Telegram user to force the same complementary search by entering the
  exact control phrase `补充搜索` after receiving release choices.
- Derive the complementary query from canonical metadata, not arbitrary user
  text.
- Keep complementary source capability independent from IMDb source
  capability.
- Preserve requester ownership and the original query context through
  `query_id`.

## Non-goals

- Do not use title search when IMDb results exist but fail quality, resolution,
  seeder, or automatic-download thresholds.
- Do not accept a caller-provided complementary search string in version one.
- Do not add a Telegram button; the existing free-text "other" path is enough.
- Do not auto-download a complementary result.
- Do not merge complementary results into the IMDb result set as if they had
  equivalent identity confidence.
- Do not treat an upstream error or timeout as an empty IMDb result.

## Terminology

`IMDb search` means the single normal pass over all configured indexers whose
IMDb mode is `native` or `keyword`.

`Complementary search` means a separate title query generated from the IMDb
metadata for an existing query snapshot.

`Raw results` means normalized Prowlarr results before quality, resolution,
seeder, or auto-selection filtering.

The word `fallback` is not used for this feature. The current fallback code is
an indexer-tier mechanism that repeats the same IMDb query against another set
of sources. It is conceptually different and will be removed.

## Indexer capability model

IMDb capability remains a mutually exclusive mode:

- `native`: use Prowlarr's structured IMDb parameter.
- `keyword`: search the literal `tt...` value.
- `disabled`: do not use this indexer in the IMDb pass.
- `unconfigured`: skip the indexer until an operator classifies it.

Complementary capability is an independent boolean. Introduce:

```text
MPILOT_PROWLARR_COMPLEMENTARY_INDEXER_IDS=
```

The corresponding setting should be named
`prowlarr_complementary_indexer_ids`. Indexer inspection should expose
`complementary_search_enabled` alongside `imdb_search_mode`.

Examples:

| Indexer kind | IMDb mode | Complementary enabled |
| --- | --- | --- |
| Structured IMDb API | `native` | operator choice |
| Literal IMDb search plus title search | `keyword` | yes |
| Title-only tracker | `disabled` | yes |
| Unsuitable or broken tracker | `disabled` | no |

An indexer may therefore be disabled for IMDb while remaining useful for
complementary search. RuTracker is an example of a title-only source. 52BT can
search some literal IMDb IDs and can also participate in title search.

## Remove the legacy indexer tiers

Delete the acquisition meaning of:

```text
MPILOT_PROWLARR_PRIMARY_INDEXER_IDS
MPILOT_PROWLARR_FALLBACK_INDEXER_IDS
```

This includes settings fields, environment aliases, Compose wiring, example
configuration, documentation, helper functions, background fallback tasks,
fallback-specific snapshot states, and tests that exist only for this tiering.

The normal IMDb pass should call the existing IMDb capability router without
an indexer subset. That router already separates `native` and `keyword`
requests, runs them concurrently, merges results, and deduplicates them.

Removing indexer tiers must not remove query snapshots themselves. Snapshots
remain the ownership-bound context required for complementary search and later
download selection.

## Metadata resolution

Add a resolver from an IMDb title ID to structured metadata:

```text
resolve_imdb_metadata(imdb_id) -> {
  imdb_id,
  canonical_title,
  year,
  media_type,
  metadata_source
}
```

Version one should use a structured, no-key provider through a small provider
boundary. The existing Wikidata service is the preferred first implementation:
query by IMDb property `P345`, retrieve a canonical English label, release
year, and enough type information to retain movie/TV routing.

Direct IMDb HTML parsing is not the primary version-one dependency because its
markup, localization, and anti-bot behavior are unstable. The provider boundary
may add IMDb-page parsing later as a secondary resolver without changing the
search contract.

Automatic complementary search requires both a non-empty canonical title and
a release year. If either is missing, stop with a metadata-unavailable result;
do not silently search a broad title-only query.

The version-one query rule is intentionally deterministic:

```text
{canonical_title} {year}
```

Examples:

```text
Port Authority 2019
Sarajevo Safari 2022
```

The original Telegram text is never reused as the complementary query.

## Search flow

### Normal IMDb pass

1. Parse or resolve the request to a canonical IMDb ID.
2. Search every configured `native` and `keyword` indexer concurrently.
3. Merge and deduplicate the raw results.
4. If raw results exist, continue the existing quality and mode behavior.
5. If zero raw results were returned successfully, create an `imdb_empty`
   snapshot and return the `complementary_search` action.
6. If the IMDb pass failed, return the upstream error. Do not start
   complementary search.

The complementary trigger is based on raw result count, not on whether an
automatic candidate was selected. For example, low-seeder IMDb results still
count as IMDb results and suppress automatic complementary search.

### Automatic trigger through qbot

MPilot's synchronous handle call cannot send a Telegram message in the middle
of an internal search. Automatic complementary search is therefore a
two-tool-call Agent protocol:

1. `acquisition_handle` completes the IMDb pass and returns:
   - `status = "success"`, because orchestration should continue rather than
     terminate as a final not-found response;
   - `action = "complementary_search"`
   - the original `query_id`
   - a user-facing message explaining that IMDb returned no results and that a
     title-based complementary search will now be attempted.
2. qbot sends that message to Telegram immediately.
3. Without waiting for another user response, qbot calls
   `acquisition_complementary_search(query_id)`.
4. MPilot resolves metadata, performs the title query, validates results, and
   returns the normal manual-choice payload.

This makes the progress message visible while keeping the transition automatic.

### Explicit Telegram trigger

After a normal result list, Telegram already provides an "other" free-text
entry. The exact normalized input is:

```text
补充搜索
```

qbot must treat this as a control phrase, not as a title or search query.
It calls `acquisition_complementary_search` with the most recent active
`query_id` owned by that requester in the current conversation or Topic.

Matching is exact after trimming surrounding whitespace. Do not add fuzzy
aliases in version one.

If no active query ID is available, qbot responds that there is no search to
supplement and asks the user to search for a movie or show first. It must not
call `acquisition_handle("补充搜索")`.

## API and MCP contract

Add an ownership-protected REST operation:

```text
POST /queries/{query_id}/complementary-search
```

The endpoint accepts no arbitrary search query. It reads the IMDb ID,
requester, media context, and categories from the stored snapshot. It applies
the same `require_snapshot_owner` behavior as the existing snapshot endpoint:
an administrator may access any snapshot, while a requester-specific API key
may access only its own snapshot. Cross-requester access must look like a 404.

Add matching client and MCP surfaces:

```text
acquisition_complementary_search(query_id: str)
```

The MCP tool description must explicitly instruct qbot to:

- call the tool automatically when `acquisition_handle` returns the
  `complementary_search` action, after first sending the returned message;
- call it when the user enters the exact control phrase `补充搜索` for an active
  query;
- never pass `补充搜索` to the normal handle tool;
- render the returned choices exactly like normal manual release choices.

The response may reuse `HandleResponse` and its existing choice rendering, but
the model must add `complementary_search` to the action enum and expose enough
provenance to prevent callers from treating the results as IMDb-verified. At a
minimum include:

```text
search_strategy = "complementary"
query_used = "Port Authority 2019"
```

Normal IMDb responses should identify `search_strategy = "imdb"` where doing
so does not break existing clients.

## Snapshot contract

The initial snapshot request must persist, at minimum:

- `requester_id`
- canonical `imdb_id`
- media type and categories
- the original user input
- the normal IMDb search result entry

Suggested statuses and reasons are:

| Status | Meaning |
| --- | --- |
| `imdb_ready` | IMDb returned one or more raw results |
| `imdb_empty` | IMDb pass succeeded with zero raw results |
| `complementary_ready` | Validated complementary results are available |
| `complementary_empty` | Complementary search completed with no valid result |
| `complementary_metadata_unavailable` | Canonical title/year resolution failed |
| `complementary_error` | Metadata or Prowlarr failed; this is not an empty result |

Append complementary results as a new snapshot entry. Preserve the IMDb entry
instead of replacing it, even when it is empty. Record `query_used`, metadata
source, trigger (`automatic_empty` or `user_requested`), and complementary
indexer IDs in snapshot metadata.

The implementation may extend the snapshot models to carry entry metadata.
Do not encode provenance only in a human-readable reason string.

## Result validation and safety

Complementary results are candidates, not IMDb-verified releases.

Version-one validation must:

- normalize Unicode, case, punctuation, separators, and repeated whitespace;
- require the canonical title tokens in order;
- require the exact release year;
- reject a result that contains a conflicting year;
- deduplicate by normalized download link or info hash;
- retain the source indexer and complementary query as provenance.

The exact-year rule intentionally rejects broad matches such as an unrelated
`Port Authority` file with no `2019` marker. Recall can be improved later only
with an explicit design change and tests.

Both automatic and user-requested complementary searches use the same system-
generated query and strict validator. The user control phrase changes only the
trigger, not the search text or validation confidence.

Regardless of the incoming mode (`auto`, `confirm`, or `manual`), a
complementary response must be manual selection only:

- return `action = "show_results"` when results exist;
- return numbered buttons through the existing Telegram choice contract;
- never enqueue a download until the user selects a result;
- clearly state that the choices came from title/year complementary search.

When the user selects a complementary result, pass the original `query_id` to
the existing download operation so requester ownership, media type, save path,
and completion notification context are preserved.

## User-facing messages

IMDb empty, before automatic complementary search:

```text
目前通过 IMDb ID 没有找到结果，我现在尝试使用标准标题和年份进行补充搜索。
```

Complementary results ready:

```text
IMDb ID 搜索没有结果。以下是通过“{title} {year}”找到的补充结果，请确认后选择。
```

No active Telegram context:

```text
当前没有可以补充的搜索，请先搜索一部电影或剧集。
```

Metadata unavailable:

```text
IMDb ID 没有搜索结果，同时无法取得可靠的标准标题和年份，因此没有启动补充搜索。
```

## Acceptance criteria

### Legacy removal

- Primary/fallback indexer settings and behavior are removed from code,
  Compose, examples, README, migration docs, MCP descriptions, and tests.
- All configured IMDb-capable indexers are searched in one pass.
- No fallback background task or fallback-specific snapshot state remains.

### Trigger behavior

- Zero raw IMDb results returns `complementary_search`, even in manual mode.
- One or more raw IMDb results never auto-trigger complementary search,
  regardless of quality or seeders.
- IMDb timeout/error never triggers complementary search.
- The exact Telegram phrase `补充搜索` routes to the complementary tool for the
  active owned query.
- The same phrase without active context does not become a title search.

### Metadata and query generation

- `tt7587282` resolves to `Port Authority`, year `2019`, and generates
  `Port Authority 2019`.
- `tt23861448` resolves to `Sarajevo Safari`, year `2022`, and generates
  `Sarajevo Safari 2022`.
- Missing title or year prevents a broad complementary query.

### Indexer routing

- Only IDs in `MPILOT_PROWLARR_COMPLEMENTARY_INDEXER_IDS` receive the title
  query.
- Complementary membership is independent of `imdb_search_mode`.
- Indexer listing reports both IMDb mode and complementary enablement.

### Results and ownership

- Results missing the exact year are excluded.
- Complementary results are never auto-downloaded.
- Choice rendering reuses the existing Telegram manual-result buttons.
- Snapshot owner enforcement prevents one requester from invoking another
  requester's complementary search.
- The original query ID remains usable when downloading a chosen result.

### Verification

- Add deterministic unit/API/MCP tests with mocked metadata and Prowlarr
  responses; live trackers must not be required by CI.
- Run the full test suite, Ruff, public-repository hygiene checks, and the
  Docker build used by CI.
- Rebuild/recreate the local MPilot API and verify deep health.
- Perform live, non-downloading smoke tests for the two IMDb IDs above and
  verify qbot's Telegram control-phrase behavior before declaring the work
  complete.

## Rollout notes

The local operator must classify complementary indexers explicitly after the
new setting exists. Do not infer complementary membership from IMDb mode.

The existing local examples suggest title-search sources such as RuTracker and
52BT, but IDs are installation-specific and must not be committed as public
defaults.

This change alters only search orchestration and manual choice generation. It
must not broaden download authorization, expose raw download URLs to the Agent,
or weaken requester-scoped snapshot/download checks.
