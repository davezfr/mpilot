from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List


TIME_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})"
)


@dataclass(frozen=True)
class Cue:
    number: str
    start: str
    end: str
    text_lines: List[str]

    @property
    def text(self) -> str:
        return "\n".join(self.text_lines)


def parse_srt_text(text: str) -> List[Cue]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [block for block in re.split(r"\n\s*\n", normalized.strip()) if block.strip()]
    cues = []

    for block_number, block in enumerate(blocks, start=1):
        lines = [line.rstrip() for line in block.split("\n") if line.strip()]
        timestamp_index = _timestamp_line_index(lines)
        if timestamp_index is None:
            _warn_malformed_block(block_number, "missing timestamp row")
            continue

        timestamp = TIME_RE.search(lines[timestamp_index])
        text_lines = lines[timestamp_index + 1 :]
        if not text_lines:
            _warn_malformed_block(block_number, "missing subtitle text")
            continue

        number = lines[0].strip() if timestamp_index == 1 else str(len(cues) + 1)
        cues.append(
            Cue(
                number=number,
                start=timestamp.group(1).replace(".", ","),
                end=timestamp.group(2).replace(".", ","),
                text_lines=text_lines,
            )
        )

    if not cues:
        raise ValueError("no valid SRT cues found")
    return cues


def read_srt(path: Path) -> List[Cue]:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return parse_srt_text(raw.decode("utf-16"))

    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        return parse_srt_text(text)
    raise ValueError("cannot decode subtitle file: %s" % path)


def _timestamp_line_index(lines: List[str]) -> int | None:
    if lines and TIME_RE.search(lines[0]):
        return 0
    if len(lines) >= 2 and TIME_RE.search(lines[1]):
        return 1
    return None


def _warn_malformed_block(block_number: int, reason: str) -> None:
    warnings.warn(
        "Skipping malformed SRT block %s: %s" % (block_number, reason),
        RuntimeWarning,
        stacklevel=2,
    )


def format_srt(cues: Iterable[Cue]) -> str:
    blocks = []
    for cue in cues:
        text_lines = [line for line in cue.text_lines if line.strip()]
        if not text_lines:
            raise ValueError("cannot format SRT cue with empty text")
        blocks.append("\n".join([cue.number, f"{cue.start} --> {cue.end}", *text_lines]))
    return "\n\n".join(blocks) + "\n\n"


def with_replacement_text(source_cues: List[Cue], translations: List[str]) -> List[Cue]:
    if len(source_cues) != len(translations):
        raise ValueError(
            "translation count mismatch source=%s target=%s"
            % (len(source_cues), len(translations))
        )
    replaced = []
    for cue, translation in zip(source_cues, translations):
        lines = [line.strip() for line in translation.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]
        if not lines:
            raise ValueError("empty translation for cue %s" % cue.number)
        replaced.append(Cue(cue.number, cue.start, cue.end, lines))
    return replaced
