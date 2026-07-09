from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .languages import language_to_code


def strip_language_suffix(path: Path, suffixes: Iterable[str]) -> str:
    stem = path.stem
    lower_stem = stem.lower()
    normalized_suffixes = [suffix.lower().lstrip(".").lstrip("_") for suffix in suffixes]
    for suffix in normalized_suffixes:
        for separator in (".", "_"):
            needle = separator + suffix
            if lower_stem.endswith(needle):
                return stem[: -len(needle)]
    return stem


def plex_sidecar_path(media_path: Path, language: str, extension: str) -> Path:
    code = language_to_code(language)
    normalized_extension = extension if extension.startswith(".") else "." + extension
    return media_path.with_name("%s.%s%s" % (media_path.stem, code, normalized_extension))


def subtitle_output_path(input_subtitle: Path, target_language: str, extension: str, source_suffixes: Iterable[str]) -> Path:
    code = language_to_code(target_language)
    normalized_extension = extension if extension.startswith(".") else "." + extension
    output_stem = strip_language_suffix(input_subtitle, source_suffixes)
    return input_subtitle.with_name("%s.%s%s" % (output_stem, code, normalized_extension))
