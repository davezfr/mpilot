from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException

from mpilot.acquisition.config import Settings, get_settings
from mpilot.acquisition.domain.quality import (
    DEFAULT_QUALITY_PREFERENCES,
    MediaType,
    QualityPreferences,
    calculate_quality_preference,
    calculate_score,
    clean_display_title,
    contains_premium_quality_request,
    extract_imdb_id,
    extract_requested_resolution,
    format_choice_label,
    format_quality,
    infer_media_type,
    normalize_user_message,
    parse_quality,
)
from mpilot.acquisition.domain.choice_table import (
    render_choice_rich_html,
    render_choice_table,
    render_title_choice_rich_html,
    render_title_choice_table,
)
from mpilot.acquisition.domain.save_paths import default_save_path_for_title, validate_save_path_override
from mpilot.acquisition.domain.torrent_metadata import parse_torrent_name
from mpilot.acquisition.exceptions import ConfigurationError, UpstreamServiceError
from mpilot.acquisition.models import (
    ChoiceButton,
    ChoiceRichMessage,
    ChoiceUiHints,
    HandleRequest,
    HandleResponse,
    ManualSearchResult,
    MovieCandidate,
    SearchRequest,
    SearchResult,
    TorrentStatus,
)
from mpilot.acquisition.services.prowlarr import search_prowlarr
from mpilot.acquisition.services.query_snapshots import QuerySnapshotStore, create_query_id
from mpilot.acquisition.services.qbittorrent import (
    _download_torrent_file,
    add_download_to_qbittorrent,
    list_downloads_from_qbittorrent,
    tag_download_for_requester,
)
from mpilot.acquisition.services.wikidata import resolve_external_movie_id, search_movie_candidates


logger = logging.getLogger("mpilot.acquisition.api.handle")
router = APIRouter()

DEFAULT_MANUAL_RESULT_LIMIT = 4
MAX_MANUAL_RESULT_LIMIT = 10
MANUAL_RESULT_LIMIT = DEFAULT_MANUAL_RESULT_LIMIT
DEFAULT_CHOICE_STYLE = "hermes-default"
CHOICE_STYLES = {"hermes-default", "telegram-rich"}
MANUAL_RESULTS_MESSAGE = "Here are the top results, please reply with the number:"
AUTO_FALLBACK_MESSAGE = "No suitable auto-download found. Here are the top results, please reply with the number:"
DEFAULT_SEARCH_CATEGORIES = [2040, 5040]
FULL_MOVIE_TV_CATEGORIES = [2000, 5000]
FULL_CATEGORY_KEYWORDS = (
    "4K",
    "2160p",
    "UHD",
    "REMUX",
)
METADATA_VERIFICATION_LIMIT = 10
METADATA_VERIFICATION_BATCH_SIZE = 2
EXTERNAL_ID_UNRESOLVED_MESSAGE = (
    "I couldn't match that link to a movie reliably. "
    "For faster and more precise results, please send the IMDb link or IMDb ID instead."
)
KEYWORD_UNRESOLVED_MESSAGE = (
    "I couldn't find a movie or show matching that title. "
    "Please send the IMDb link or IMDb ID and I'll take it from there."
)
CHOOSE_TITLE_MESSAGE = "I found a few possible matches. Reply with the number of the title you mean:"
MOVIE_CANDIDATE_LIMIT = 5


