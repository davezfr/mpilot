from __future__ import annotations

import re
from typing import List


LANGUAGE_CODES = {
    "arabic": "ar",
    "chinese": "zh",
    "simplified chinese": "zh",
    "traditional chinese": "zh",
    "mandarin": "zh",
    "cn": "zh",
    "zh-cn": "zh",
    "english": "en",
    "eng": "en",
    "french": "fr",
    "francais": "fr",
    "français": "fr",
    "german": "de",
    "spanish": "es",
    "japanese": "ja",
    "korean": "ko",
    "italian": "it",
    "portuguese": "pt",
    "russian": "ru",
}


def normalize_language_label(language: str) -> str:
    return re.sub(r"\s+", " ", language.strip().lower().replace("_", " ").replace("-", " "))


def language_to_code(language: str) -> str:
    normalized = normalize_language_label(language)
    if normalized in LANGUAGE_CODES:
        return LANGUAGE_CODES[normalized]
    compact = normalized.replace(" ", "")
    if len(compact) == 2 and compact.isalpha():
        return "zh" if compact == "cn" else compact
    if len(compact) == 3 and compact in {"eng", "fre", "fra", "zho", "chi"}:
        return {"eng": "en", "fre": "fr", "fra": "fr", "zho": "zh", "chi": "zh"}[compact]
    raise ValueError("unknown language code or label: %s" % language)


def language_suffixes(language: str) -> List[str]:
    code = language_to_code(language)
    normalized = normalize_language_label(language)
    values = [code, normalized.replace(" ", "-"), normalized.replace(" ", "_"), normalized]
    aliases = {
        "en": ["eng", "english"],
        "fr": ["fra", "fre", "french"],
        "zh": ["zho", "chi", "chinese", "zh-cn"],
    }
    values.extend(aliases.get(code, []))
    deduped = []
    for value in values:
        value = value.strip(". _-").lower()
        if value and value not in deduped:
            deduped.append(value)
    return deduped
