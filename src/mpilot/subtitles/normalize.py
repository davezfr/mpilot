from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .srt import Cue, format_srt, read_srt


MICRODVD_LINE_RE = re.compile(r"^\{(-?\d+)\}\{(-?\d+)\}(.*)$")
UNSUPPORTED_SUB_MESSAGE = "unsupported .sub subtitle format; only MicroDVD text .sub can be normalized to SRT"


@dataclass(frozen=True)
class NormalizedSubtitle:
    path: Path
    cues: List[Cue]
    temporary: bool = False


def normalize_to_srt(input_path: Path, work_dir: Path, ffmpeg: str = "ffmpeg") -> NormalizedSubtitle:
    suffix = input_path.suffix.lower()
    if suffix == ".srt":
        return NormalizedSubtitle(path=input_path, cues=read_srt(input_path), temporary=False)
    if suffix == ".sub":
        work_dir.mkdir(parents=True, exist_ok=True)
        output_path = work_dir / ("%s.normalized.srt" % input_path.stem)
        cues = parse_microdvd_text(_read_text_subtitle(input_path))
        output_path.write_text(format_srt(cues), encoding="utf-8")
        return NormalizedSubtitle(path=output_path, cues=cues, temporary=True)
    if suffix not in {".ass", ".ssa", ".vtt"}:
        raise ValueError("unsupported subtitle format for normalization: %s" % input_path)

    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = work_dir / ("%s.normalized.srt" % input_path.stem)
    convert_text_subtitle_to_srt(input_path, output_path, ffmpeg=ffmpeg)
    return NormalizedSubtitle(path=output_path, cues=read_srt(output_path), temporary=True)


def convert_text_subtitle_to_srt(input_path: Path, output_path: Path, ffmpeg: str = "ffmpeg") -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-v", "error", "-i", str(input_path), str(output_path)]
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg subtitle normalization timed out for %s" % input_path)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg subtitle normalization failed: %s" % (proc.stderr.strip() or proc.stdout.strip()))


def parse_microdvd_text(text: str, fps: Optional[float] = None) -> List[Cue]:
    selected_fps = fps
    cues: List[Cue] = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        match = MICRODVD_LINE_RE.match(line)
        if not match:
            continue
        start_frame = int(match.group(1))
        end_frame = int(match.group(2))
        body = match.group(3).strip()
        if not cues and _is_microdvd_fps_declaration(start_frame, end_frame, body):
            selected_fps = _parse_fps(body)
            continue
        if selected_fps is None:
            raise ValueError("MicroDVD .sub requires an FPS declaration line such as {1}{1}23.976")
        if selected_fps <= 0:
            raise ValueError("MicroDVD .sub FPS must be greater than zero")
        if end_frame < start_frame:
            raise ValueError("MicroDVD .sub cue end frame is before start frame")
        text_lines = [part.strip() for part in body.split("|") if part.strip()]
        if not text_lines:
            continue
        cues.append(
            Cue(
                str(len(cues) + 1),
                _format_milliseconds(_frame_to_milliseconds(start_frame, selected_fps)),
                _format_milliseconds(_frame_to_milliseconds(end_frame, selected_fps)),
                text_lines,
            )
        )

    if not cues:
        raise ValueError(UNSUPPORTED_SUB_MESSAGE)
    return cues


def _read_text_subtitle(path: Path) -> str:
    payload = path.read_bytes()
    if _looks_binary(payload):
        raise ValueError(UNSUPPORTED_SUB_MESSAGE)
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("cannot decode subtitle file: %s" % path)


def _looks_binary(payload: bytes) -> bool:
    if b"\x00" in payload:
        return True
    sample = payload[:4096]
    if not sample:
        return False
    control_bytes = sum(1 for byte in sample if byte < 32 and byte not in {9, 10, 13})
    return control_bytes > max(4, len(sample) // 100)


def _is_microdvd_fps_declaration(start_frame: int, end_frame: int, body: str) -> bool:
    return start_frame in {0, 1} and end_frame in {0, 1} and _parse_fps(body) is not None


def _parse_fps(value: str) -> Optional[float]:
    text = value.strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    return float(text)


def _frame_to_milliseconds(frame: int, fps: float) -> int:
    return int(round(frame * 1000.0 / fps))


def _format_milliseconds(milliseconds: int) -> str:
    total_seconds, millis = divmod(max(0, milliseconds), 1000)
    minutes_total, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes_total, 60)
    return "%02d:%02d:%02d,%03d" % (hours, minutes, seconds, millis)