@router.post(
    "/handle",
    response_model=HandleResponse,
    operation_id="acquisition_handle",
    summary="Search or download a movie or TV show",
    description=(
        "Use MPilot acquisition to handle one movie or TV request. IMDb IDs, URLs, and supported Douban/AlloCine "
        "links resolve to the canonical IMDb flow; by default all input returns ranked choices."
    ),
    tags=["acquisition"],
)
async def handle(request: HandleRequest, background_tasks: BackgroundTasks) -> HandleResponse:
    """Search movie or TV requests and either auto-download or return ranked choices."""
    try:
        settings = get_settings()
        user_message = normalize_user_message(request.user_message)
        imdb_id = extract_imdb_id(user_message)
        query_id = create_query_id()
        store = QuerySnapshotStore(_query_snapshot_dir(settings))

        mode = _resolve_mode(request.mode, settings)

        if imdb_id:
            logger.info("Handling IMDb request for user_id=%s mode=%s", request.user_id or "anonymous", mode)
            return await _handle_imdb_request(
                imdb_id,
                settings,
                user_message=user_message,
                query_id=query_id,
                store=store,
                background_tasks=background_tasks,
                save_path_override=request.save_path,
                requester_id=request.user_id,
                mode=mode,
            )

        external_resolution = await resolve_external_movie_id(user_message, settings)
        if external_resolution and external_resolution.get("imdb_id"):
            logger.info(
                "Resolved %s source_id=%s to imdb_id=%s for user_id=%s mode=%s",
                external_resolution.get("source"),
                external_resolution.get("source_id"),
                external_resolution.get("imdb_id"),
                request.user_id or "anonymous",
                mode,
            )
            return await _handle_imdb_request(
                str(external_resolution["imdb_id"]),
                settings,
                user_message=user_message,
                query_id=query_id,
                store=store,
                background_tasks=background_tasks,
                save_path_override=request.save_path,
                requester_id=request.user_id,
                mode=mode,
            )
        if external_resolution:
            logger.info(
                "Could not resolve external movie source=%s source_id=%s for user_id=%s",
                external_resolution.get("source"),
                external_resolution.get("source_id"),
                request.user_id or "anonymous",
            )
            return _needs_imdb_response(
                query_id=query_id,
                store=store,
                user_message=user_message,
                settings=settings,
                message=EXTERNAL_ID_UNRESOLVED_MESSAGE,
                snapshot_status="external_id_unresolved",
            )

        logger.info("Handling keyword request for user_id=%s", request.user_id or "anonymous")
        candidates = await search_movie_candidates(user_message, settings, limit=MOVIE_CANDIDATE_LIMIT)

        if not candidates:
            logger.info("No movie/show candidate matched the keyword for user_id=%s", request.user_id or "anonymous")
            return _needs_imdb_response(
                query_id=query_id,
                store=store,
                user_message=user_message,
                settings=settings,
                message=KEYWORD_UNRESOLVED_MESSAGE,
                snapshot_status="keyword_unresolved",
            )

        if len(candidates) == 1:
            only = candidates[0]
            logger.info(
                "Keyword resolved to a single candidate imdb_id=%s for user_id=%s",
                only["imdb_id"],
                request.user_id or "anonymous",
            )
            return await _handle_imdb_request(
                only["imdb_id"],
                settings,
                user_message=user_message,
                query_id=query_id,
                store=store,
                background_tasks=background_tasks,
                save_path_override=request.save_path,
                requester_id=request.user_id,
                mode=mode,
            )

        logger.info(
            "Keyword matched %d candidates for user_id=%s",
            len(candidates),
            request.user_id or "anonymous",
        )
        return _choose_title_response(
            candidates,
            query_id=query_id,
            store=store,
            user_message=user_message,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except UpstreamServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _handle_imdb_request(
    imdb_id: str,
    settings: Settings,
    *,
    user_message: str,
    query_id: str,
    store: QuerySnapshotStore,
    background_tasks: BackgroundTasks,
    save_path_override: str | None,
    requester_id: str | None,
    mode: str = "auto",
) -> HandleResponse:
    base_request = SearchRequest(query=imdb_id, categories=get_categories(user_message))
    primary_results = await _search_primary(base_request, settings)
    media_type = infer_media_type(user_message, primary_results)
    prefer_premium = contains_premium_quality_request(user_message)
    requested_resolution = extract_requested_resolution(user_message)
    preferences = _preferences(settings)
    initial_selected = _select_best_result(
        primary_results,
        media_type=media_type,
        prefer_premium=prefer_premium,
        requested_resolution=requested_resolution,
        preferences=preferences,
    )
    skip_existing_check = mode == "manual"
    existing_download = None
    if not skip_existing_check:
        existing_download = await _find_existing_download_for_results(
            primary_results,
            settings,
            media_type=media_type,
            prefer_premium=prefer_premium,
            requested_resolution=requested_resolution,
        )
    if existing_download:
        await tag_download_for_requester(settings, existing_download.hash, requester_id)
        primary_ranked = _rank_results(
            primary_results,
            media_type=media_type,
            prefer_premium=prefer_premium,
            requested_resolution=requested_resolution,
            require_min_seeders=False,
            preferences=preferences,
        )
        store.create(
            query_id=query_id,
            request=_snapshot_request_payload(user_message=user_message, search_request=base_request, settings=settings),
            status="already_in_qbittorrent",
            reason="matching_download_exists",
            results=primary_ranked,
        )
        display_title = clean_display_title(existing_download.name)
        quality = format_quality(parse_quality(existing_download.name))
        logger.info(
            "Existing qBittorrent download matched imdb_id=%s media_type=%s quality=%s hash=%s",
            imdb_id,
            media_type,
            quality,
            existing_download.hash,
        )
        return HandleResponse(
            status="success",
            action="auto_download",
            imdb_id=imdb_id,
            media_type=media_type,
            title=display_title,
            quality=quality,
            query_id=query_id,
            snapshot_status="already_in_qbittorrent",
            download_status=existing_download,
            message=_auto_download_message(
                display_title,
                existing_download.seeds or _first_known_seeders(primary_ranked),
                already_downloading=True,
            ),
            alternatives=_to_manual_results(primary_ranked[:_INLINE_ALTERNATIVE_LIMIT], compact_labels=True) or None,
        )

    if _should_refine_imdb_title_search(
        initial_selected,
        media_type=media_type,
        prefer_premium=prefer_premium,
        requested_resolution=requested_resolution,
        preferences=preferences,
    ):
        primary_results = _merge_results(
            primary_results,
            await _search_title_refinement(base_request, settings, initial_results=primary_results),
        )
        media_type = infer_media_type(user_message, primary_results)

    selected = await _select_best_verified_result(
        primary_results,
        settings,
        media_type=media_type,
        prefer_premium=prefer_premium,
        requested_resolution=requested_resolution,
    )
    primary_ranked = _rank_results(
        primary_results,
        media_type=media_type,
        prefer_premium=prefer_premium,
        requested_resolution=requested_resolution,
        require_min_seeders=False,
        preferences=preferences,
    )
    snapshot_status = "primary_ready" if primary_ranked else "primary_empty"
    results_for_manual_fallback = primary_results
    fallback_searched = False

    store.create(
        query_id=query_id,
        request=_snapshot_request_payload(user_message=user_message, search_request=base_request, settings=settings),
        status=snapshot_status,
        reason="primary_results_ready" if primary_ranked else "primary_no_results",
        results=primary_ranked,
    )

    if mode == "manual":
        _schedule_fallback_snapshot(
            background_tasks,
            query_id=query_id,
            base_request=base_request,
            settings=settings,
            store=store,
            existing_results=primary_ranked,
            media_type=media_type,
            prefer_premium=prefer_premium,
            requested_resolution=requested_resolution,
        )
        return _manual_results_response(
            primary_ranked,
            status="success" if primary_ranked else "not_found",
            message=MANUAL_RESULTS_MESSAGE,
            media_type=media_type,
            prefer_premium=prefer_premium,
            requested_resolution=requested_resolution,
            query_id=query_id,
            snapshot_status="primary_ready" if primary_ranked else "not_found",
            preferences=preferences,
            compact_labels=True,
            manual_result_limit=_manual_result_limit(settings),
            choice_style=_choice_style(settings),
        )

    if not selected:
        fallback_results = await _search_fallback_and_append(
            query_id=query_id,
            base_request=base_request,
            settings=settings,
            store=store,
            existing_results=primary_ranked,
            media_type=media_type,
            prefer_premium=prefer_premium,
            requested_resolution=requested_resolution,
            require_min_seeders=False,
        )
        fallback_searched = True
        results_for_manual_fallback = fallback_results
        selected = await _select_best_verified_result(
            fallback_results,
            settings,
            media_type=media_type,
            prefer_premium=prefer_premium,
            requested_resolution=requested_resolution,
        )
        if selected:
            primary_results = fallback_results
            snapshot_status = "fallback_ready"
        else:
            snapshot_status = "not_found"

    if not selected:
        logger.info(
            "No auto-download candidate met the threshold for imdb_id=%s media_type=%s",
            imdb_id,
            media_type,
        )
        return _manual_results_response(
            results_for_manual_fallback,
            status="not_found",
            message=AUTO_FALLBACK_MESSAGE,
            media_type=media_type,
            prefer_premium=prefer_premium,
            requested_resolution=requested_resolution,
            query_id=query_id,
            snapshot_status=snapshot_status,
            preferences=preferences,
            compact_labels=True,
            manual_result_limit=_manual_result_limit(settings),
            choice_style=_choice_style(settings),
        )

    if not fallback_searched:
        snapshot_status = "primary_ready"
        _schedule_fallback_snapshot(
            background_tasks,
            query_id=query_id,
            base_request=base_request,
            settings=settings,
            store=store,
            existing_results=primary_ranked,
            media_type=media_type,
            prefer_premium=prefer_premium,
            requested_resolution=requested_resolution,
        )

    quality = format_quality(parse_quality(selected.title))
    display_title = clean_display_title(selected.title)
    alternatives_pool = [r for r in primary_ranked if r.download_link != selected.download_link]
    alternatives = _to_manual_results(alternatives_pool[:_INLINE_ALTERNATIVE_LIMIT], compact_labels=True) or None

    if mode == "confirm":
        logger.info(
            "Confirm-mode selection for imdb_id=%s media_type=%s quality=%s seeders=%s indexer=%s",
            imdb_id,
            media_type,
            quality,
            selected.seeders,
            selected.indexer,
        )
        return HandleResponse(
            status="success",
            action="confirm",
            imdb_id=imdb_id,
            media_type=media_type,
            title=display_title,
            quality=quality,
            query_id=query_id,
            snapshot_status=snapshot_status,
            message=(
                f"Top pick: {display_title} in {quality}. "
                "Send the download_link to /download (or call acquisition_download) to queue it."
            ),
            results=_to_manual_results([selected], compact_labels=True),
            alternatives=alternatives,
        )

    save_path = _save_path_for_download(
        settings=settings,
        media_type=media_type,
        title=selected.title,
        override=save_path_override,
    )

    logger.info(
        "Auto-selected imdb_id=%s media_type=%s quality=%s seeders=%s indexer=%s save_path=%s",
        imdb_id,
        media_type,
        quality,
        selected.seeders,
        selected.indexer,
        save_path,
    )
    download_status = await add_download_to_qbittorrent(
        selected.download_link,
        settings,
        save_path=save_path,
        requester_id=requester_id,
    )

    return HandleResponse(
        status="success",
        action="auto_download",
        imdb_id=imdb_id,
        media_type=media_type,
        title=display_title,
        quality=quality,
        query_id=query_id,
        snapshot_status=snapshot_status,
        download_status=download_status,
        message=_auto_download_message(display_title, selected.seeders),
        alternatives=alternatives,
    )


def _save_path_for_download(
    *,
    settings: Settings,
    media_type: MediaType,
    title: str,
    override: str | None = None,
) -> str:
    if override:
        return validate_save_path_override(override, settings)
    return default_save_path_for_title(settings=settings, media_type=media_type, title=title)


def _preferences(settings: Settings) -> QualityPreferences:
    prefs = getattr(settings, "quality_preferences", None)
    if prefs is not None:
        return prefs
    return QualityPreferences(
        resolution=getattr(settings, "prefer_resolution", DEFAULT_QUALITY_PREFERENCES.resolution),
        source=getattr(settings, "prefer_source", DEFAULT_QUALITY_PREFERENCES.source),
        codec=getattr(settings, "prefer_codec", DEFAULT_QUALITY_PREFERENCES.codec),
        min_seeders=getattr(settings, "min_seeders", DEFAULT_QUALITY_PREFERENCES.min_seeders),
    )


def _resolve_mode(request_mode: str | None, settings: Settings) -> str:
    if request_mode:
        return request_mode
    default = getattr(settings, "default_mode", "manual") or "manual"
    return default if default in ("auto", "manual", "confirm") else "manual"


def _manual_result_limit(settings: Settings) -> int:
    value = getattr(settings, "manual_result_limit", DEFAULT_MANUAL_RESULT_LIMIT)
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MANUAL_RESULT_LIMIT
    if limit < 1:
        return DEFAULT_MANUAL_RESULT_LIMIT
    return min(limit, MAX_MANUAL_RESULT_LIMIT)


def _choice_style(settings: Settings) -> str:
    style = str(getattr(settings, "choice_style", DEFAULT_CHOICE_STYLE) or DEFAULT_CHOICE_STYLE).strip().lower()
    return style if style in CHOICE_STYLES else DEFAULT_CHOICE_STYLE


def _to_manual_results(
    results: list[SearchResult],
    *,
    start_index: int = 1,
    limit: int = MANUAL_RESULT_LIMIT,
    compact_labels: bool = False,
) -> list[ManualSearchResult]:
    pool = _dedupe_same_release(results) if compact_labels else results
    manual_results = []
    for index, result in enumerate(pool[:limit], start=start_index):
        parsed = parse_quality(result.title)
        manual_results.append(
            ManualSearchResult(
                index=index,
                title=result.title,
                quality=format_quality(parsed),
                seeders=result.seeders,
                size=result.size,
                download_link=result.download_link,
                label=format_choice_label(parsed) if compact_labels else result.title,
            )
        )
    return manual_results


def _dedupe_same_release(results: list[SearchResult]) -> list[SearchResult]:
    # The same release listed by multiple indexers differs only in seeders;
    # results arrive ranked best-first, so keeping the first occurrence keeps
    # the most-seeded copy of each (label, size) pairing.
    seen: set[tuple[str, int | None]] = set()
    deduped = []
    for result in results:
        key = (format_choice_label(parse_quality(result.title)), result.size)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(result)
    return deduped


_INLINE_ALTERNATIVE_LIMIT = 3


def _select_best_result(
    results: list[SearchResult],
    *,
    media_type: MediaType,
    prefer_premium: bool,
    requested_resolution: str | None = None,
    preferences: QualityPreferences = DEFAULT_QUALITY_PREFERENCES,
) -> SearchResult | None:
    ranked_results = _rank_results(
        results,
        media_type=media_type,
        prefer_premium=prefer_premium,
        requested_resolution=requested_resolution,
        require_min_seeders=True,
        preferences=preferences,
    )
    if not ranked_results:
        return None

    return ranked_results[0]


async def _select_best_verified_result(
    results: list[SearchResult],
    settings: Settings,
    *,
    media_type: MediaType,
    prefer_premium: bool,
    requested_resolution: str | None = None,
) -> SearchResult | None:
    preferences = _preferences(settings)
    ranked_results = _rank_results(
        results,
        media_type=media_type,
        prefer_premium=prefer_premium,
        requested_resolution=requested_resolution,
        require_min_seeders=True,
        preferences=preferences,
    )
    if not ranked_results:
        return None

    best_score: int | None = None
    best_result: SearchResult | None = None
    verification_results = ranked_results[:METADATA_VERIFICATION_LIMIT]
    for start in range(0, len(verification_results), METADATA_VERIFICATION_BATCH_SIZE):
        batch = verification_results[start : start + METADATA_VERIFICATION_BATCH_SIZE]
        original_scores = [
            calculate_score(
                result,
                media_type=media_type,
                prefer_premium=prefer_premium,
                requested_resolution=requested_resolution,
                preferences=_preferences(settings),
            )
            for result in batch
        ]
        if best_score is not None and all(
            original_score is None or best_score >= original_score for original_score in original_scores
        ):
            break

        verified_results = await asyncio.gather(
            *(_result_with_torrent_metadata_title(result, settings) for result in batch)
        )
        for original_score, verified_result in zip(original_scores, verified_results):
            if best_score is not None and original_score is not None and best_score >= original_score:
                return best_result or ranked_results[0]

            verified_score = calculate_score(
                verified_result,
                media_type=media_type,
                prefer_premium=prefer_premium,
                requested_resolution=requested_resolution,
                preferences=_preferences(settings),
            )
            if verified_score is not None and (best_score is None or verified_score > best_score):
                best_score = verified_score
                best_result = verified_result

    return best_result or ranked_results[0]


async def _result_with_torrent_metadata_title(result: SearchResult, settings: Settings) -> SearchResult:
    metadata_title = await _get_torrent_metadata_title(result, settings)
    if not metadata_title:
        return result
    return result.model_copy(update={"title": metadata_title})


async def _get_torrent_metadata_title(result: SearchResult, settings: Settings) -> str | None:
    parsed = urlparse(result.download_link)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.path.rstrip("/").endswith("/download"):
        return None

    try:
        content = await _download_torrent_file(result.download_link, settings)
    except UpstreamServiceError:
        return None

    return parse_torrent_name(content)


async def _find_existing_download_for_results(
    results: list[SearchResult],
    settings: Settings,
    *,
    media_type: MediaType,
    prefer_premium: bool,
    requested_resolution: str | None,
) -> TorrentStatus | None:
    result_limit = _manual_result_limit(settings)
    title_keys = {_title_match_key(clean_display_title(result.title)) for result in results[:result_limit]}
    title_keys.discard("")
    if not title_keys:
        return None

    try:
        downloads = await list_downloads_from_qbittorrent(settings)
    except (AttributeError, UpstreamServiceError):
        logger.warning("Could not check existing qBittorrent downloads before auto-selection")
        return None

    preferences = _preferences(settings)
    for download in downloads:
        if not _existing_download_matches_quality(
            download,
            media_type=media_type,
            prefer_premium=prefer_premium,
            requested_resolution=requested_resolution,
            preferences=preferences,
        ):
            continue
        if _title_match_key(clean_display_title(download.name)) in title_keys:
            return download

    return None


def _existing_download_matches_quality(
    download: TorrentStatus,
    *,
    media_type: MediaType,
    prefer_premium: bool,
    requested_resolution: str | None,
    preferences: QualityPreferences = DEFAULT_QUALITY_PREFERENCES,
) -> bool:
    parsed = parse_quality(download.name)
    if requested_resolution:
        return parsed.resolution == requested_resolution
    if prefer_premium:
        return parsed.is_premium
    return (
        parsed.resolution == preferences.resolution
        and parsed.source == preferences.source
        and parsed.codec == preferences.codec
    )


def _title_match_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.casefold())


