from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from mpilot.acquisition.env import env_first
from mpilot.acquisition.domain.quality import (
    DEFAULT_PREFER_CODEC,
    DEFAULT_PREFER_RESOLUTION,
    DEFAULT_PREFER_SOURCE,
    MIN_AUTO_DOWNLOAD_SEEDERS,
    QualityPreferences,
)
from mpilot.acquisition.exceptions import ConfigurationError


_RETENTION_ACTIONS = {
    "stop": "Stop",
    "remove": "Remove",
    "removewithcontent": "RemoveWithContent",
    "enablesuperseeding": "EnableSuperSeeding",
}

_CHOICE_STYLES = {"hermes-default", "telegram-rich"}


@dataclass(frozen=True)
class Settings:
    prowlarr_url: str
    prowlarr_download_url: str | None
    prowlarr_api_key: str
    qbit_url: str
    qbit_username: str
    qbit_password: str
    request_timeout_seconds: float = 30.0
    query_snapshot_dir: str = "data/query-snapshots"
    prowlarr_primary_indexer_ids: list[int] | None = None
    prowlarr_fallback_indexer_ids: list[int] | None = None
    prowlarr_imdb_native_indexer_ids: list[int] | None = None
    prowlarr_imdb_keyword_indexer_ids: list[int] | None = None
    prowlarr_imdb_disabled_indexer_ids: list[int] | None = None
    qbitlarr_api_key: str | None = None
    qbitlarr_save_path_movie: str = "/downloads/movies"
    qbitlarr_save_path_movie_4k: str = "/downloads/movies-4k"
    qbitlarr_save_path_tv: str = "/downloads/tv"
    qbitlarr_extra_save_paths: list[str] | None = None
    prefer_resolution: str = DEFAULT_PREFER_RESOLUTION
    prefer_source: str = DEFAULT_PREFER_SOURCE
    prefer_codec: str = DEFAULT_PREFER_CODEC
    min_seeders: int = MIN_AUTO_DOWNLOAD_SEEDERS
    default_mode: str = "manual"
    manual_result_limit: int = 4
    choice_style: str = "hermes-default"
    retention_enabled: bool = False
    retention_ratio_limit: float | None = 2.0
    retention_seeding_time_limit_minutes: int | None = 10080
    retention_action: str = "Remove"
    cleanup_enabled: bool = False
    cleanup_completed_after_seconds: int = 259_200
    cleanup_interval_seconds: int = 21_600
    cleanup_include_legacy_requester_tags: bool = True
    query_snapshot_retention_seconds: int = 604_800

    def __post_init__(self) -> None:
        groups = {
            "native": set(self.prowlarr_imdb_native_indexer_ids or []),
            "keyword": set(self.prowlarr_imdb_keyword_indexer_ids or []),
            "disabled": set(self.prowlarr_imdb_disabled_indexer_ids or []),
        }
        overlaps = {
            indexer_id
            for mode, ids in groups.items()
            for other_mode, other_ids in groups.items()
            if mode < other_mode
            for indexer_id in ids & other_ids
        }
        if overlaps:
            joined = ", ".join(str(indexer_id) for indexer_id in sorted(overlaps))
            raise ConfigurationError(f"IMDb indexer IDs must belong to only one search mode: {joined}")

    @property
    def imdb_indexer_routing_configured(self) -> bool:
        return any(
            value is not None
            for value in (
                self.prowlarr_imdb_native_indexer_ids,
                self.prowlarr_imdb_keyword_indexer_ids,
                self.prowlarr_imdb_disabled_indexer_ids,
            )
        )

    @property
    def quality_preferences(self) -> QualityPreferences:
        return QualityPreferences(
            resolution=self.prefer_resolution,
            source=self.prefer_source,
            codec=self.prefer_codec,
            min_seeders=self.min_seeders,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            prowlarr_url=_required_env("PROWLARR_URL").rstrip("/"),
            prowlarr_download_url=_optional_env("PROWLARR_DOWNLOAD_URL"),
            prowlarr_api_key=_required_env("PROWLARR_API_KEY"),
            qbit_url=_required_env("QBIT_URL").rstrip("/"),
            qbit_username=_required_env("QBIT_USERNAME"),
            qbit_password=_required_env("QBIT_PASSWORD"),
            request_timeout_seconds=float(_env_with_default("REQUEST_TIMEOUT_SECONDS", "30")),
            query_snapshot_dir=_env_with_default("QBITLARR_QUERY_SNAPSHOT_DIR", "data/query-snapshots"),
            prowlarr_primary_indexer_ids=_optional_int_list("PROWLARR_PRIMARY_INDEXER_IDS"),
            prowlarr_fallback_indexer_ids=_optional_int_list("PROWLARR_FALLBACK_INDEXER_IDS"),
            prowlarr_imdb_native_indexer_ids=_optional_int_list("PROWLARR_IMDB_NATIVE_INDEXER_IDS"),
            prowlarr_imdb_keyword_indexer_ids=_optional_int_list("PROWLARR_IMDB_KEYWORD_INDEXER_IDS"),
            prowlarr_imdb_disabled_indexer_ids=_optional_int_list("PROWLARR_IMDB_DISABLED_INDEXER_IDS"),
            qbitlarr_api_key=_optional_str_env("QBITLARR_API_KEY"),
            qbitlarr_save_path_movie=_env_with_default("QBITLARR_SAVE_PATH_MOVIE", "/downloads/movies"),
            qbitlarr_save_path_movie_4k=_env_with_default("QBITLARR_SAVE_PATH_MOVIE_4K", "/downloads/movies-4k"),
            qbitlarr_save_path_tv=_env_with_default("QBITLARR_SAVE_PATH_TV", "/downloads/tv"),
            qbitlarr_extra_save_paths=_optional_str_list("QBITLARR_EXTRA_SAVE_PATHS"),
            prefer_resolution=_env_with_default("QBITLARR_PREFER_RESOLUTION", DEFAULT_PREFER_RESOLUTION),
            prefer_source=_env_with_default("QBITLARR_PREFER_SOURCE", DEFAULT_PREFER_SOURCE),
            prefer_codec=_env_with_default("QBITLARR_PREFER_CODEC", DEFAULT_PREFER_CODEC),
            min_seeders=int(_env_with_default("QBITLARR_MIN_SEEDERS", str(MIN_AUTO_DOWNLOAD_SEEDERS))),
            default_mode=_env_with_default("QBITLARR_DEFAULT_MODE", "manual").lower(),
            manual_result_limit=_bounded_int_env("QBITLARR_MANUAL_RESULT_LIMIT", default=4, minimum=1, maximum=10),
            choice_style=_choice_style_env("QBITLARR_CHOICE_STYLE", "hermes-default"),
            retention_enabled=_env_bool("QBITLARR_RETENTION_ENABLED", False),
            retention_ratio_limit=_optional_float_env("QBITLARR_RETENTION_RATIO_LIMIT", default=2.0),
            retention_seeding_time_limit_minutes=_optional_int_env(
                "QBITLARR_RETENTION_SEEDING_TIME_LIMIT_MINUTES",
                default=10080,
            ),
            retention_action=_retention_action_env("QBITLARR_RETENTION_ACTION", "Remove"),
            cleanup_enabled=_env_bool("QBITLARR_CLEANUP_ENABLED", False),
            cleanup_completed_after_seconds=_optional_int_env(
                "QBITLARR_CLEANUP_COMPLETED_AFTER_SECONDS",
                default=259_200,
            )
            or 259_200,
            cleanup_interval_seconds=_optional_int_env("QBITLARR_CLEANUP_INTERVAL_SECONDS", default=21_600)
            or 21_600,
            cleanup_include_legacy_requester_tags=_env_bool("QBITLARR_CLEANUP_INCLUDE_LEGACY_REQUESTER_TAGS", True),
            query_snapshot_retention_seconds=_optional_int_env(
                "QBITLARR_QUERY_SNAPSHOT_RETENTION_SECONDS",
                default=604_800,
            )
            or 604_800,
        )


