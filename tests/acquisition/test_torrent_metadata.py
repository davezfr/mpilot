from __future__ import annotations

import hashlib

from app.domain.torrent_metadata import parse_torrent_info_hash, parse_torrent_name


def test_parse_torrent_name_still_returns_info_name():
    content = b"d4:info" + b"d4:name4:Teste" + b"e"

    assert parse_torrent_name(content) == "Test"


def test_parse_torrent_info_hash_returns_sha1_of_raw_info_dict():
    info_dict = b"d4:name4:Teste"
    content = b"d8:announce15:https://tracker4:info" + info_dict + b"e"

    assert parse_torrent_info_hash(content) == hashlib.sha1(info_dict).hexdigest()
