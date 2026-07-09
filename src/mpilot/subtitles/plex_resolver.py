from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


class PlexResolverError(RuntimeError):
    """Base class for Plex resolver failures."""


class PlexConfigurationError(PlexResolverError):
    """Raised when required Plex connection settings are missing."""


class PlexApiError(PlexResolverError):
    """Raised when Plex returns an HTTP or JSON error."""


class PlexNotFoundError(PlexResolverError):
    """Raised when Plex has no matching media item."""


@dataclass(frozen=True)
class PlexConnection:
    base_url: str
    token: str
    timeout: float = 20.0

    @classmethod
    def from_values(cls, base_url: Optional[str], token: Optional[str], timeout: float = 20.0) -> "PlexConnection":
        if not base_url:
            raise PlexConfigurationError("PLEX_BASE_URL is required; set PLEX_BASE_URL or pass --plex-base-url")
        if not token:
            raise PlexConfigurationError("PLEX_TOKEN is required; set PLEX_TOKEN or pass --plex-token")
        return cls(base_url=base_url.rstrip("/"), token=token, timeout=timeout)

    @classmethod
    def from_env(cls) -> "PlexConnection":
        return cls.from_values(os.environ.get("PLEX_BASE_URL"), os.environ.get("PLEX_TOKEN"))


@dataclass(frozen=True)
class PathMapping:
    plex_path_prefix: Optional[str] = None
    local_path_prefix: Optional[str] = None

    @classmethod
    def from_values(cls, plex_path_prefix: Optional[str], local_path_prefix: Optional[str]) -> "PathMapping":
        if bool(plex_path_prefix) != bool(local_path_prefix):
            raise PlexConfigurationError(
                "path prefix mapping requires both MPILOT_PLEX_PATH_PREFIX and "
                "MPILOT_LOCAL_PATH_PREFIX, both BABELARR_PLEX_PATH_PREFIX and "
                "BABELARR_LOCAL_PATH_PREFIX, or both --plex-path-prefix and --local-path-prefix"
            )
        return cls(plex_path_prefix=plex_path_prefix, local_path_prefix=local_path_prefix)

    @classmethod
    def from_env(cls) -> "PathMapping":
        return cls.from_values(
            _env_first("MPILOT_PLEX_PATH_PREFIX", "BABELARR_PLEX_PATH_PREFIX", "MST_PLEX_PATH_PREFIX"),
            _env_first("MPILOT_LOCAL_PATH_PREFIX", "BABELARR_LOCAL_PATH_PREFIX", "MST_LOCAL_PATH_PREFIX"),
        )

    def map(self, plex_file: str) -> Tuple[str, bool]:
        if not self.plex_path_prefix or not self.local_path_prefix:
            return plex_file, False
        plex_prefix = _strip_trailing_slash(self.plex_path_prefix)
        local_prefix = _strip_trailing_slash(self.local_path_prefix)
        if plex_file == plex_prefix:
            return local_prefix, True
        if plex_file.startswith(plex_prefix + "/"):
            return local_prefix + plex_file[len(plex_prefix) :], True
        return plex_file, False


@dataclass(frozen=True)
class PlexResolvedMedia:
    rating_key: str
    title: str
    media_type: str
    plex_file: str
    local_file: str
    path_mapping_applied: bool
    guid: Optional[str] = None
    guids: Tuple[str, ...] = ()
    imdb: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    show_title: Optional[str] = None
    library_section_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "ratingKey": self.rating_key,
            "title": self.title,
            "media_type": self.media_type,
            "plex_file": self.plex_file,
            "local_file": self.local_file,
            "path_mapping_applied": self.path_mapping_applied,
        }
        if self.guid:
            data["guid"] = self.guid
        if self.guids:
            data["guids"] = list(self.guids)
        if self.imdb:
            data["imdb"] = self.imdb
        if self.season is not None:
            data["season"] = self.season
        if self.episode is not None:
            data["episode"] = self.episode
        if self.show_title:
            data["show_title"] = self.show_title
        if self.library_section_id:
            data["librarySectionID"] = self.library_section_id
        return data