def _auto_download_message(
    display_title: str,
    seeders: int | None,
    *,
    already_downloading: bool = False,
) -> str:
    action = "is already in the system" if already_downloading else "is now downloading"
    if seeders is None or seeders < 0:
        return f"{display_title} {action}. You can ask for a status update any time."
    return (
        f"{display_title} {action} with {seeders} {_pluralize_seeder(seeders)}. "
        "You can ask for a status update any time."
    )


def _first_known_seeders(results: list[SearchResult]) -> int | None:
    for result in results:
        if result.seeders is not None and result.seeders >= 0:
            return result.seeders
    return None


def _pluralize_seeder(seeders: int) -> str:
    return "seeder" if seeders == 1 else "seeders"


def get_categories(user_message: str) -> list[int]:
    normalized_message = user_message.casefold()
    if any(keyword.casefold() in normalized_message for keyword in FULL_CATEGORY_KEYWORDS):
        return list(FULL_MOVIE_TV_CATEGORIES)
    return list(DEFAULT_SEARCH_CATEGORIES)


def _needs_imdb_response(
    *,
    query_id: str,
    store: QuerySnapshotStore,
    user_message: str,
    settings: Settings,
    message: str,
    snapshot_status: str,
) -> HandleResponse:
    base_request = SearchRequest(query=user_message, categories=get_categories(user_message))
    store.create(
        query_id=query_id,
        request=_snapshot_request_payload(user_message=user_message, search_request=base_request, settings=settings),
        status=snapshot_status,
        reason=snapshot_status,
        results=[],
    )
    return HandleResponse(
        status="not_found",
        action="needs_imdb",
        message=message,
        query_id=query_id,
        snapshot_status=snapshot_status,
        results=[],
    )


