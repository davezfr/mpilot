from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .plex_resolver import PlexResolvedMedia
from .source import VIDEO_FILE_EXTENSIONS
from .subtitle_matching import parse_release_info


def local_search_by_title(
    query: str,
    root: Path,
    *,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    year: Optional[int] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    if not query.strip():
        raise ValueError("query is required")
    if (season is None) != (episode is None):
        raise ValueError("pass both --season and --episode for TV episodes")

    root = Path(root).expanduser()
    effective_season, effective_episode = _episode_context(query, season, episode)
    query_title = _query_title(query, effective_season, effective_episode)
    query_tokens = _tokens(query_title)
    if not root.exists() or not query_tokens:
        return _search_result(query, [])

    episode_search = effective_season is not None and effective_episode is not None
    search_roots = _episode_search_roots(root, query_tokens) if episode_search else [root]
    matches: List[Dict[str, Any]] = []

    for search_root in search_roots:
        for video_path in _iter_video_files(search_root):
            if episode_search:
                match = _episode_match(video_path, root, query_tokens, effective_season, effective_episode)
            else:
                match = _movie_match(video_path, root, query_tokens, year)
            if match is None:
                continue
            matches.append(match)
            if len(matches) >= max(1, int(limit)):
                break
        if len(matches) >= max(1, int(limit)):
            break
    return _search_result(query, matches)


def _search_result(query: str, matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(matches) == 1:
        status = "single_match"
    elif matches:
        status = "multiple_matches"
    else:
        status = "no_match"
    return {
        "status": status,
        "source": "local",
        "query": query,
        "match_count": len(matches),
        "matches": matches,
    }


def _episode_context(query: str, season: Optional[int], episode: Optional[int]) -> Tuple[Optional[int], Optional[int]]:
    if season is not None and episode is not None:
        return season, episode
    parsed = parse_release_info(query)
    return parsed.season, parsed.episode


def _query_title(query: str, season: Optional[int], episode: Optional[int]) -> str:
    if season is None or episode is None:
        return query
    match = re.search(r"(?:^|[^a-z0-9])s\d{1,2}[^a-z0-9]*e\d{1,3}(?:[^a-z0-9]|$)", query, re.IGNORECASE)
    if match:
        before = query[: match.start()].strip()
        if before:
            return before
    return query


def _episode_search_roots(root: Path, query_tokens: Tuple[str, ...]) -> List[Path]:
    candidates: List[Path] = []
    for child in _safe_iterdir(root):
        if not child.is_dir():
            continue
        if _contains_all_tokens(child.name, query_tokens):
            candidates.append(child)
            continue
        for grandchild in _safe_iterdir(child):
            if grandchild.is_dir() and _contains_all_tokens(grandchild.name, query_tokens):
                candidates.append(grandchild)
    return candidates


def _safe_iterdir(path: Path) -> Iterable[Path]:
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


def _iter_video_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in VIDEO_FILE_EXTENSIONS:
            yield path


def _episode_match(path: Path, root: Path, query_tokens: Tuple[str, ...], season: int, episode: int) -> Optional[Dict[str, Any]]:
    parsed = parse_release_info(path.name)
    if parsed.season != season or parsed.episode != episode:
        return None
    haystack = _episode_haystack(path, root, season, episode)
    if not _contains_all_tokens(haystack, query_tokens):
        return None
    return PlexResolvedMedia(
        rating_key=_local_rating_key(path),
        title=path.stem,
        media_type="episode",
        plex_file=str(path),
        local_file=str(path),
        path_mapping_applied=False,
        season=season,
        episode=episode,
        show_title=_show_title(path, root),
    ).to_dict()


def _movie_match(path: Path, root: Path, query_tokens: Tuple[str, ...], year: Optional[int]) -> Optional[Dict[str, Any]]:
    parsed = parse_release_info(path.name)
    if year is not None and parsed.year != year:
        return None
    if not _contains_all_tokens(_relative_text(path, root), query_tokens):
        return None
    return PlexResolvedMedia(
        rating_key=_local_rating_key(path),
        title=path.stem,
        media_type="movie",
        plex_file=str(path),
        local_file=str(path),
        path_mapping_applied=False,
    ).to_dict()


def _episode_haystack(path: Path, root: Path, season: int, episode: int) -> str:
    marker = re.compile(r"s%02d[^a-z0-9]*e%02d" % (season, episode), re.IGNORECASE)
    stem = path.stem
    match = marker.search(stem)
    filename_prefix = stem[: match.start()] if match else stem
    return " ".join([_relative_parent_text(path, root), filename_prefix])


def _relative_text(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _relative_parent_text(path: Path, root: Path) -> str:
    try:
        return str(path.parent.relative_to(root))
    except ValueError:
        return str(path.parent)


def _show_title(path: Path, root: Path) -> str:
    try:
        relative_parent = path.parent.relative_to(root)
        if relative_parent.parts:
            return relative_parent.parts[-1]
    except ValueError:
        pass
    return path.parent.name


def _local_rating_key(path: Path) -> str:
    return "local:%s" % path


def _tokens(value: str) -> Tuple[str, ...]:
    return tuple(token for token in re.split(r"[^a-z0-9]+", value.lower()) if token)


def _contains_all_tokens(value: str, required_tokens: Tuple[str, ...]) -> bool:
    available = set(_tokens(value))
    return all(token in available for token in required_tokens)