class PlexApiClient:
    def __init__(self, connection: PlexConnection):
        self.connection = connection

    def get_metadata(self, rating_key: str) -> List[Dict[str, Any]]:
        return metadata_items(self.get_json("/library/metadata/%s" % _quote_path_component(rating_key)))

    def search(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        payload = self.get_json("/hubs/search", {"query": query, "limit": str(limit)})
        return search_metadata_items(payload)

    def list_sections(self) -> List[Dict[str, Any]]:
        payload = self.get_json("/library/sections")
        container = payload.get("MediaContainer") or {}
        return _as_list(container.get("Directory"))

    def section_items_by_guid(self, section_key: str, guid: str) -> List[Dict[str, Any]]:
        return metadata_items(
            self.get_json(
                "/library/sections/%s/all" % _quote_path_component(section_key),
                {"guid": guid, "includeGuids": "1"},
            )
        )

    def section_items_with_guids(self, section_key: str) -> List[Dict[str, Any]]:
        return metadata_items(
            self.get_json(
                "/library/sections/%s/all" % _quote_path_component(section_key),
                {"includeGuids": "1"},
            )
        )

    def get_all_leaves(self, rating_key: str) -> List[Dict[str, Any]]:
        return metadata_items(self.get_json("/library/metadata/%s/allLeaves" % _quote_path_component(rating_key)))

    def scan_library_path(self, section_key: str, path: str) -> Dict[str, Any]:
        self.request(
            "/library/sections/%s/refresh" % _quote_path_component(section_key),
            {"path": path},
        )
        return {
            "status": "requested",
            "method": "library-section-path-scan",
            "library_section_id": str(section_key),
            "path": path,
        }

    def get_json(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        body = self.request(path, params)
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise PlexApiError("Plex API returned non-JSON response for %s" % path) from error

    def request(self, path: str, params: Optional[Dict[str, str]] = None) -> str:
        query = dict(params or {})
        url = self.connection.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "X-Plex-Token": self.connection.token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.connection.timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace").strip()
            message = "Plex API request failed for %s: HTTP %s" % (path, error.code)
            if detail:
                message += " %s" % detail
            raise PlexApiError(message) from error
        except urllib.error.URLError as error:
            raise PlexApiError("Plex API request failed for %s: %s" % (path, error.reason)) from error


class PlexResolver:
    def __init__(self, client: Any, path_mapping: Optional[PathMapping] = None):
        self.client = client
        self.path_mapping = path_mapping or PathMapping()

    def resolve(
        self,
        imdb: Optional[str] = None,
        rating_key: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> PlexResolvedMedia:
        if bool(imdb) == bool(rating_key):
            raise ValueError("pass exactly one of --imdb or --rating-key")
        if (season is None) != (episode is None):
            raise ValueError("pass both --season and --episode for TV episodes")
        if rating_key:
            item = self._metadata_by_rating_key(rating_key)
            return self._resolve_from_item(item, season=season, episode=episode)
        return self._resolve_from_imdb(imdb or "", season=season, episode=episode)

    def search_by_title(
        self,
        query: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        year: Optional[int] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        if not query.strip():
            raise ValueError("query is required")
        if (season is None) != (episode is None):
            raise ValueError("pass both --season and --episode for TV episodes")

        playable: List[Dict[str, Any]] = []
        needs_episode: List[Dict[str, Any]] = []
        seen = set()
        search_limit = max(1, int(limit))

        for item in self.client.search(query, limit=search_limit):
            detail = self._hydrate(item)
            rating_key = metadata_rating_key(detail)
            if not rating_key or rating_key in seen:
                continue
            if year is not None and _metadata_year(detail) != year:
                continue
            seen.add(rating_key)

            item_type = str(detail.get("type") or "")
            if item_type == "show":
                if season is not None and episode is not None:
                    episode_item = self._episode_from_show(detail, season, episode)
                    if episode_item is not None:
                        playable.append(self._to_result(episode_item).to_dict())
                else:
                    needs_episode.append(_show_candidate(detail))
                continue

            if season is not None and episode is not None:
                if item_type != "episode" or not episode_matches(detail, season, episode):
                    continue
            if item_type not in {"movie", "episode"}:
                continue
            if not primary_media_file(detail):
                continue
            playable.append(self._to_result(detail).to_dict())

        matches = playable if playable else needs_episode
        if playable:
            status = "single_match" if len(playable) == 1 else "multiple_matches"
        elif needs_episode:
            status = "needs_episode"
        else:
            status = "no_match"
        return {
            "status": status,
            "query": query,
            "match_count": len(matches),
            "matches": matches,
        }

    def _resolve_from_imdb(self, imdb: str, season: Optional[int], episode: Optional[int]) -> PlexResolvedMedia:
        imdb_id = normalize_imdb_id(imdb)
        guid = imdb_guid(imdb_id)
        candidates = self._guid_candidates(guid, imdb_id)
        if not candidates:
            raise PlexNotFoundError("No Plex item found for IMDb ID %s" % imdb_id)

        if season is not None and episode is not None:
            for candidate in candidates:
                candidate_type = str(candidate.get("type", ""))
                if candidate_type == "episode" and episode_matches(candidate, season, episode):
                    return self._to_result(candidate, resolved_imdb=imdb_id)
                if candidate_type == "show":
                    episode_item = self._episode_from_show(candidate, season, episode)
                    if episode_item is not None:
                        return self._to_result(episode_item, resolved_imdb=imdb_id)
            raise PlexNotFoundError("No Plex episode found for IMDb ID %s season %s episode %s" % (imdb_id, season, episode))

        for candidate in candidates:
            if primary_media_file(candidate):
                return self._to_result(candidate, resolved_imdb=imdb_id)
        if any(str(candidate.get("type", "")) == "show" for candidate in candidates):
            raise PlexNotFoundError("Plex IMDb ID %s resolved to a show; pass --season and --episode to resolve an episode" % imdb_id)
        raise PlexNotFoundError("No Plex media file found for IMDb ID %s" % imdb_id)

    def _resolve_from_item(self, item: Dict[str, Any], season: Optional[int], episode: Optional[int]) -> PlexResolvedMedia:
        item_type = str(item.get("type", ""))
        if season is not None and episode is not None:
            if item_type == "episode":
                if not episode_matches(item, season, episode):
                    raise PlexNotFoundError(
                        "Plex ratingKey %s is not season %s episode %s" % (metadata_rating_key(item), season, episode)
                    )
                return self._to_result(item)
            if item_type == "show":
                episode_item = self._episode_from_show(item, season, episode)
                if episode_item is not None:
                    return self._to_result(episode_item)
                raise PlexNotFoundError(
                    "No Plex episode found under ratingKey %s for season %s episode %s"
                    % (metadata_rating_key(item), season, episode)
                )
        return self._to_result(item)

    def _episode_from_show(self, show_item: Dict[str, Any], season: int, episode: int) -> Optional[Dict[str, Any]]:
        show_rating_key = metadata_rating_key(show_item)
        if not show_rating_key:
            return None
        for leaf in self.client.get_all_leaves(show_rating_key):
            if episode_matches(leaf, season, episode):
                return self._hydrate(leaf)
        return None

    def _guid_candidates(self, guid: str, query: str) -> List[Dict[str, Any]]:
        candidates = self._matching_guid_details(self.client.search(query, limit=50), guid)
        if candidates:
            return candidates

        raw_candidates: List[Dict[str, Any]] = []
        section_keys: List[str] = []
        for section in self.client.list_sections():
            if str(section.get("type", "")) not in {"movie", "show"}:
                continue
            section_key = str(section.get("key", ""))
            if not section_key:
                continue
            section_keys.append(section_key)
            raw_candidates.extend(self.client.section_items_by_guid(section_key, guid))
        candidates = self._matching_guid_details(raw_candidates, guid)
        if candidates:
            return candidates
        scan_candidates: List[Dict[str, Any]] = []
        for section_key in section_keys:
            scan_candidates.extend(self.client.section_items_with_guids(section_key))
        # section_items_with_guids fetches includeGuids=1 so GUIDs are already present;
        # filter before hydrating to avoid one HTTP request per library item.
        pre_filtered = [item for item in dedupe_metadata(scan_candidates) if metadata_matches_guid(item, guid)]
        return dedupe_metadata([self._hydrate(item) for item in pre_filtered])

    def _matching_guid_details(self, items: Iterable[Dict[str, Any]], guid: str) -> List[Dict[str, Any]]:
        candidates = []
        for item in dedupe_metadata(items):
            detail = self._hydrate(item)
            if metadata_matches_guid(detail, guid):
                candidates.append(detail)
        return dedupe_metadata(candidates)

    def _metadata_by_rating_key(self, rating_key: str) -> Dict[str, Any]:
        items = self.client.get_metadata(str(rating_key))
        if not items:
            raise PlexNotFoundError("No Plex item found for ratingKey %s" % rating_key)
        return items[0]

    def _hydrate(self, item: Dict[str, Any]) -> Dict[str, Any]:
        rating_key = metadata_rating_key(item)
        if not rating_key:
            return item
        items = self.client.get_metadata(rating_key)
        return items[0] if items else item

    def _to_result(self, item: Dict[str, Any], resolved_imdb: Optional[str] = None) -> PlexResolvedMedia:
        rating_key = metadata_rating_key(item)
        if not rating_key:
            raise PlexResolverError("Plex item has no ratingKey")
        plex_file = primary_media_file(item)
        if not plex_file:
            raise PlexResolverError("Plex item %s has no Media Part file" % rating_key)
        local_file, mapping_applied = self.path_mapping.map(plex_file)
        return PlexResolvedMedia(
            rating_key=rating_key,
            title=str(item.get("title") or ""),
            media_type=str(item.get("type") or ""),
            plex_file=plex_file,
            local_file=local_file,
            path_mapping_applied=mapping_applied,
            guid=item.get("guid"),
            guids=tuple(guid_ids(item)),
            imdb=imdb_id_from_metadata(item) or resolved_imdb,
            season=_optional_int(item.get("parentIndex")),
            episode=_optional_int(item.get("index")) if str(item.get("type", "")) == "episode" else None,
            show_title=item.get("grandparentTitle") or item.get("parentTitle"),
            library_section_id=_optional_str(item.get("librarySectionID")),
        )


def metadata_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    container = payload.get("MediaContainer") or payload
    return _as_list(container.get("Metadata"))


def search_metadata_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    container = payload.get("MediaContainer") or payload
    items = list(_as_list(container.get("Metadata")))
    for hub in _as_list(container.get("Hub")):
        items.extend(_as_list(hub.get("Metadata")))
    return dedupe_metadata(items)


def dedupe_metadata(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped = []
    seen = set()
    for item in items:
        key = metadata_rating_key(item) or item.get("key") or id(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def metadata_rating_key(item: Dict[str, Any]) -> Optional[str]:
    value = item.get("ratingKey")
    if value is not None:
        return str(value)
    key = item.get("key")
    if isinstance(key, str):
        match = re.search(r"/library/metadata/([^/?]+)", key)
        if match:
            return match.group(1)
    return None


def primary_media_file(item: Dict[str, Any]) -> Optional[str]:
    direct_file = item.get("file")
    if direct_file:
        return str(direct_file)
    for media in _as_list(item.get("Media")):
        for part in _as_list(media.get("Part")):
            file_value = part.get("file")
            if file_value:
                return str(file_value)
    return None


def guid_ids(item: Dict[str, Any]) -> List[str]:
    values = []
    guid = item.get("guid")
    if isinstance(guid, str):
        values.append(guid)
    for guid_item in _as_list(item.get("Guid")):
        values.extend(_string_values(guid_item.get("id")))
    deduped = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def metadata_matches_guid(item: Dict[str, Any], guid: str) -> bool:
    wanted = guid.lower()
    return any(value.lower() == wanted for value in guid_ids(item))


def imdb_id_from_metadata(item: Dict[str, Any]) -> Optional[str]:
    for guid in guid_ids(item):
        match = re.search(r"imdb://(tt\d+)", guid, flags=re.IGNORECASE)
        if match:
            return match.group(1).lower()
    return None


def normalize_imdb_id(imdb: str) -> str:
    match = re.search(r"(tt\d{6,})", imdb.strip(), flags=re.IGNORECASE)
    if not match:
        raise ValueError("IMDb ID must look like tt1234567")
    return match.group(1).lower()


def imdb_guid(imdb: str) -> str:
    return "imdb://%s" % normalize_imdb_id(imdb)


def episode_matches(item: Dict[str, Any], season: int, episode: int) -> bool:
    return _optional_int(item.get("parentIndex")) == season and _optional_int(item.get("index")) == episode


def _show_candidate(item: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "ratingKey": metadata_rating_key(item),
        "title": str(item.get("title") or ""),
        "media_type": "show",
        "requires_episode": True,
    }
    imdb = imdb_id_from_metadata(item)
    if imdb:
        data["imdb"] = imdb
    year = _metadata_year(item)
    if year is not None:
        data["year"] = year
    return data


def _metadata_year(item: Dict[str, Any]) -> Optional[int]:
    for key in ("year", "parentYear", "originallyAvailableAt"):
        value = item.get(key)
        if key == "originallyAvailableAt" and isinstance(value, str):
            value = value[:4]
        parsed = _optional_int(value)
        if parsed is not None:
            return parsed
    return None


def _as_list(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _string_values(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_string_values(item))
        return values
    if isinstance(value, dict):
        values = []
        for item in value.values():
            values.extend(_string_values(item))
        return values
    return []


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _strip_trailing_slash(value: str) -> str:
    return value.rstrip("/") if value != "/" else value


def _env_first(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return None


def _quote_path_component(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")