def _choose_title_response(
    candidates: list[dict],
    *,
    query_id: str,
    store: QuerySnapshotStore,
    user_message: str,
    settings: Settings,
) -> HandleResponse:
    base_request = SearchRequest(query=user_message, categories=get_categories(user_message))
    movie_candidates = _to_movie_candidates(candidates)
    rendered_choices_table = render_title_choice_table(movie_candidates) if movie_candidates else None
    choice_style = _choice_style(settings)
    choices_table, choice_display = _choice_rendering_fields(
        CHOOSE_TITLE_MESSAGE,
        rendered_choices_table,
        choice_style,
    )
    store.create(
        query_id=query_id,
        request=_snapshot_request_payload(user_message=user_message, search_request=base_request, settings=settings),
        status="title_candidates",
        reason="title_candidates_ready",
        results=[],
    )
    return HandleResponse(
        status="success",
        action="choose_title",
        message=CHOOSE_TITLE_MESSAGE,
        choices_table=choices_table,
        choice_display=choice_display,
        choice_buttons=_candidate_choice_buttons(movie_candidates) if movie_candidates else None,
        ui_hints=_choice_ui_hints(choice_style) if movie_candidates else None,
        choice_rich_message=_title_choice_rich_message(CHOOSE_TITLE_MESSAGE, movie_candidates) if movie_candidates else None,
        query_id=query_id,
        snapshot_status="title_candidates",
        candidates=movie_candidates,
    )


