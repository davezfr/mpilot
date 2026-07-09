from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .srt import Cue
from .text import flatten_subtitle_lines, strip_terminal_statement_punctuation_from_lines


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: {playres_x}
PlayResY: {playres_y}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{styles}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

SIZE_TABLES = {
    "cjk": {360: (19, 14), 720: (38, 24), 1080: (56, 36), 2160: (112, 72)},
    "latin": {360: (16, 11), 720: (32, 23), 1080: (48, 34), 2160: (96, 68)},
}
SOURCE_MARGIN_TABLE = {360: 18, 720: 36, 1080: 55, 2160: 110}
PLAYRES_TABLE = {360: (640, 360), 720: (1280, 720), 1080: (1920, 1080), 2160: (3840, 2160)}
PRIMARY_FONT_BY_SCRIPT = {"cjk": "PingFang SC", "latin": "Arial"}
SECONDARY_FONT_BY_SCRIPT = {"cjk": "PingFang SC", "latin": "Arial"}


@dataclass(frozen=True)
class AssOptions:
    mode: str = "bilingual-ass"
    primary_script: str = "cjk"
    secondary_script: str = "latin"
    height: int = 1080
    primary_size: Optional[int] = None
    secondary_size: Optional[int] = None
    primary_font: Optional[str] = None
    secondary_font: Optional[str] = None
    marginv: Optional[int] = None


def srt_time_to_ass(value: str) -> str:
    value = value.replace(".", ",")
    hms, millis = value.split(",")
    hours, minutes, seconds = hms.split(":")
    centiseconds = min(99, round(int(millis) / 10.0))
    return "%s:%s:%s.%02d" % (int(hours), minutes, seconds, centiseconds)


def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def join_lines(lines: List[str], clean_terminal: bool = False) -> str:
    text_lines = strip_terminal_statement_punctuation_from_lines(lines) if clean_terminal else lines
    return r"\N".join(ass_escape(line.strip()) for line in text_lines if line.strip())


def join_flat_lines(lines: List[str], clean_terminal: bool = False) -> str:
    return ass_escape(flatten_subtitle_lines(lines, clean_terminal=clean_terminal))


def _nearest_height(height: int, table) -> int:
    return min(table, key=lambda candidate: abs(candidate - (height or 720)))


def pick_sizes(options: AssOptions) -> Tuple[int, int]:
    if options.primary_size is not None:
        primary = options.primary_size
        secondary = options.secondary_size if options.secondary_size is not None else max(8, round(primary / 1.7))
        return primary, secondary
    table = SIZE_TABLES[options.primary_script]
    primary, secondary = table[_nearest_height(options.height, table)]
    if options.secondary_size is not None:
        secondary = options.secondary_size
    return primary, secondary


def pick_source_margin(options: AssOptions) -> int:
    if options.marginv is not None:
        return options.marginv
    return SOURCE_MARGIN_TABLE[_nearest_height(options.height, SOURCE_MARGIN_TABLE)]


def pick_playres(options: AssOptions) -> Tuple[int, int]:
    return PLAYRES_TABLE[_nearest_height(options.height, PLAYRES_TABLE)]


def build_styles(options: AssOptions, primary_size: int, secondary_size: int, source_marginv: int) -> str:
    primary_font = options.primary_font or PRIMARY_FONT_BY_SCRIPT[options.primary_script]
    secondary_font = options.secondary_font or SECONDARY_FONT_BY_SCRIPT[options.secondary_script]
    if options.mode == "bilingual-ass":
        line_gap = max(2, round(secondary_size * 0.05))
        primary_marginv = source_marginv + secondary_size + line_gap
        return "\n".join(
            [
                "Style: Primary,%s,%s,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,3.0,0.6,2,120,120,%s,1"
                % (primary_font, primary_size, primary_marginv),
                "Style: Secondary,%s,%s,&H00D6F4FF,&H000000FF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,1,2.4,0.4,2,120,120,%s,1"
                % (secondary_font, secondary_size, source_marginv),
            ]
        )
    return "Style: Default,%s,%s,&H00FFFFFF,&H000000FF,&H64000000,&H00000000,1,0,0,0,100,100,0,0,1,1.2,0,2,20,20,%s,1" % (
        primary_font,
        primary_size,
        source_marginv,
    )


def build_ass(source_cues: List[Cue], target_cues: List[Cue], options: AssOptions) -> str:
    if len(source_cues) != len(target_cues):
        raise ValueError("SRT entry count mismatch: source=%s target=%s" % (len(source_cues), len(target_cues)))

    primary_size, secondary_size = pick_sizes(options)
    source_marginv = pick_source_margin(options)
    playres_x, playres_y = pick_playres(options)
    lines = [
        ASS_HEADER.format(
            styles=build_styles(options, primary_size, secondary_size, source_marginv),
            playres_x=playres_x,
            playres_y=playres_y,
        )
    ]

    for index, (source, target) in enumerate(zip(source_cues, target_cues), start=1):
        if source.number != target.number:
            raise ValueError("cue number mismatch at entry %s: source=%s target=%s" % (index, source.number, target.number))
        if source.start != target.start or source.end != target.end:
            raise ValueError("timestamp mismatch at entry %s: source=%s->%s target=%s->%s" % (index, source.start, source.end, target.start, target.end))

        if options.mode == "bilingual-ass":
            lines.append(
                "Dialogue: 1,%s,%s,Primary,,0,0,0,,%s"
                % (srt_time_to_ass(target.start), srt_time_to_ass(target.end), join_flat_lines(target.text_lines, clean_terminal=True))
            )
            lines.append(
                "Dialogue: 0,%s,%s,Secondary,,0,0,0,,%s"
                % (srt_time_to_ass(source.start), srt_time_to_ass(source.end), join_flat_lines(source.text_lines, clean_terminal=True))
            )
        else:
            lines.append(
                "Dialogue: 0,%s,%s,Default,,0,0,0,,%s"
                % (srt_time_to_ass(target.start), srt_time_to_ass(target.end), join_lines(target.text_lines, clean_terminal=True))
            )
    return "\n".join(lines) + "\n"
