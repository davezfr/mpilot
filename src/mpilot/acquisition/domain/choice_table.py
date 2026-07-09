from __future__ import annotations

from html import escape

from mpilot.acquisition.domain.quality import parse_quality
from mpilot.acquisition.models import ManualSearchResult, MovieCandidate

SEEDERS_EMOJI = "🧲"
SIZE_EMOJI = "💾"
MISSING = "—"
DEFAULT_RESOLUTION = "1080p"


def render_choice_table(results: list[ManualSearchResult]) -> str:
    """Render an aligned monospace choice table for identity-certain result lists.

    Designed for Telegram <pre> blocks: every row places the same emojis at the
    same column, so the rows stay vertically aligned despite emoji width
    varying between platforms. Recommendation, if any, is conveyed by the
    surrounding picker UI instead of an extra glyph in the table.
    """
    if not results:
        return ""

    rows = []
    for result in results:
        parsed = parse_quality(result.title)
        source = "REMUX" if parsed.is_remux and parsed.source in {None, "BluRay"} else (parsed.source or MISSING)
        rows.append(
            {
                "index": result.index,
                "resolution": parsed.resolution,
                "source": source,
                "codec": parsed.codec or MISSING,
                "seeders": str(result.seeders) if result.seeders is not None else MISSING,
                "size": _compact_size(result.size),
            }
        )

    show_resolution = any(r["resolution"] and r["resolution"] != DEFAULT_RESOLUTION for r in rows)
    source_width = max(len(r["source"]) for r in rows)
    codec_width = max(len(r["codec"]) for r in rows)
    seeders_width = max(len(r["seeders"]) for r in rows)
    size_width = max(len(r["size"]) for r in rows)

    lines = []
    for row in rows:
        parts = [f"{row['index']}."]
        if show_resolution:
            parts.append((row["resolution"] or MISSING).rjust(5))
        parts.append(row["source"].ljust(source_width))
        parts.append(_center(row["codec"], codec_width))
        parts.append(f"{SEEDERS_EMOJI} {row['seeders'].rjust(seeders_width)}")
        parts.append(f"{SIZE_EMOJI} {row['size'].rjust(size_width)}")
        lines.append("  ".join(parts))
    return "\n".join(lines)


def render_choice_rich_html(message: str, results: list[ManualSearchResult]) -> str:
    """Render release choices as Telegram rich-message HTML.

    Telegram rich tables remove the need for monospace alignment tricks while
    keeping download links out of the chat-rendering payload.
    """
    rows = [
        "<tr>"
        "<th>#</th>"
        "<th>Resolution</th>"
        "<th>Source</th>"
        "<th>Codec</th>"
        "<th>Seeders</th>"
        "<th>Size</th>"
        "</tr>"
    ]
    for result in results:
        parsed = parse_quality(result.title)
        source = "REMUX" if parsed.is_remux and parsed.source in {None, "BluRay"} else (parsed.source or MISSING)
        rows.append(
            "<tr>"
            f'<td align="right"><b>{result.index}</b></td>'
            f"<td>{escape(parsed.resolution or MISSING)}</td>"
            f"<td>{escape(source)}</td>"
            f"<td>{escape(parsed.codec or MISSING)}</td>"
            f'<td align="right">{escape(str(result.seeders) if result.seeders is not None else MISSING)}</td>'
            f'<td align="right">{escape(_compact_size(result.size))}</td>'
            "</tr>"
        )
    return (
        f"<p><b>{escape(message)}</b></p>"
        "<table bordered striped>"
        "<caption>Release choices</caption>"
        f"{''.join(rows)}"
        "</table>"
    )


def render_title_choice_table(candidates: list[MovieCandidate]) -> str:
    """Render a monospace title-choice list for ambiguous keyword matches."""
    return "\n".join(f"{candidate.index}. {candidate.label}" for candidate in candidates)


def render_title_choice_rich_html(message: str, candidates: list[MovieCandidate]) -> str:
    """Render title candidates as Telegram rich-message HTML."""
    rows = [
        "<tr>"
        "<th>#</th>"
        "<th>Title</th>"
        "<th>Year</th>"
        "</tr>"
    ]
    for candidate in candidates:
        rows.append(
            "<tr>"
            f'<td align="right"><b>{candidate.index}</b></td>'
            f"<td>{escape(candidate.title)}</td>"
            f'<td align="right">{escape(str(candidate.year)) if candidate.year else MISSING}</td>'
            "</tr>"
        )
    return (
        f"<p><b>{escape(message)}</b></p>"
        "<table bordered striped>"
        "<caption>Title choices</caption>"
        f"{''.join(rows)}"
        "</table>"
    )


def _center(value: str, width: int) -> str:
    return value.center(width)


def _compact_size(size: int | None) -> str:
    if size is None or size <= 0:
        return MISSING
    value = float(size)
    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    while abs(value) >= 1000 and unit_index < len(units) - 1:
        value /= 1000
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)}B"
    formatted = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{formatted}{units[unit_index]}"