def _to_movie_candidates(candidates: list[dict]) -> list[MovieCandidate]:
    movie_candidates: list[MovieCandidate] = []
    for index, candidate in enumerate(candidates, start=1):
        title = candidate["title"]
        year = candidate.get("year")
        label = f"{title} ({year})" if year else title
        movie_candidates.append(
            MovieCandidate(index=index, title=title, year=year, imdb_id=candidate["imdb_id"], label=label)
        )
    return movie_candidates


def _manual_results_response(
    results: list[SearchResult],
    *,
    status: str,
    message: str,
    media_type: MediaType,
    prefer_premium: bool,
    requested_resolution: str | None = None,
    query_id: str | None = None,
    snapshot_status: str | None = None,
    preferences: QualityPreferences = DEFAULT_QUALITY_PREFERENCES,
    compact_labels: bool = False,
    manual_result_limit: int = DEFAULT_MANUAL_RESULT_LIMIT,
    choice_style: str = DEFAULT_CHOICE_STYLE,
) -> HandleResponse:
    ranked_results = _rank_results(
        results,
        media_type=media_type,
        prefer_premium=prefer_premium,
        requested_resolution=requested_resolution,
        require_min_seeders=False,
        preferences=preferences,
    )
    manual_results = _to_manual_results(
        ranked_results,
        compact_labels=compact_labels,
        limit=manual_result_limit,
    )
    rendered_choices_table = render_choice_table(manual_results) if compact_labels and manual_results else None
    choices_table, choice_display = _choice_rendering_fields(
        message,
        rendered_choices_table,
        choice_style,
    )
    return HandleResponse(
        status=status,
        action="show_results",
        message=message,
        choices_table=choices_table,
        choice_display=choice_display,
        choice_buttons=_choice_buttons(manual_results) if manual_results else None,
        ui_hints=_choice_ui_hints(choice_style) if manual_results else None,
        choice_rich_message=_choice_rich_message(message, manual_results) if manual_results else None,
        query_id=query_id,
        snapshot_status=snapshot_status,
        results=manual_results,
    )


