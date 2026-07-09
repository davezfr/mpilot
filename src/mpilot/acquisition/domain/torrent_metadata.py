from __future__ import annotations

import hashlib


def parse_torrent_name(content: bytes) -> str | None:
    torrent = _parse_torrent(content)
    if not torrent:
        return None

    name = _torrent_info_name(torrent.info)
    if not isinstance(name, bytes):
        return None

    text = name.decode("utf-8", errors="replace").strip()
    return text or None


def parse_torrent_info_hash(content: bytes) -> str | None:
    info_raw = _extract_info_raw(content)
    if not info_raw:
        return None
    return hashlib.sha1(info_raw).hexdigest()


class ParsedTorrent:
    def __init__(self, *, info: dict | None):
        self.info = info


def _parse_torrent(content: bytes) -> ParsedTorrent | None:
    try:
        parsed = _BencodeParser(content).parse()
    except (IndexError, TypeError, ValueError):
        return None

    if not isinstance(parsed, dict):
        return None

    info = parsed.get(b"info")
    return ParsedTorrent(info=info if isinstance(info, dict) else None)


def _extract_info_raw(content: bytes) -> bytes | None:
    try:
        parser = _BencodeParser(content)
        if parser.content[parser.position : parser.position + 1] != b"d":
            return None

        parser.position += 1
        while parser.content[parser.position : parser.position + 1] != b"e":
            key = parser.parse()
            value_start = parser.position
            parser.parse()
            value_end = parser.position
            if key == b"info":
                return content[value_start:value_end]
    except (IndexError, TypeError, ValueError):
        return None

    return None


def _torrent_info_name(info: dict | None) -> bytes | None:
    if not isinstance(info, dict):
        return None
    name = info.get(b"name.utf-8") or info.get(b"name")
    return name if isinstance(name, bytes) else None


class _BencodeParser:
    def __init__(self, content: bytes):
        self.content = content
        self.position = 0

    def parse(self):
        marker = self.content[self.position : self.position + 1]
        if marker == b"d":
            return self._parse_dict()
        if marker == b"l":
            return self._parse_list()
        if marker == b"i":
            return self._parse_int()
        if marker.isdigit():
            return self._parse_bytes()
        raise ValueError("invalid bencode marker")

    def _parse_dict(self) -> dict:
        self.position += 1
        result = {}
        while self.content[self.position : self.position + 1] != b"e":
            key = self.parse()
            result[key] = self.parse()
        self.position += 1
        return result

    def _parse_list(self) -> list:
        self.position += 1
        result = []
        while self.content[self.position : self.position + 1] != b"e":
            result.append(self.parse())
        self.position += 1
        return result

    def _parse_int(self) -> int:
        self.position += 1
        end = self.content.index(b"e", self.position)
        value = int(self.content[self.position : end])
        self.position = end + 1
        return value

    def _parse_bytes(self) -> bytes:
        colon = self.content.index(b":", self.position)
        length = int(self.content[self.position : colon])
        self.position = colon + 1
        value = self.content[self.position : self.position + length]
        self.position += length
        return value
