from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from .languages import language_suffixes, language_to_code, normalize_language_label


TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
IMAGE_SUBTITLE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "vobsub", "xsub"}
VIDEO_FILE_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts"}


class AcquisitionStatus(str, Enum):
    READY = "ready"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class SubtitleStream:
    index: int
    codec_name: str
    language: Optional[str] = None
    title: Optional[str] = None

    @property
    def is_text(self) -> bool:
        return self.codec_name.lower() in TEXT_SUBTITLE_CODECS

    @property
    def is_image(self) -> bool:
        return self.codec_name.lower() in IMAGE_SUBTITLE_CODECS


@dataclass(frozen=True)
class AcquisitionResult:
    status: AcquisitionStatus
    path: Optional[Path] = None
    method: Optional[str] = None
    message: str = ""
    stream: Optional[SubtitleStream] = None
    warning: Optional[str] = None
    execution: Optional[str] = None
    remote_path: Optional[str] = None


ProbeRunner = Callable[[Path], List[SubtitleStream]]
Extractor = Callable[[Path, SubtitleStream, Path], None]
ProgressCallback = Callable[[Dict[str, Any]], None]


@dataclass(frozen=True)
class RemoteSubtitleCommandRunner:
    ssh_host: str
    local_path_prefix: str
    remote_path_prefix: str
    ssh_bin: str = "ssh"
    ffprobe: str = "/usr/bin/ffprobe"
    ffmpeg: str = "/usr/bin/ffmpeg"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> Optional["RemoteSubtitleCommandRunner"]:
        values = env if env is not None else os.environ
        ssh_host = _env_first_string(
            values,
            "MPILOT_SOURCE_REMOTE_SSH_HOST",
            "BABELARR_SOURCE_REMOTE_SSH_HOST",
            "MST_SOURCE_REMOTE_SSH_HOST",
            "MST_NAS_SSH_HOST",
        )
        local_prefix = _env_first_string(
            values,
            "MPILOT_SOURCE_LOCAL_PATH_PREFIX",
            "BABELARR_SOURCE_LOCAL_PATH_PREFIX",
            "MST_SOURCE_LOCAL_PATH_PREFIX",
            "MPILOT_LOCAL_PATH_PREFIX",
            "BABELARR_LOCAL_PATH_PREFIX",
            "MST_LOCAL_PATH_PREFIX",
        )
        remote_prefix = _env_first_string(
            values,
            "MPILOT_SOURCE_REMOTE_PATH_PREFIX",
            "BABELARR_SOURCE_REMOTE_PATH_PREFIX",
            "MST_SOURCE_REMOTE_PATH_PREFIX",
            "MPILOT_PLEX_PATH_PREFIX",
            "BABELARR_PLEX_PATH_PREFIX",
            "MST_PLEX_PATH_PREFIX",
        )
        if not ssh_host or not local_prefix or not remote_prefix:
            return None
        return cls(
            ssh_host=ssh_host,
            local_path_prefix=local_prefix,
            remote_path_prefix=remote_prefix,
            ssh_bin=_env_first_string(values, "MPILOT_SOURCE_REMOTE_SSH_BIN", "BABELARR_SOURCE_REMOTE_SSH_BIN", "MST_SOURCE_REMOTE_SSH_BIN") or "ssh",
            ffprobe=_env_first_string(values, "MPILOT_SOURCE_REMOTE_FFPROBE", "BABELARR_SOURCE_REMOTE_FFPROBE", "MST_SOURCE_REMOTE_FFPROBE") or "/usr/bin/ffprobe",
            ffmpeg=_env_first_string(values, "MPILOT_SOURCE_REMOTE_FFMPEG", "BABELARR_SOURCE_REMOTE_FFMPEG", "MST_SOURCE_REMOTE_FFMPEG") or "/usr/bin/ffmpeg",
        )

    def remote_path_for(self, local_path: Path) -> Optional[str]:
        path = str(local_path)
        local_prefix = self.local_path_prefix.rstrip("/")
        remote_prefix = self.remote_path_prefix.rstrip("/")
        if path == local_prefix:
            return remote_prefix
        if path.startswith(local_prefix + "/"):
            return remote_prefix + path[len(local_prefix) :]
        return None

    def local_path_for_remote(self, remote_path: str) -> Optional[Path]:
        remote_prefix = self.remote_path_prefix.rstrip("/")
        local_prefix = self.local_path_prefix.rstrip("/")
        if remote_path == remote_prefix:
            return Path(local_prefix)
        if remote_path.startswith(remote_prefix + "/"):
            return Path(local_prefix + remote_path[len(remote_prefix) :])
        return None

    def can_handle(self, local_path: Path) -> bool:
        return self.remote_path_for(local_path) is not None

    def resolve_video_file_path(self, local_path: Path) -> Path:
        remote_path = self._required_remote_path(local_path)
        extensions = sorted(VIDEO_FILE_EXTENSIONS)
        find_predicate = []
        for extension in extensions:
            if find_predicate:
                find_predicate.append("-o")
            find_predicate.extend(["-iname", "*%s" % extension])
        script = (
            'if [ -f "$1" ]; then printf "0\\t%%s\\n" "$1"; exit 0; fi; '
            'if [ ! -d "$1" ]; then exit 2; fi; '
            'find "$1" -maxdepth 3 -type f \\( %s \\) -printf "%%s\\t%%p\\n"'
        ) % " ".join(shlex.quote(value) for value in find_predicate)
        proc = self._run_video_scan(remote_path, script)
        if proc.returncode != 0 and _find_printf_unsupported(proc):
            proc = self._run_video_scan(remote_path, _portable_find_script(find_predicate))
        if proc.returncode != 0:
            raise RuntimeError("remote video directory scan failed on %s: %s" % (self.ssh_host, (proc.stderr.strip() or proc.stdout.strip())))
        selected = _largest_size_path_from_lines(proc.stdout)
        if selected is None:
            raise FileNotFoundError("no video file found under remote path: %s" % remote_path)
        mapped = self.local_path_for_remote(selected)
        if mapped is None:
            raise RuntimeError("resolved remote video path is not under configured remote prefix: %s" % selected)
        return mapped

    def probe_subtitle_streams(self, video_path: Path) -> List[SubtitleStream]:
        remote_path = self._required_remote_path(video_path)
        command = self._ssh_command(
            [
                self.ffprobe,
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_entries",
                "stream=index,codec_name:stream_tags=language,title",
                "-of",
                "json",
                remote_path,
            ]
        )
        try:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise RuntimeError("remote ffprobe timed out probing %s on %s" % (remote_path, self.ssh_host))
        if proc.returncode != 0:
            raise RuntimeError("remote ffprobe failed on %s: %s" % (self.ssh_host, (proc.stderr.strip() or proc.stdout.strip())))
        return parse_ffprobe_streams(proc.stdout)

    def extract_text_subtitle_to_srt(self, video_path: Path, stream: SubtitleStream, output_path: Path) -> None:
        remote_path = self._required_remote_path(video_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = self._ssh_command(
            [
                self.ffmpeg,
                "-v",
                "error",
                "-i",
                remote_path,
                "-map",
                "0:%s" % stream.index,
                "-c:s",
                "srt",
                "-f",
                "srt",
                "-",
            ]
        )
        try:
            proc = subprocess.run(command, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise RuntimeError("remote ffmpeg subtitle extraction timed out for %s on %s" % (remote_path, self.ssh_host))
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace") if isinstance(proc.stderr, bytes) else str(proc.stderr or "")
            stdout = proc.stdout.decode("utf-8", errors="replace") if isinstance(proc.stdout, bytes) else str(proc.stdout or "")
            raise RuntimeError("remote ffmpeg subtitle extraction failed on %s: %s" % (self.ssh_host, (stderr.strip() or stdout.strip())))
        output_path.write_bytes(proc.stdout if isinstance(proc.stdout, bytes) else str(proc.stdout).encode("utf-8"))

    def ensure_remote_can_write(self, local_path: Path, force: bool = False) -> None:
        remote_path = self._required_remote_path(local_path)
        script = (
            'dest="$1"; force="$2"; dir=$(dirname "$dest"); '
            'if [ ! -d "$dir" ]; then mkdir -p "$dir" || exit 2; fi; '
            'if [ ! -d "$dir" ]; then exit 2; fi; '
            'if [ ! -w "$dir" ]; then exit 3; fi; '
            'if [ -e "$dest" ] && [ "$force" != "1" ]; then exit 4; fi'
        )
        command = self._ssh_command(["sh", "-c", script, "sh", remote_path, "1" if force else "0"])
        try:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise RuntimeError("remote output write check timed out for %s on %s" % (remote_path, self.ssh_host))
        if proc.returncode == 0:
            return
        if proc.returncode == 2:
            raise FileNotFoundError("remote output directory does not exist: %s" % str(Path(remote_path).parent))
        if proc.returncode == 3:
            raise PermissionError("remote output directory is not writable on %s: %s" % (self.ssh_host, str(Path(remote_path).parent)))
        if proc.returncode == 4:
            raise FileExistsError("output already exists: %s (remote: %s)" % (local_path, remote_path))
        raise RuntimeError("remote output write check failed on %s: %s" % (self.ssh_host, (proc.stderr.strip() or proc.stdout.strip())))

    def copy_file_to_remote(self, source_path: Path, destination_path: Path, force: bool = False) -> None:
        remote_path = self._required_remote_path(destination_path)
        script = (
            'set -eu; dest="$1"; force="$2"; dir=$(dirname "$dest"); '
            'mkdir -p "$dir"; '
            'if [ -e "$dest" ] && [ "$force" != "1" ]; then exit 4; fi; '
            'tmp="${dest}.tmp.$$"; '
            'trap \'rm -f "$tmp"\' EXIT HUP INT TERM; '
            'cat > "$tmp"; '
            'mv -f "$tmp" "$dest"; '
            'trap - EXIT HUP INT TERM'
        )
        command = self._ssh_command(["sh", "-c", script, "sh", remote_path, "1" if force else "0"])
        try:
            with source_path.open("rb") as source_file:
                proc = subprocess.run(command, stdin=source_file, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        except subprocess.TimeoutExpired:
            raise RuntimeError("remote output copy timed out for %s on %s" % (remote_path, self.ssh_host))
        if proc.returncode == 0:
            return
        stderr = proc.stderr.decode("utf-8", errors="replace") if isinstance(proc.stderr, bytes) else str(proc.stderr or "")
        stdout = proc.stdout.decode("utf-8", errors="replace") if isinstance(proc.stdout, bytes) else str(proc.stdout or "")
        if proc.returncode == 4:
            raise FileExistsError("output already exists: %s (remote: %s)" % (destination_path, remote_path))
        raise RuntimeError("remote output copy failed on %s: %s" % (self.ssh_host, (stderr.strip() or stdout.strip())))

    def _required_remote_path(self, local_path: Path) -> str:
        remote_path = self.remote_path_for(local_path)
        if remote_path is None:
            raise RuntimeError("path is not under remote subtitle local prefix: %s" % local_path)
        return remote_path

    def _ssh_command(self, remote_argv: List[str]) -> List[str]:
        command = "exec " + " ".join(shlex.quote(value) for value in remote_argv)
        return [self.ssh_bin, self.ssh_host, command]

    def _run_video_scan(self, remote_path: str, script: str) -> subprocess.CompletedProcess:
        command = self._ssh_command(["sh", "-c", script, "sh", remote_path])
        try:
            return subprocess.run(command, text=True, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise RuntimeError("remote video directory scan timed out for %s on %s" % (remote_path, self.ssh_host))


def _find_printf_unsupported(proc: subprocess.CompletedProcess) -> bool:
    output = "%s\n%s" % (proc.stderr or "", proc.stdout or "")
    normalized = output.casefold()
    return "-printf" in normalized and any(
        marker in normalized for marker in ("unknown", "unsupported", "illegal", "bad option", "unrecognized")
    )


def _portable_find_script(find_predicate: List[str]) -> str:
    return (
        'if [ -f "$1" ]; then printf "0\\t%%s\\n" "$1"; exit 0; fi; '
        'if [ ! -d "$1" ]; then exit 2; fi; '
        "find \"$1\" -maxdepth 3 -type f \\( %s \\) "
        "-exec sh -c 'for file do size=$(wc -c < \"$file\" | tr -d \" \"); "
        "printf \"%%s\\t%%s\\n\" \"$size\" \"$file\"; done' sh {} +"
    ) % " ".join(shlex.quote(value) for value in find_predicate)


def resolve_video_file_path(video_path: Path, remote_runner: Optional[RemoteSubtitleCommandRunner] = None) -> Path:
    if remote_runner is not None and remote_runner.can_handle(video_path) and not video_path.exists():
        return remote_runner.resolve_video_file_path(video_path)
    if video_path.is_file():
        return video_path
    if video_path.is_dir():
        return _largest_local_video_file(video_path)
    if remote_runner is not None and remote_runner.can_handle(video_path):
        return remote_runner.resolve_video_file_path(video_path)
    return video_path


def _largest_local_video_file(root: Path) -> Path:
    candidates = [path for path in root.rglob("*") if path.is_file() and _is_video_file(path)]
    if not candidates:
        raise FileNotFoundError("no video file found under directory: %s" % root)
    return max(candidates, key=lambda path: path.stat().st_size)


def _is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_FILE_EXTENSIONS


def _largest_size_path_from_lines(output: str) -> Optional[str]:
    best_size = -1
    best_path = None
    for line in output.splitlines():
        if "\t" not in line:
            continue
        size_text, path = line.split("\t", 1)
        try:
            size = int(size_text)
        except ValueError:
            size = 0
        if size > best_size:
            best_size = size
            best_path = path
    return best_path


def find_source_sidecar(video_path: Path, source_language: str) -> Optional[Path]:
    suffixes = language_suffixes(source_language)
    wanted_names = []
    for extension in (".srt", ".sub"):
        for suffix in suffixes:
            for separator in (".", "_"):
                wanted_names.append("%s%s%s%s" % (video_path.stem, separator, suffix, extension))
    wanted_lookup = {name.lower(): name for name in wanted_names}
    if video_path.parent.exists():
        children = {child.name.lower(): child for child in video_path.parent.iterdir()}
        for wanted in wanted_lookup:
            child = children.get(wanted)
            if child is not None:
                return child
    return None


def build_ffprobe_command(video_path: Path, ffprobe: str = "ffprobe") -> List[str]:
    return [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "s",
        "-show_entries",
        "stream=index,codec_name:stream_tags=language,title",
        "-of",
        "json",
        str(video_path),
    ]


def build_ffmpeg_probe_command(video_path: Path, ffmpeg: str = "ffmpeg") -> List[str]:
    return [ffmpeg, "-hide_banner", "-i", str(video_path)]


def parse_ffprobe_streams(payload: str) -> List[SubtitleStream]:
    data = json.loads(payload)
    streams = []
    for item in data.get("streams", []):
        tags = item.get("tags") or {}
        streams.append(
            SubtitleStream(
                index=int(item["index"]),
                codec_name=str(item.get("codec_name", "")),
                language=tags.get("language"),
                title=tags.get("title"),
            )
        )
    return streams


def parse_ffmpeg_streams(payload: str) -> List[SubtitleStream]:
    streams = []
    stream_re = re.compile(r"Stream #0:(\d+)(?:\(([^)]+)\))?: Subtitle: ([^,\s]+)")
    for line in payload.splitlines():
        match = stream_re.search(line)
        if not match:
            continue
        streams.append(
            SubtitleStream(
                index=int(match.group(1)),
                codec_name=match.group(3).strip(),
                language=match.group(2),
                title=None,
            )
        )
    return streams


def probe_subtitle_streams(video_path: Path, ffprobe: str = "ffprobe") -> List[SubtitleStream]:
    try:
        proc = subprocess.run(build_ffprobe_command(video_path, ffprobe), text=True, capture_output=True, timeout=60)
    except FileNotFoundError:
        return probe_subtitle_streams_with_ffmpeg(video_path)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffprobe timed out probing %s" % video_path)
    if proc.returncode != 0:
        raise RuntimeError("ffprobe failed: %s" % (proc.stderr.strip() or proc.stdout.strip()))
    return parse_ffprobe_streams(proc.stdout)


def probe_subtitle_streams_with_ffmpeg(video_path: Path, ffmpeg: str = "ffmpeg") -> List[SubtitleStream]:
    try:
        proc = subprocess.run(build_ffmpeg_probe_command(video_path, ffmpeg), text=True, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg probe timed out for %s" % video_path)
    return parse_ffmpeg_streams("\n".join([proc.stdout, proc.stderr]))


def _language_matches(stream: SubtitleStream, requested_language: str) -> bool:
    requested_code = language_to_code(requested_language)
    values = [stream.language or "", stream.title or ""]
    for value in values:
        if not value:
            continue
        normalized = normalize_language_label(value)
        if normalized == requested_code:
            return True
        try:
            if language_to_code(normalized) == requested_code:
                return True
        except ValueError:
            continue
    return False


def select_text_subtitle_stream(
    streams: List[SubtitleStream],
    source_language: str,
    assume_unlabeled_language: bool = False,
) -> Optional[SubtitleStream]:
    text_streams = [stream for stream in streams if stream.is_text]
    for stream in text_streams:
        if _language_matches(stream, source_language):
            return stream
    if assume_unlabeled_language and len(text_streams) == 1 and _is_unlabeled_stream(text_streams[0]):
        return text_streams[0]
    return None


def _is_unlabeled_stream(stream: SubtitleStream) -> bool:
    return not (stream.language or "").strip() and not (stream.title or "").strip()


def extract_text_subtitle_to_srt(video_path: Path, stream: SubtitleStream, output_path: Path, ffmpeg: str = "ffmpeg") -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(video_path),
        "-map",
        "0:%s" % stream.index,
        "-c:s",
        "srt",
        str(output_path),
    ]
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg subtitle extraction timed out for %s" % video_path)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg subtitle extraction failed: %s" % (proc.stderr.strip() or proc.stdout.strip()))


def acquire_source_subtitle(
    video_path: Path,
    source_language: str,
    work_dir: Path,
    probe_runner: Optional[ProbeRunner] = None,
    extractor: Optional[Extractor] = None,
    assume_unlabeled_stream_language: bool = False,
    progress_callback: Optional[ProgressCallback] = None,
    executor_name: Optional[str] = None,
    remote_path: Optional[str] = None,
) -> AcquisitionResult:
    execution = executor_name or "local"
    _emit_progress(
        progress_callback,
        "checking_source_sidecar",
        "Checking for source subtitle sidecar.",
        input=str(video_path),
        source_language=source_language,
        execution="local",
    )
    sidecar = find_source_sidecar(video_path, source_language)
    if sidecar is not None:
        return AcquisitionResult(
            AcquisitionStatus.READY,
            path=sidecar,
            method="sidecar",
            message="found source sidecar",
            execution="local",
        )

    _emit_progress(
        progress_callback,
        "probing_embedded_subtitles",
        "Probing embedded subtitle streams.",
        input=str(video_path),
        source_language=source_language,
        execution=execution,
        remote_path=remote_path,
    )
    streams = probe_runner(video_path) if probe_runner else probe_subtitle_streams(video_path)
    selected = select_text_subtitle_stream(
        streams,
        source_language,
        assume_unlabeled_language=assume_unlabeled_stream_language,
    )
    if selected is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        output_path = work_dir / ("%s.%s.source.srt" % (video_path.stem, language_to_code(source_language)))
        _emit_progress(
            progress_callback,
            "extracting_embedded_subtitle",
            "Extracting embedded source subtitle stream.",
            input=str(video_path),
            source_language=source_language,
            execution=execution,
            remote_path=remote_path,
            stream_index=selected.index,
            codec_name=selected.codec_name,
            stream_language=selected.language,
        )
        if extractor:
            extractor(video_path, selected, output_path)
        else:
            extract_text_subtitle_to_srt(video_path, selected, output_path)
        assumed_unlabeled = assume_unlabeled_stream_language and _is_unlabeled_stream(selected)
        return AcquisitionResult(
            AcquisitionStatus.READY,
            path=output_path,
            method="embedded-text-unlabeled" if assumed_unlabeled else "embedded-text",
            message=(
                "extracted single unlabeled embedded text subtitle stream as %s" % source_language
                if assumed_unlabeled
                else "extracted embedded text subtitle stream"
            ),
            stream=selected,
            warning=(
                "Assumed the only unlabeled embedded text subtitle stream is %s." % source_language
                if assumed_unlabeled
                else None
            ),
            execution=execution,
            remote_path=remote_path,
        )

    if any(stream.is_image for stream in streams):
        return AcquisitionResult(
            AcquisitionStatus.UNSUPPORTED,
            method="embedded-image",
            message="Only image-based subtitles were found. OCR is not part of the MVP; provide an external SRT or add an OCR/source-provider stage.",
            stream=streams[0],
            execution=execution,
            remote_path=remote_path,
        )

    return AcquisitionResult(
        AcquisitionStatus.NOT_FOUND,
        method="none",
        message="No source sidecar or embedded text subtitle was found. Use translate-plex online provider fallback or provide an external SRT.",
        execution=execution,
        remote_path=remote_path,
    )


def _emit_progress(
    callback: Optional[ProgressCallback],
    stage: str,
    message: str,
    **details: Any,
) -> None:
    if callback is None:
        return
    try:
        callback(
            {
                "stage": stage,
                "message": message,
                "details": {key: value for key, value in details.items() if value is not None},
            }
        )
    except Exception:
        pass


def _env_string(env: Mapping[str, str], key: str) -> Optional[str]:
    value = env.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_first_string(env: Mapping[str, str], *keys: str) -> Optional[str]:
    for key in keys:
        value = _env_string(env, key)
        if value:
            return value
    return None