def _choice_rendering_fields(
    message: str,
    choices_table: str | None,
    choice_style: str,
) -> tuple[str | None, str | None]:
    if not choices_table:
        return None, None
    normalized = choice_style if choice_style in CHOICE_STYLES else DEFAULT_CHOICE_STYLE
    if normalized == "telegram-rich":
        return None, _choice_plain_display(message, choices_table)
    return choices_table, _choice_markdown_display(message, choices_table)


def _choice_plain_display(message: str, choices_table: str) -> str:
    return f"{message}\n\n{choices_table}"


def _choice_markdown_display(message: str, choices_table: str) -> str:
    return f"{message}\n\n```text\n{choices_table}\n```"


def _choice_buttons(results: list[ManualSearchResult]) -> list[ChoiceButton]:
    return [
        ChoiceButton(index=result.index, text=str(result.index), value=str(result.index))
        for result in results
    ]


def _candidate_choice_buttons(candidates: list[MovieCandidate]) -> list[ChoiceButton]:
    return [
        ChoiceButton(index=candidate.index, text=str(candidate.index), value=str(candidate.index))
        for candidate in candidates
    ]


def _choice_rich_message(message: str, results: list[ManualSearchResult]) -> ChoiceRichMessage:
    return ChoiceRichMessage(
        format="telegram-html",
        html=render_choice_rich_html(message, results),
        skip_entity_detection=True,
    )