def _required_env(name: str) -> str:
    value = env_first(name)
    if value is None or not value.strip():
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value.strip()


def _optional_env(name: str) -> str | None:
    value = env_first(name)
    if value is None or not value.strip():
        return None
    return value.strip().rstrip("/")


def _optional_str_env(name: str) -> str | None:
    value = env_first(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _env_with_default(name: str, default: str) -> str:
    value = env_first(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = env_first(name)
    if value is None or not value.strip():
        return default

    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"Invalid boolean environment variable: {name}")


def _optional_float_env(name: str, *, default: float | None = None) -> float | None:
    value = env_first(name)
    if value is None:
        return default
    if not value.strip():
        return None
    return float(value.strip())


def _optional_int_env(name: str, *, default: int | None = None) -> int | None:
    value = env_first(name)
    if value is None:
        return default
    if not value.strip():
        return None
    return int(value.strip())


def _bounded_int_env(name: str, *, default: int, minimum: int, maximum: int) -> int:
    value = _optional_int_env(name, default=default)
    if value is None:
        value = default
    if value < minimum or value > maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _choice_style_env(name: str, default: str) -> str:
    value = _env_with_default(name, default).strip().lower()
    if value not in _CHOICE_STYLES:
        raise ConfigurationError(f"{name} must be one of: {', '.join(sorted(_CHOICE_STYLES))}")
    return value


def _retention_action_env(name: str, default: str) -> str:
    value = env_first(name)
    if value is None or not value.strip():
        return default

    normalized = value.strip().casefold()
    action = _RETENTION_ACTIONS.get(normalized)
    if action is None:
        raise ConfigurationError(f"Invalid retention action environment variable: {name}")
    return action


def _optional_int_list(name: str) -> list[int] | None:
    value = env_first(name)
    if value is None or not value.strip():
        return None
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def _optional_str_list(name: str) -> list[str] | None:
    value = env_first(name)
    if value is None or not value.strip():
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
