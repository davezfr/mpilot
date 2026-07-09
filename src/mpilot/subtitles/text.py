from __future__ import annotations

import re
from typing import List


DOTTED_INITIALISM_RE = re.compile(r"(?:[A-Za-z]\.){2,}")
PROTECTED_TERMINAL_ABBREVIATIONS = {
    "Mr.",
    "Mrs.",
    "Ms.",
    "Dr.",
    "Prof.",
    "Sr.",
    "Jr.",
    "St.",
    "U.S.",
    "U.K.",
}
CJK_PUNCTUATION = "，。！？、：；（）【】《》「」『』"


def strip_terminal_statement_punctuation(text: str) -> str:
    stripped = text.rstrip()
    if not stripped:
        return text
    if stripped.endswith(("?", "!", "？", "！", "...", "…")):
        return text
    if stripped.endswith(".") and should_preserve_terminal_period(stripped):
        return text
    if stripped[-1] in "。，.,":
        without_terminal = stripped[:-1].rstrip()
        return without_terminal or text
    return text


def strip_terminal_statement_punctuation_from_lines(lines: List[str]) -> List[str]:
    return [strip_terminal_statement_punctuation(line) for line in lines]


def is_cjk_or_cjk_punctuation(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or char in CJK_PUNCTUATION
    )


def needs_flat_separator(previous_text: str, next_text: str) -> bool:
    if next_text.startswith(("-", "–", "—")):
        return True
    previous_char = previous_text[-1]
    next_char = next_text[0]
    if is_cjk_or_cjk_punctuation(previous_char) or is_cjk_or_cjk_punctuation(next_char):
        return False
    return True


def flatten_subtitle_lines(lines: List[str], clean_terminal: bool = False) -> str:
    text_lines = strip_terminal_statement_punctuation_from_lines(lines) if clean_terminal else lines
    stripped_lines = [line.strip() for line in text_lines if line.strip()]
    if not stripped_lines:
        return ""
    text = stripped_lines[0]
    for line in stripped_lines[1:]:
        separator = " " if needs_flat_separator(text, line) else ""
        text = "%s%s%s" % (text, separator, line)
    if clean_terminal:
        text = strip_terminal_statement_punctuation(text)
    return text


def should_preserve_terminal_period(text: str) -> bool:
    token = text.split()[-1].strip("\"'“”‘’()[]{}")
    return token in PROTECTED_TERMINAL_ABBREVIATIONS or bool(DOTTED_INITIALISM_RE.fullmatch(token))