def _title_choice_rich_message(message: str, candidates: list[MovieCandidate]) -> ChoiceRichMessage:
    return ChoiceRichMessage(
        format="telegram-html",
        html=render_title_choice_rich_html(message, candidates),
        skip_entity_detection=True,
    )


def _choice_ui_hints(choice_style: str) -> ChoiceUiHints:
    normalized = choice_style if choice_style in CHOICE_STYLES else DEFAULT_CHOICE_STYLE
    return ChoiceUiHints(
        choice_style=normalized,
        recommended_button_layout="inline-row" if normalized == "telegram-rich" else "vertical",
        closed_choice=True,
    )


def _rank_results(
    results: list[SearchResult],
    *,
    media_type: MediaType,
    prefer_premium: bool,
    requested_resolution: str | None,
    require_min_seeders: bool,
    preferences: QualityPreferences = DEFAULT_QUALITY_PREFERENCES,
) -> list[SearchResult]:
    ranked: list[tuple[int, int, int, SearchResult]] = []
    for result in results:
        rank_score = _result_rank_score(
            result,
            media_type=media_type,
            prefer_premium=prefer_premium,
            requested_resolution=requested_resolution,
            require_min_seeders=require_min_seeders,
            preferences=preferences,
        )
        if rank_score is None:
            continue

        ranked.append((rank_score, result.seeders or 0, result.size or 0, result))

    if require_min_seeders:
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    else:
        ranked.sort(key=lambda item: (item[1], item[0], item[2]), reverse=True)
    return [item[3] for item in ranked]


def _result_rank_score(
    result: SearchResult,
    *,
    media_type: MediaType,
    prefer_premium: bool,
    requested_resolution: str | None,
    require_min_seeders: bool,
    preferences: QualityPreferences = DEFAULT_QUALITY_PREFERENCES,
) -> int | None:
    if require_min_seeders:
        return calculate_score(
            result,
            media_type=media_type,
            prefer_premium=prefer_premium,
            requested_resolution=requested_resolution,
            preferences=preferences,
        )

    preference_score = calculate_quality_preference(
        result,
        media_type=media_type,
        prefer_premium=prefer_premium,
        requested_resolution=requested_resolution,
        preferences=preferences,
    )
    if preference_score is None:
        return None

    return preference_score + min(result.seeders or 0, 99)


async def _search_primary(request: SearchRequest, settings: Settings) -> list[SearchResult]:
    return await search_prowlarr(
        request.model_copy(update={"indexer_ids": _primary_indexer_ids(settings)}),
        settings,
    )


