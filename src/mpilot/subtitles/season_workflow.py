from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .plex_resolver import PlexResolvedMedia
from .provider_policy import LowConfidenceSubtitleCandidatesError
from .workflow import WorkflowOptions, translate_plex_resolved


EpisodeRunner = Callable[[PlexResolvedMedia, WorkflowOptions], Dict[str, Any]]


def translate_plex_season(
    resolver: Any,
    *,
    imdb: Optional[str],
    rating_key: Optional[str],
    season: int,
    episode_start: int,
    episode_end: int,
    batch_size: int,
    options: WorkflowOptions,
    episode_runner: EpisodeRunner = translate_plex_resolved,
) -> Dict[str, Any]:
    _validate_season_request(
        imdb=imdb,
        rating_key=rating_key,
        season=season,
        episode_start=episode_start,
        episode_end=episode_end,
        batch_size=batch_size,
    )

    requested_episodes = list(range(episode_start, episode_end + 1))
    batch_episodes = requested_episodes[:batch_size]
    initial_deferred = requested_episodes[len(batch_episodes) :]
    jobs = []
    status = "complete" if not initial_deferred else "batch_complete"
    next_batch = _next_batch(initial_deferred, episode_end, batch_size)

    for episode in batch_episodes:
        resolved = None
        try:
            resolved = resolver.resolve(
                imdb=imdb,
                rating_key=rating_key,
                season=season,
                episode=episode,
            )
            episode_options = replace(
                options,
                work_dir=_episode_work_dir(options.work_dir, season, episode),
            )
            result = episode_runner(resolved, episode_options)
        except LowConfidenceSubtitleCandidatesError as error:
            jobs.append(_failed_job(season, episode, resolved, error, status="needs_confirmation"))
            status = "needs_confirmation"
            next_batch = None
            break
        except Exception as error:
            jobs.append(_failed_job(season, episode, resolved, error, status="failed"))
            status = "failed"
            next_batch = None
            break
        jobs.append(
            {
                "season": season,
                "episode": episode,
                "status": "succeeded",
                "plex": resolved.to_dict(),
                "result": result,
            }
        )

    processed_episode_numbers = [job["episode"] for job in jobs if job["status"] == "succeeded"]
    if status in {"failed", "needs_confirmation"}:
        deferred_episodes = [episode for episode in requested_episodes if episode not in processed_episode_numbers]
    else:
        deferred_episodes = initial_deferred

    return {
        "workflow": "translate-plex-season",
        "status": status,
        "serial": True,
        "source_language": options.source_language,
        "target_language": options.target_language,
        "output_mode": options.output_mode,
        "requested": {
            "season": season,
            "episode_start": episode_start,
            "episode_end": episode_end,
            "episodes": requested_episodes,
            "count": len(requested_episodes),
        },
        "batch": {
            "season": season,
            "episode_start": batch_episodes[0],
            "episode_end": batch_episodes[-1],
            "episodes": batch_episodes,
            "limit": batch_size,
        },
        "deferred_episodes": deferred_episodes,
        "next_batch": next_batch,
        "jobs": jobs,
    }


def _validate_season_request(
    *,
    imdb: Optional[str],
    rating_key: Optional[str],
    season: int,
    episode_start: int,
    episode_end: int,
    batch_size: int,
) -> None:
    if bool(imdb) == bool(rating_key):
        raise ValueError("pass exactly one of --imdb or --rating-key")
    if season < 1:
        raise ValueError("season must be >= 1")
    if episode_start < 1 or episode_end < 1:
        raise ValueError("episode-start and episode-end must be >= 1")
    if episode_start > episode_end:
        raise ValueError("episode-start must be <= episode-end")
    if batch_size < 1 or batch_size > 3:
        raise ValueError("batch-size must be between 1 and 3")


def _episode_work_dir(work_dir: Optional[Path], season: int, episode: int) -> Optional[Path]:
    if work_dir is None:
        return None
    return work_dir / ("S%02dE%02d" % (season, episode))


def _next_batch(deferred_episodes, episode_end: int, batch_size: int):
    if not deferred_episodes:
        return None
    return {
        "episode_start": deferred_episodes[0],
        "episode_end": episode_end,
        "batch_size": batch_size,
    }


def _failed_job(season: int, episode: int, resolved, error: Exception, *, status: str) -> Dict[str, Any]:
    job = {
        "season": season,
        "episode": episode,
        "status": status,
        "error": {
            "type": error.__class__.__name__,
            "message": str(error),
        },
    }
    if resolved is not None:
        job["plex"] = resolved.to_dict()
    if isinstance(error, LowConfidenceSubtitleCandidatesError):
        job["proposal"] = error.to_dict()
    return job
