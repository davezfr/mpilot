# Complementary Title Search

Status: approved for implementation
Date: 2026-07-12

## Summary

MPilot keeps IMDb IDs as the primary acquisition key. Every indexer that can
search an IMDb ID is queried in one pass. Query transport alone is not treated
as result identity proof: every raw result must pass a local canonical
title/alias and release-year gate. When that complete IMDb pass returns zero
identity-verified results, MPilot offers a separate complementary title search
using a canonical `title + year` query.

The same complementary path can also be requested explicitly from Telegram.
IMDb-verified release pickers render five numbered choices followed by a `🔎`
button on the same row. Selecting `🔎` uses the active query ID to call the
complementary search tool. The exact input `补充搜索` remains available as a
backward-compatible free-text control phrase; neither value is a search query.

Complementary results are not directly verified by an IMDb identifier. They
must be title/year validated and shown as manual choices. Version one must
never auto-download them.

## Goals

- Keep IMDb as the normal and authoritative search key.
- Search all IMDb-capable indexers concurrently in one pass.
- Remove the unused primary/fallback indexer tiers.
- Automatically offer complementary search only when the completed IMDb pass
  returns exactly zero identity-verified results.
- Allow a Telegram user to force the same complementary search through the
  `🔎` release-picker action or exact control phrase `补充搜索`.
- Derive the complementary query from canonical metadata, not arbitrary user
  text.
- Keep complementary source capability independent from IMDb source
  capability.
- Preserve requester ownership and the original query context through
  `query_id`.

## Non-goals

- Do not use title search when identity-verified IMDb results exist but fail
  quality, resolution, seeder, or automatic-download thresholds.
- Do not accept a caller-provided complementary search string in version one.
- Do not show `🔎` on complementary-result pickers, which would create a loop.
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
identity, seeder, or auto-selection filtering.

`Identity-verified results` means raw results admitted by MPilot's local IMDb
identity gate. The provider query mode (`native` or `keyword`) is provenance,
not proof. A result must match the canonical title or a known title alias;
movie results must also carry the exact non-conflicting release year.

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
  title_aliases,
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
2. Resolve canonical title, aliases, release year, and media type.
3. Search every configured `native` and `keyword` indexer concurrently.
4. Merge and deduplicate the raw results while retaining result IDs, indexer
   ID, and query mode as provenance.
5. Admit only results that pass the local IMDb identity gate. Provider search
   mode or ranking never bypasses this gate.
6. If identity-verified results exist, continue the existing quality and mode
   behavior using only those results.
7. If zero identity-verified results were returned successfully, create an
   `imdb_empty` snapshot and return the `complementary_search` action.
8. If raw results exist but canonical identity metadata is unavailable, record
   `imdb_identity_unavailable` and fail closed without persisting selectable
   raw links.
9. If the IMDb pass failed, return the upstream error. Do not start
   complementary search.

The complementary trigger is based on identity-verified result count, not raw
provider result count or whether an automatic candidate was selected. For
example, a low-seeder but identity-verified result suppresses automatic
complementary search; an unrelated collection returned for the `tt...` keyword
does not.

### Automatic trigger through qbot

MPilot's synchronous handle call cannot send a Telegram message in the middle
of an internal search. Automatic complementary search is therefore a
two-tool-call Agent protocol:

1. `acquisition_handle` completes the IMDb pass and returns:
   - `status = "success"`, because orchestration should continue rather than
     terminate as a final not-found response;
   - `action = "complementary_search"`
   - the original `query_id`
   - `message_key = "imdb_empty_complementary_starting"` with any interpolation
     values in `message_params`; the English `message` is fallback-only.
2. qbot localizes that semantic message in the current conversation language
   and sends it to Telegram immediately.
3. Without waiting for another user response, qbot calls
   `acquisition_complementary_search(query_id)`.
4. MPilot resolves metadata, performs the title query, validates results, and
   returns the normal manual-choice payload.

This makes the progress message visible while keeping the transition automatic.

### Explicit Telegram trigger

After an IMDb-verified result list, Telegram renders:

```text
[1] [2] [3] [4] [5] [🔎]
[✏️ Other (type answer)]
```

Selecting `🔎` resolves the clarify with that exact value and qbot immediately
calls `acquisition_complementary_search` for the active `query_id`. A
complementary-result picker keeps `[1]` through `[5]` and Other, but omits `🔎`.

The backward-compatible normalized free-text input is:

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
- call it when an IMDb-verified release clarify resolves to the exact `🔎`
  action for the active query;
- call it when the user enters the exact control phrase `补充搜索` for an active
  query;
