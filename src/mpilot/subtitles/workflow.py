from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .ass import AssOptions, build_ass
from .languages import language_suffixes, language_to_code
from .normalize import normalize_to_srt
from .plex import plex_sidecar_path, subtitle_output_path
from .plex_resolver import PlexResolvedMedia
from .source import AcquisitionResult, AcquisitionStatus, RemoteSubtitleCommandRunner, acquire_source_subtitle, resolve_video_file_path
from .srt import Cue, format_srt, read_srt
from .text import flatten_subtitle_lines
from .translate import TranslationOptions, translate_cues

OnlineSubtitleFetcher = Callable[[PlexResolvedMedia, str, Path, bool], Any]
PlexRefreshHandler = Callable[[PlexResolvedMedia, Path], Dict[str, Any]]
ProgressCallback = Callable[[Dict[str, Any]], None]


@dataclass(frozen=True)
class WorkflowOptions:
    source_language: str
    target_language: str
    output_mode: str = "single-srt"
    backend: str = "codex-cli"
    model: str = "gpt-5.4-mini"
    output: Optional[Path] = None
    force: bool = False
    work_dir: Optional[Path] = None
    keep_work_dir: bool = False
    primary_script: str = "cjk"
    secondary_script: str = "latin"
    ass_height: int = 1080
    openai_base_url: str = "http://127.0.0.1:11434/v1"
    openai_api_key: str = "ollama"
    online_subtitle_fetcher: Optional[OnlineSubtitleFetcher] = None
    write_back: bool = False
    refresh_plex: bool = False
    plex_refresher: Optional[PlexRefreshHandler] = None
    assume_unlabeled_stream_language: bool = False
    progress_callback: Optional[ProgressCallback] = None


@dataclass(frozen=True)
class PreparedOutput:
    requested_path: Path
    render_path: Path
    remote_path: Optional[str] = None
    remote_runner: Optional[RemoteSubtitleCommandRunner] = None


def output_extension_for_mode(output_mode: str) -> str:
    if output_mode == "single-srt":
        return ".srt"
    if output_mode == "bilingual-ass":
        return ".ass"
    raise ValueError("unsupported output mode: %s" % output_mode)


