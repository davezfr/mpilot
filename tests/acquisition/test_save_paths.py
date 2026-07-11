from __future__ import annotations

from types import SimpleNamespace

import pytest

from mpilot.acquisition.domain.save_paths import validate_save_path_override


def _settings(**overrides):
    values = {
        "qbitlarr_save_path_movie": "/downloads/movies",
        "qbitlarr_save_path_movie_4k": "/downloads/movies-4k",
        "qbitlarr_save_path_tv": "/downloads/tv",
        "qbitlarr_extra_save_paths": ["/media/Kids"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "save_path",
    [
        "/downloads/movies/../../outside",
        "/downloads/movies/.",
        "/downloads/movies/../movies",
        "/downloads/movies\\..\\outside",
        "downloads/movies",
        "/downloads/movies-evil",
    ],
)
def test_save_path_override_rejects_non_canonical_paths_outside_allowed_roots(save_path):
    with pytest.raises(ValueError, match="save_path must be inside"):
        validate_save_path_override(save_path, _settings())


def test_save_path_override_returns_canonical_posix_path_for_allowed_child():
    assert validate_save_path_override("/downloads/movies//Movie Folder/", _settings()) == "/downloads/movies/Movie Folder"
