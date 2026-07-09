from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


SUPPORTED_DOWNLOAD_SCHEMES = {"http", "https", "magnet", "bc"}


def normalize_download_link(download_link: str) -> str:
    link = download_link.strip()
    if not link:
        raise ValueError("download_link must not be empty")

    scheme = urlparse(link).scheme.lower()
    if scheme not in SUPPORTED_DOWNLOAD_SCHEMES:
        raise ValueError("download_link must use http, https, magnet, or bc scheme")

    return link


def normalize_optional_save_path(save_path: str | None) -> str | None:
    if save_path is None:
        return None
    path = save_path.strip()
    return path or None


def normalize_optional_user_id(user_id: str | None) -> str | None:
    if user_id is None:
        return None
    normalized = user_id.strip()
    return normalized or None


def normalize_optional_query_id(query_id: str | None) -> str | None:
    if query_id is None:
        return None
    normalized = query_id.strip()
    return normalized or None


class SearchRequest(BaseModel):
    identifier: str | None = Field(default=None, description="Optional media ID")
    query: str | None = Field(default=None, description="Optional search keywords")
    categories: list[int] | None = Field(default=None, description="Optional Prowlarr category IDs")
    indexer_ids: list[int] | None = Field(default=None, description="Optional Prowlarr indexer IDs")


class SearchResult(BaseModel):
    title: str
    download_link: str
    size: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    grabs: int | None = None
    indexer: str | None = None
    protocol: str | None = None
    publish_date: str | None = None
    info_hash: str | None = None


class DownloadRequest(BaseModel):
    download_link: str
    save_path: str | None = Field(default=None, description="Optional qBittorrent save path override")
    query_id: str | None = Field(
        default=None,
        description="Optional saved query ID from qbitlarr_handle for context-aware manual downloads",
    )
    user_id: str | None = Field(
        default=None,
        description="Optional requester identifier used for multi-user torrent tagging",
    )

    @field_validator("download_link")
    @classmethod
    def validate_download_link(cls, value: str) -> str:
        return normalize_download_link(value)

    @field_validator("save_path")
    @classmethod
    def validate_save_path(cls, value: str | None) -> str | None:
        return normalize_optional_save_path(value)

    @field_validator("query_id")
    @classmethod
    def validate_query_id(cls, value: str | None) -> str | None:
        return normalize_optional_query_id(value)

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: str | None) -> str | None:
        return normalize_optional_user_id(value)


class TorrentStatus(BaseModel):
    name: str
    state: str
    progress: float
    size: int
    seeds: int
    hash: str
    download_speed: int | None = None
    eta: int | None = None
    content_path: str | None = None


class DynamicProgressWatchPolicy(BaseModel):
    mode: Literal["bounded_edit_loop"] = "bounded_edit_loop"
    max_duration_seconds: int
    update_interval_seconds: int
    min_progress_delta: float
    completion_notifications_are_separate: bool
    timeout_message: str


class DownloadControlButton(BaseModel):
    text: str
    callback_data: str


class RenderedDownloadsStatusResponse(BaseModel):
    message: str
    watch_policy: DynamicProgressWatchPolicy
    downloads: list[TorrentStatus]


class RenderedDownloadStatusResponse(BaseModel):
    message: str
    watch_policy: DynamicProgressWatchPolicy
    download: TorrentStatus
    buttons: list[DownloadControlButton] = Field(default_factory=list)


class DownloadControlResponse(BaseModel):
    status: Literal["success"] = "success"
    action: Literal["pause", "resume", "delete"]
    download: TorrentStatus


class DownloadResponse(BaseModel):
    status: Literal["success"] = "success"
    message: str = "Download queued"
    imdb_id: str | None = Field(default=None, description="Canonical IMDb ID when known from query context")
    media_type: Literal["movie", "tv"] | None = Field(default=None, description="Inferred media type when known")
    download_status: TorrentStatus | None = None
    rendered_status: str | None = Field(
        default=None,
        description=(
            "Pre-rendered chat-ready progress card for the queued torrent. Send "
            "this string verbatim as a separate status message; do not rebuild "
            "the bar from download_status fields. The 10-cell emoji bar updates "
            "via qbitlarr_render_download_status."
        ),
    )
    rendered_status_buttons: list[DownloadControlButton] = Field(
        default_factory=list,
        description=(
            "Optional Telegram inline button specs for rendered_status. When "
            "present, send them with the status message without modifying "
            "callback_data."
        ),
    )


HandleMode = Literal["auto", "manual", "confirm"]