def ensure_can_write(path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError("output already exists: %s" % path)


def _translation_options(options: WorkflowOptions, work_dir: Optional[Path]) -> TranslationOptions:
    return TranslationOptions(
        source_language=options.source_language,
        target_language=options.target_language,
        backend=options.backend,
        model=options.model,
        work_dir=work_dir,
        keep_work_dir=options.keep_work_dir,
        openai_base_url=options.openai_base_url,
        openai_api_key=options.openai_api_key,
        progress_callback=options.progress_callback,
    )


def render_output(source_cues, target_cues, output_path: Path, options: WorkflowOptions) -> None:
    if options.output_mode == "single-srt":
        output_path.write_text(format_srt(target_cues), encoding="utf-8")
        return
    if options.output_mode == "bilingual-ass":
        ass = build_ass(
            source_cues,
            target_cues,
            AssOptions(
                mode="bilingual-ass",
                primary_script=options.primary_script,
                secondary_script=options.secondary_script,
                height=options.ass_height,
            ),
        )
        output_path.write_text(ass, encoding="utf-8")
        return
    raise ValueError("unsupported output mode: %s" % options.output_mode)


def prepare_target_cues_for_output(target_cues: List[Cue], target_language: str) -> List[Cue]:
    if not _is_chinese_target_language(target_language):
        return target_cues
    prepared = []
    for cue in target_cues:
        text = flatten_subtitle_lines(cue.text_lines, clean_terminal=True)
        if not text:
            raise ValueError("cannot format empty translated cue: %s" % cue.number)
        prepared.append(Cue(cue.number, cue.start, cue.end, [text]))
    return prepared


def _is_chinese_target_language(target_language: str) -> bool:
    try:
        return language_to_code(target_language) == "zh"
    except ValueError:
        return False


def translate_srt_file(input_path: Path, options: WorkflowOptions) -> Dict[str, Any]:
    _emit_progress(options, "reading_source_subtitle", "Reading source subtitle.", input=str(input_path))
    source_cues = read_srt(input_path)
    extension = output_extension_for_mode(options.output_mode)
    output_path = options.output or subtitle_output_path(
        input_path,
        language_to_code(options.target_language),
        extension,
        language_suffixes(options.source_language),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_can_write(output_path, options.force)

    work_dir = options.work_dir
    run = translate_cues(source_cues, _translation_options(options, work_dir))
    target_cues = prepare_target_cues_for_output(run.cues, options.target_language)
    _emit_progress(options, "rendering_output", "Rendering translated subtitle.", output=str(output_path))
    render_output(source_cues, target_cues, output_path, options)
    _emit_progress(options, "output_ready", "Translated subtitle output is ready.", output=str(output_path))
    return {
        "input": str(input_path),
        "source_language": options.source_language,
        "target_language": options.target_language,
        "output_mode": options.output_mode,
        "output": str(output_path),
        "translation_summary": run.summary,
    }


def translate_video_file(video_path: Path, options: WorkflowOptions, resolved_media: Optional[PlexResolvedMedia] = None) -> Dict[str, Any]:
    temp_root = None
    if options.work_dir:
        work_dir = options.work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp_root = Path(tempfile.mkdtemp(prefix="babelarr-video."))
        work_dir = temp_root
    try:
        remote_runner = RemoteSubtitleCommandRunner.from_env()
        original_video_path = video_path
        video_path = resolve_video_file_path(video_path, remote_runner=remote_runner)
        if resolved_media is not None and getattr(resolved_media, "local_file", None) != str(video_path):
            try:
                resolved_media = replace(resolved_media, local_file=str(video_path))
            except TypeError:
                setattr(resolved_media, "local_file", str(video_path))
        if video_path != original_video_path:
            _emit_progress(
                options,
                "resolved_video_file",
                "Resolved input directory to video file.",
                input=str(original_video_path),
                video_path=str(video_path),
            )
        extension = output_extension_for_mode(options.output_mode)
        output_path = options.output or plex_sidecar_path(video_path, language_to_code(options.target_language), extension)
        prepared_output = _prepare_video_output(output_path, options, work_dir, remote_runner)

        _emit_progress(
            options,
            "checking_local_subtitles",
            "Checking local sidecar and embedded subtitles.",
            source_language=options.source_language,
            target_language=options.target_language,
            input=str(video_path),
        )
        probe_runner = None
        extractor = None
        executor_name = "local"
        remote_path = None
        if remote_runner is not None and remote_runner.can_handle(video_path):
            probe_runner = remote_runner.probe_subtitle_streams
            extractor = remote_runner.extract_text_subtitle_to_srt
            executor_name = "remote"
            remote_path = remote_runner.remote_path_for(video_path)
            _emit_progress(
                options,
                "using_remote_source_executor",
                "Using remote media host for embedded subtitle inspection.",
                input=str(video_path),
                remote_path=remote_path,
                ssh_host=remote_runner.ssh_host,
            )
        acquisition = acquire_source_subtitle(
            video_path,
            options.source_language,
            work_dir / "source",
            probe_runner=probe_runner,
            extractor=extractor,
            assume_unlabeled_stream_language=options.assume_unlabeled_stream_language,
            progress_callback=options.progress_callback,
            executor_name=executor_name,
            remote_path=remote_path,
        )
        provider_selection = None
        if acquisition.status != AcquisitionStatus.READY or acquisition.path is None:
            _emit_progress(
                options,
                "local_source_missing",
                "No usable local source subtitle was found.",
                source_language=options.source_language,
                status=acquisition.status.value,
                method=acquisition.method,
                reason=acquisition.message,
            )
            if not options.online_subtitle_fetcher or resolved_media is None:
                raise RuntimeError(acquisition.message)
            _emit_progress(
                options,
                "searching_online_subtitles",
                "Searching third-party subtitle providers.",
                source_language=options.source_language,
                target_language=options.target_language,
            )
            provider_selection = options.online_subtitle_fetcher(
                resolved_media,
                options.source_language,
                work_dir / "provider-source",
                options.force,
            )
            provider_path = Path(provider_selection.download.path)
            if not provider_path.exists():
                raise FileNotFoundError("provider subtitle download path does not exist: %s" % provider_path)
            acquisition = AcquisitionResult(
                AcquisitionStatus.READY,
                path=provider_path,
                method="provider:%s" % provider_selection.candidate.provider,
                message="downloaded online provider subtitle",
            )
            _emit_progress(
                options,
                "online_subtitle_selected",
                "Selected a third-party source subtitle.",
                provider=provider_selection.candidate.provider,
                language=provider_selection.candidate.language,
                file_name=provider_selection.candidate.file_name,
                source_language=options.source_language,
            )
        else:
            _emit_progress(
                options,
                "source_subtitle_ready",
                "Found a local source subtitle.",
                source_language=options.source_language,
                method=acquisition.method,
                path=str(acquisition.path),
                execution=acquisition.execution,
            )

        _emit_progress(options, "normalizing_source", "Normalizing source subtitle.", path=str(acquisition.path))
        normalized = normalize_to_srt(acquisition.path, work_dir / "normalize")
        source_cues = normalized.cues

        run = translate_cues(source_cues, _translation_options(options, work_dir / "translation"))
        target_cues = prepare_target_cues_for_output(run.cues, options.target_language)
        _emit_progress(
            options,
            "rendering_output",
            "Rendering translated subtitle.",
            output=str(prepared_output.requested_path),
            render_path=str(prepared_output.render_path) if prepared_output.remote_path else None,
            remote_path=prepared_output.remote_path,
        )
        render_output(source_cues, target_cues, prepared_output.render_path, options)
        if prepared_output.remote_path and prepared_output.remote_runner:
            _emit_progress(
                options,
                "writing_remote_output",
                "Writing subtitle output to the NAS media library.",
                output=str(prepared_output.requested_path),
                remote_path=prepared_output.remote_path,
            )
            prepared_output.remote_runner.copy_file_to_remote(
                prepared_output.render_path,
                prepared_output.requested_path,
                force=options.force,
            )
        _emit_progress(
            options,
            "output_ready",
            "Translated subtitle output is ready.",
            output=str(prepared_output.requested_path),
            remote_path=prepared_output.remote_path,
        )
        source_acquisition: Dict[str, Any] = {
            "status": acquisition.status.value,
            "method": acquisition.method,
            "path": str(acquisition.path),
            "message": acquisition.message,
        }
        if acquisition.execution:
            source_acquisition["execution"] = acquisition.execution
        if acquisition.remote_path:
            source_acquisition["remote_path"] = acquisition.remote_path
        if provider_selection is not None:
            source_acquisition["provider_fallback"] = provider_selection.to_dict()
        if acquisition.warning:
            source_acquisition["warning"] = acquisition.warning

        result = {
            "input": str(video_path),
            "source_language": options.source_language,
            "target_language": options.target_language,
            "source_acquisition": source_acquisition,
            "normalized_source": str(normalized.path),
            "output_mode": options.output_mode,
            "output": str(prepared_output.requested_path),
            "translation_summary": run.summary,
        }
        if prepared_output.remote_path:
            result["output_delivery"] = {
                "execution": "remote",
                "remote_path": prepared_output.remote_path,
                "staged_output": str(prepared_output.render_path),
            }
        return result
    finally:
        if temp_root and not options.keep_work_dir:
            shutil.rmtree(temp_root, ignore_errors=True)


def translate_plex_resolved(resolved_media: PlexResolvedMedia, options: WorkflowOptions) -> Dict[str, Any]:
    _emit_progress(
        options,
        "media_resolved",
        "Resolved Plex item to a media file.",
        title=resolved_media.title,
        media_type=resolved_media.media_type,
        local_file=resolved_media.local_file,
    )
    video_path = Path(resolved_media.local_file)
    remote_runner = RemoteSubtitleCommandRunner.from_env()
    original_video_path = video_path
    video_path = resolve_video_file_path(video_path, remote_runner=remote_runner)
    if not video_path.exists() and (remote_runner is None or not remote_runner.can_handle(video_path)):
        raise FileNotFoundError("resolved Plex local_file does not exist: %s" % video_path)
    if video_path != original_video_path:
        try:
            resolved_media = replace(resolved_media, local_file=str(video_path))
        except TypeError:
            setattr(resolved_media, "local_file", str(video_path))
    if options.refresh_plex and not options.write_back:
        raise ValueError("--refresh-plex requires --write-back")

    extension = output_extension_for_mode(options.output_mode)
    write_back_path = plex_sidecar_path(video_path, language_to_code(options.target_language), extension)
    output_path = options.output
    if output_path is None:
        output_path = _translate_plex_staging_output_path(resolved_media, video_path, options, extension)

    effective_options = replace(options, output=output_path)

    write_back_remote = False
    if options.write_back and output_path is not None and output_path != write_back_path:
        write_back_remote = _ensure_write_back_can_write(write_back_path, options.force, remote_runner)

    translation = translate_video_file(video_path, effective_options, resolved_media=resolved_media)
    write_back = _write_back_summary(options.write_back)
    if options.write_back:
        _emit_progress(options, "writing_back", "Writing subtitle next to the media file.", path=str(write_back_path))
        generated_output = Path(translation["output"])
        if generated_output != write_back_path:
            write_back_remote = write_back_remote or _ensure_write_back_can_write(write_back_path, options.force, remote_runner)
            if write_back_remote:
                assert remote_runner is not None
                remote_runner.copy_file_to_remote(generated_output, write_back_path, force=options.force)
            else:
                shutil.copy2(generated_output, write_back_path)
        write_back = {
            "requested": True,
            "status": "written",
            "path": str(write_back_path),
        }
        if write_back_remote:
            assert remote_runner is not None
            write_back["execution"] = "remote"
            write_back["remote_path"] = remote_runner.remote_path_for(write_back_path)
        _emit_progress(options, "write_back_complete", "Subtitle sidecar was written next to the media file.", path=str(write_back_path))

    plex_refresh = None
    if options.refresh_plex:
        _emit_progress(options, "refreshing_plex", "Refreshing Plex metadata for the media folder.")
        if options.plex_refresher is None:
            plex_refresh = {
                "requested": True,
                "status": "skipped",
                "reason": "no Plex refresh handler was configured",
            }
        else:
            plex_refresh = options.plex_refresher(resolved_media, write_back_path)
        _emit_progress(options, "plex_refresh_complete", "Plex refresh request completed.")

    summary = {
        "plex": resolved_media.to_dict(),
        "input": resolved_media.local_file,
        "source_language": options.source_language,
        "target_language": options.target_language,
        "output_mode": options.output_mode,
        "output": translation["output"],
        "write_back": write_back,
        "translation": translation,
    }
    if plex_refresh is not None:
        summary["plex_refresh"] = plex_refresh
    return summary


def _emit_progress(options: WorkflowOptions, stage: str, message: str, **details: Any) -> None:
    callback = options.progress_callback
    if callback is None:
        return
    payload = {
        "stage": stage,
        "message": message,
        "details": {key: value for key, value in details.items() if value is not None},
    }
    try:
        callback(payload)
    except Exception:
        pass


def _write_back_summary(requested: bool) -> Dict[str, Any]:
    if requested:
        return {"requested": True, "status": "pending"}
    return {
        "requested": False,
        "status": "not_requested",
    }


def _ensure_write_back_can_write(
    path: Path,
    force: bool,
    remote_runner: Optional[RemoteSubtitleCommandRunner],
) -> bool:
    if remote_runner is not None and remote_runner.can_handle(path) and not path.parent.exists():
        remote_runner.ensure_remote_can_write(path, force)
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ensure_can_write(path, force)
        return False
    except FileExistsError:
        raise
    except OSError:
        if remote_runner is None or not remote_runner.can_handle(path):
            raise
        remote_runner.ensure_remote_can_write(path, force)
        return True


def _prepare_video_output(
    output_path: Path,
    options: WorkflowOptions,
    work_dir: Path,
    remote_runner: Optional[RemoteSubtitleCommandRunner],
) -> PreparedOutput:
    remote_path = remote_runner.remote_path_for(output_path) if remote_runner is not None else None
    if remote_runner is not None and remote_path is not None and not output_path.parent.exists():
        return _prepare_remote_video_output(output_path, options, work_dir, remote_runner, remote_path)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_can_write(output_path, options.force)
        return PreparedOutput(requested_path=output_path, render_path=output_path)
    except FileExistsError:
        raise
    except OSError as exc:
        if remote_runner is None or remote_path is None:
            raise
        return _prepare_remote_video_output(output_path, options, work_dir, remote_runner, remote_path, local_error=exc)


def _prepare_remote_video_output(
    output_path: Path,
    options: WorkflowOptions,
    work_dir: Path,
    remote_runner: RemoteSubtitleCommandRunner,
    remote_path: str,
    local_error: Optional[OSError] = None,
) -> PreparedOutput:
    remote_runner.ensure_remote_can_write(output_path, options.force)
    staged_output = work_dir / "remote-output" / output_path.name
    staged_output.parent.mkdir(parents=True, exist_ok=True)
    _emit_progress(
        options,
        "using_remote_output_writer",
        "Using NAS media host for subtitle output.",
        output=str(output_path),
        remote_path=remote_path,
        local_error=str(local_error) if local_error is not None else None,
    )
    return PreparedOutput(
        requested_path=output_path,
        render_path=staged_output,
        remote_path=remote_path,
        remote_runner=remote_runner,
    )


def _translate_plex_staging_output_path(
    resolved_media: PlexResolvedMedia,
    video_path: Path,
    options: WorkflowOptions,
    extension: str,
) -> Path:
    if options.work_dir:
        base_dir = options.work_dir / "output"
    else:
        base_dir = Path(".runtime") / "translate-plex" / _safe_path_segment(resolved_media.rating_key)
    return plex_sidecar_path(base_dir / video_path.name, language_to_code(options.target_language), extension)


def _safe_path_segment(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in value)
    return cleaned.strip("-") or "item"


def summary_json(summary: Dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2)