- never pass `🔎` or `补充搜索` to the normal handle tool;
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
- canonical title, aliases, and year when available
- the admitted IMDb search result entry

Suggested statuses and reasons are:

| Status | Meaning |
| --- | --- |
| `imdb_ready` | One or more locally identity-verified IMDb results are available |
| `imdb_empty` | IMDb pass succeeded with zero identity-verified results |
| `imdb_identity_unavailable` | Raw results existed but canonical identity metadata was unavailable |
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

Normal IMDb results and complementary results use separate local admission
statuses. Only `imdb_verified` and `title_year_validated` snapshot results are
selectable through a `query_id`; old or otherwise `unverified` snapshot links
are rejected at the download boundary.

Normal IMDb result validation must:

- retain provider-reported IMDb/TMDB/TVDB IDs, indexer ID, and native/keyword
  query mode as provenance;
- reject a non-empty provider IMDb ID that conflicts with the requested ID;
- require contiguous canonical-title or known-alias tokens;
- for movies, require the exact release year and reject conflicting years;
- reject collection, pack, anthology, and similar aggregate markers outside
  the matched title;
- run before ranking, existing-download matching, manual display, confirmation,
  or automatic download;
- persist only admitted results in selectable snapshot entries while retaining
  aggregate rejection counts as snapshot metadata.

Complementary results are title/year-validated candidates, not IMDb-ID-verified
releases.

Version-one validation must:

- normalize Unicode, case, punctuation, separators, and repeated whitespace;
- require the canonical title or known-alias tokens contiguously;
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
- return numbered buttons through the existing Telegram choice contract, but
  omit `🔎` from complementary-result pickers;
- never enqueue a download until the user selects a result;
- clearly state that the choices came from title/year complementary search.

When the user selects a complementary result, pass the original `query_id` to
the existing download operation so requester ownership, media type, save path,
and completion notification context are preserved. The download operation must
also verify that the exact link belongs to an admitted snapshot result.

## User-facing messages

MPilot does not choose the user's language. It returns a semantic `message_key`
and `message_params`, plus an English `message` fallback for non-agent clients.
Qbot renders the meaning in the current conversation language.

| Situation | `message_key` | Parameters |
| --- | --- | --- |
| IMDb empty; complementary search starting | `imdb_empty_complementary_starting` | none |
| Automatic complementary results ready | `complementary_results_automatic` | `query_used` |
| User-requested complementary results ready | `complementary_results_user_requested` | `query_used` |
| No complementary results | `complementary_no_results` | `query_used` |
| Metadata unavailable | `complementary_metadata_unavailable` | none |

The no-active-query Telegram response is also generated by Qbot in the current
chat language; it is not a fixed backend string.

## Acceptance criteria

### Legacy removal

- Primary/fallback indexer settings and behavior are removed from code,
  Compose, examples, README, migration docs, MCP descriptions, and tests.
- All configured IMDb-capable indexers are searched in one pass.
- No fallback background task or fallback-specific snapshot state remains.

### Trigger behavior

- Zero identity-verified IMDb results returns `complementary_search`, even when
  raw providers returned unrelated matches and even in manual mode.
- One or more identity-verified IMDb results never auto-trigger complementary
  search, regardless of quality or seeders.
- IMDb timeout/error never triggers complementary search.
- The exact Telegram phrase `补充搜索` routes to the complementary tool for the
  active owned query.
- IMDb-verified release pickers render `[1] [2] [3] [4] [5] [🔎]` on one row;
  `🔎` routes to complementary search and does not select a release.
- Complementary-result pickers retain Other but do not render `🔎`.
- The same phrase without active context does not become a title search.

### Metadata and query generation

- `tt7587282` resolves to `Port Authority`, year `2019`, and generates
  `Port Authority 2019`.
- `tt23861448` resolves to `Sarajevo Safari`, year `2022`, and generates
  `Sarajevo Safari 2022`.
- Missing title or year prevents a broad complementary query.
- Raw IMDb results are never trusted when canonical identity metadata is
  unavailable.

### Indexer routing

- Only IDs in `MPILOT_PROWLARR_COMPLEMENTARY_INDEXER_IDS` receive the title
  query.
- Complementary membership is independent of `imdb_search_mode`.
- Indexer listing reports both IMDb mode and complementary enablement.

### Results and ownership

- Results missing the exact year are excluded.
- Wrong-title collections returned from a literal IMDb keyword search are
  excluded before ranking and are absent from selectable snapshots.
- Canonical title aliases are accepted when the exact release year matches.
- A snapshot download rejects unverified and non-member links.
- Complementary results are never auto-downloaded.
- Choice rendering reuses the existing Telegram manual-result buttons and
  keeps the hardcoded Other free-text row.
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