class HandleRequest(BaseModel):
    user_message: str = Field(description="Natural-language title, IMDb ID, IMDb URL, or search phrase")
    user_id: str | None = Field(default=None, description="Optional caller/user identifier")
    save_path: str | None = Field(default=None, description="Optional qBittorrent save path override")
    mode: HandleMode | None = Field(
        default=None,
        description=(
            "Output mode. 'auto' (default) downloads the best release when confident; "
            "'manual' always returns ranked results without downloading; "
            "'confirm' returns the top pick plus alternatives without downloading. "
            "When omitted, the server uses QBITLARR_DEFAULT_MODE."
        ),
    )

    @field_validator("user_message")
    @classmethod
    def validate_user_message(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("user_message must not be empty")
        return message

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: str | None) -> str | None:
        return normalize_optional_user_id(value)

    @field_validator("save_path")
    @classmethod
    def validate_save_path(cls, value: str | None) -> str | None:
        return normalize_optional_save_path(value)

    @field_validator("mode", mode="before")
    @classmethod
    def validate_mode(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        if normalized not in ("auto", "manual", "confirm"):
            raise ValueError("mode must be 'auto', 'manual', or 'confirm'")
        return normalized


class ManualSearchResult(BaseModel):
    index: int
    title: str
    quality: str
    seeders: int | None = None
    size: int | None = None
    download_link: str
    label: str | None = Field(
        default=None,
        description=(
            "Chat-ready one-line display label. For IMDb-resolved searches this is a "
            "compact differentiator (e.g. 'WEB-DL · H.265') because every result is "
            "the same film; for keyword searches it keeps the full release title."
        ),
    )


class MovieCandidate(BaseModel):
    index: int
    title: str
    year: int | None = None
    imdb_id: str
    label: str = Field(
        description=(
            "Chat-ready one-line label such as 'The Hitch-Hiker (1953)'. Render each "
            "candidate as a pick option; when the user picks one, call qbitlarr_handle "
            "again with its imdb_id to get the release choices."
        ),
    )


class ChoiceButton(BaseModel):
    index: int = Field(description="One-based choice index that maps to a result or candidate index")
    text: str = Field(description="Short button label for chat UIs, usually the numeric choice")
    value: str = Field(description="Opaque value to send back when this choice is selected")


class ChoiceUiHints(BaseModel):
    choice_style: Literal["hermes-default", "telegram-rich"] = Field(
        description="Display profile requested by qBitlarr configuration."
    )
    recommended_button_layout: Literal["vertical", "inline-row"] = Field(
        description="Suggested button layout for hosts that can control inline keyboards."
    )
    closed_choice: bool = Field(
        description="Whether callers should present only the supplied choices instead of a free-form picker."
    )


class ChoiceRichMessage(BaseModel):
    format: Literal["telegram-html"] = Field(
        description="Rich-message format. Pass html as InputRichMessage.html for Telegram sendRichMessage."
    )
    html: str = Field(description="Telegram rich message HTML for the formatted release choices.")
    skip_entity_detection: bool = Field(
        default=True,
        description="Pass through to InputRichMessage.skip_entity_detection to keep table text stable."
    )


class HandleResponse(BaseModel):
    status: Literal["success", "not_found"]
    action: Literal["auto_download", "show_results", "confirm", "choose_title", "needs_imdb"]
    message: str
    choices_table: str | None = Field(
        default=None,
        description=(
            "Pre-rendered monospace choice table for release choices or title "
            "candidates. Send verbatim inside a monospace block; do not re-format. "
            "Recommendation, if any, should be conveyed by the surrounding UI. "
            "Omitted for telegram-rich responses so adapters do not render the "
            "same numbered list twice."
        ),
    )
    choice_display: str | None = Field(
        default=None,
        description=(
            "Complete formatted choice message. Send this field alone as the text "
            "fallback; do not append choices_table, results, or labels to it. "
            "hermes-default uses a Markdown fenced table, while telegram-rich uses "
            "plain text without Markdown fences."
        ),
    )
    choice_buttons: list[ChoiceButton] | None = Field(
        default=None,
        description=(
            "Closed-choice button metadata. Values are numeric indexes, never download links "
            "or private IDs; map them back to the matching result or candidate."
        ),
    )
    ui_hints: ChoiceUiHints | None = Field(
        default=None,
        description="Rendering hints for adapters that can customize button layout."
    )
    choice_rich_message: ChoiceRichMessage | None = Field(
        default=None,
        description=(
            "Telegram-rich formatted choices. Hosts that support Telegram Bot API "
            "sendRichMessage can pass html as rich_message.html and render choice_buttons below it."
        ),
    )
    query_id: str | None = None
    snapshot_status: str | None = None
    imdb_id: str | None = Field(default=None, description="Canonical IMDb ID when the request resolved to one")
    media_type: Literal["movie", "tv"] | None = Field(default=None, description="Inferred media type when known")
    title: str | None = None
    quality: str | None = None
    download_status: TorrentStatus | None = None
    results: list[ManualSearchResult] | None = None
    candidates: list[MovieCandidate] | None = Field(
        default=None,
        description=(
            "Populated on the 'choose_title' action when a keyword matched several "
            "movies/shows. Ask the user which one they mean, then re-call "
            "qbitlarr_handle with the chosen candidate's imdb_id."
        ),
    )
    alternatives: list[ManualSearchResult] | None = Field(
        default=None,
        description=(
            "Ranked runner-up releases. Populated on auto_download and confirm actions "
            "so callers can offer alternatives without a second query lookup."
        ),
    )


class QuerySnapshotEntry(BaseModel):
    version: int
    reason: str
    created_at: str
    results: list[SearchResult]


class QuerySnapshot(BaseModel):
    query_id: str
    status: str
    created_at: str
    updated_at: str
    request: dict
    snapshots: list[QuerySnapshotEntry]


class ProwlarrIndexer(BaseModel):
    id: int
    name: str | None = None
    enabled: bool | None = None
    protocol: str | None = None