async def _search_title_refinement(
    base_request: SearchRequest,
    settings: Settings,
    *,
    initial_results: list[SearchResult],
) -> list[SearchResult]:
    refinement_query = _derive_refinement_query(initial_results)
    if not refinement_query or refinement_query == base_request.query:
        return []

    try:
        return await _search_primary(base_request.model_copy(update={"query": refinement_query}), settings)
    except UpstreamServiceError:
        logger.warning("IMDb title refinement search failed for query=%s", refinement_query)
        return []


def _derive_refinement_query(results: list[SearchResult]) -> str | None:
    for result in results:
        candidate = clean_display_title(result.title)
        if candidate and candidate != result.title:
            return candidate
    return None


def _should_refine_imdb_title_search(
    selected: SearchResult | None,
    *,
    media_type: MediaType,
    prefer_premium: bool,
    requested_resolution: str | None,
    preferences: QualityPreferences = DEFAULT_QUALITY_PREFERENCES,
) -> bool:
    if prefer_premium or requested_resolution not in {None, preferences.resolution}:
        return False
    if selected is None:
        return True

    parsed = parse_quality(selected.title)
    if media_type == "tv":
        return not (parsed.is_amzn and parsed.source == preferences.source and parsed.codec == preferences.codec)
    return not (parsed.source == preferences.source and parsed.codec == preferences.codec)


async def _search_fallback_and_append(
    *,
    query_id: str,
    base_request: SearchRequest,
    settings: Settings,
    store: QuerySnapshotStore,
    existing_results: list[SearchResult],
    media_type: MediaType,
    prefer_premium: bool,
    requested_resolution: str | None,
    require_min_seeders: bool,
) -> list[SearchResult]:
    fallback_ids = _fallback_indexer_ids(settings)
    if not fallback_ids:
        store.append(
            query_id=query_id,
            status="not_found",
            reason="fallback_not_configured",
            results=existing_results,
        )
        return existing_results

    try:
        fallback_results = await search_prowlarr(
            base_request.model_copy(update={"indexer_ids": fallback_ids}),
            settings,
        )
    except UpstreamServiceError:
        logger.warning("Fallback search failed for query_id=%s", query_id)
        store.append(
            query_id=query_id,
            status="fallback_error" if existing_results else "not_found",
            reason="fallback_error",
            results=existing_results,
        )
        return existing_results
    merged_results = _rank_results(
        _merge_results(existing_results, fallback_results),
        media_type=media_type,
        prefer_premium=prefer_premium,
        requested_resolution=requested_resolution,
        require_min_seeders=require_min_seeders,
        preferences=_preferences(settings),
    )
    store.append(
        query_id=query_id,
        status="fallback_ready" if merged_results else "not_found",
        reason="fallback_results_ready" if merged_results else "fallback_no_results",
        results=merged_results,
    )
    return merged_results


def _schedule_fallback_snapshot(
    background_tasks: BackgroundTasks,
    *,
    query_id: str,
    base_request: SearchRequest,
    settings: Settings,
    store: QuerySnapshotStore,
    existing_results: list[SearchResult],
    media_type: MediaType,
    prefer_premium: bool,
    requested_resolution: str | None,
) -> None:
    if not _fallback_indexer_ids(settings):
        return
    background_tasks.add_task(
        _search_fallback_and_append,
        query_id=query_id,
        base_request=base_request,
        settings=settings,
        store=store,
        existing_results=existing_results,
        media_type=media_type,
        prefer_premium=prefer_premium,
        requested_resolution=requested_resolution,
        require_min_seeders=False,
    )


def _merge_results(primary: list[SearchResult], fallback: list[SearchResult]) -> list[SearchResult]:
    merged: list[SearchResult] = []
    seen: set[str] = set()
    for result in [*primary, *fallback]:
        key = result.download_link.casefold()
        if key in seen:
            continue
        seen.add(key)
        merged.append(result)
    return merged


def _snapshot_request_payload(
    *,
    user_message: str,
    search_request: SearchRequest,
    settings: Settings,
) -> dict:
    return {
        "input": user_message,
        "identifier": search_request.identifier,
        "query": search_request.query,
        "categories": search_request.categories,
        "primary_indexer_ids": _primary_indexer_ids(settings),
        "fallback_indexer_ids": _fallback_indexer_ids(settings),
    }


def _primary_indexer_ids(settings: Settings) -> list[int] | None:
    return getattr(settings, "prowlarr_primary_indexer_ids", None)


def _fallback_indexer_ids(settings: Settings) -> list[int] | None:
    return getattr(settings, "prowlarr_fallback_indexer_ids", None)


def _query_snapshot_dir(settings: Settings) -> str:
    return getattr(settings, "query_snapshot_dir", "data/query-snapshots")
