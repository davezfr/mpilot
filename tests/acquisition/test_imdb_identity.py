from __future__ import annotations

from mpilot.acquisition.domain.imdb_identity import validate_imdb_results
from mpilot.acquisition.models import SearchResult


def _result(title: str, *, source_imdb_id: str | None = None) -> SearchResult:
    return SearchResult(
        title=title,
        download_link=f"https://example.test/{abs(hash(title))}.torrent",
        seeders=10,
        source_imdb_id=source_imdb_id,
    )


def test_imdb_identity_gate_rejects_observed_unrelated_collections():
    correct = _result("Rush.2013.1080p.BluRay.x264", source_imdb_id="tt1979320")
    wrong_pack = _result("IMDB.Top.250.Movies.Collection.2013.1080p")
    unrelated_collection = _result("Amazing.Films.8.2013.1080p")

    validation = validate_imdb_results(
        [wrong_pack, unrelated_collection, correct],
        imdb_id="tt1979320",
        canonical_title="Rush",
        title_aliases=[],
        year=2013,
        media_type="movie",
    )

    assert [result.title for result in validation.verified_results] == [correct.title]
    assert validation.verified_results[0].verification_status == "imdb_verified"
    assert validation.verified_results[0].verification_reason == "source_imdb_title_year"
    assert validation.rejection_counts == {"title_mismatch": 2}


def test_imdb_identity_gate_accepts_canonical_alias_and_rejects_source_id_mismatch():
    alias_release = _result("Ford.v.Ferrari.2019.1080p.WEB-DL.H264")
    mismatched_source = _result(
        "Le.Mans.66.2019.1080p.WEB-DL.H264",
        source_imdb_id="tt1979320",
    )

    validation = validate_imdb_results(
        [alias_release, mismatched_source],
        imdb_id="tt1950186",
        canonical_title="Le Mans '66",
        title_aliases=["Ford v Ferrari"],
        year=2019,
        media_type="movie",
    )

    assert [result.title for result in validation.verified_results] == [alias_release.title]
    assert validation.rejection_counts == {"source_imdb_mismatch": 1}


def test_imdb_identity_gate_requires_contiguous_title_and_nonconflicting_year():
    separated_title = _result("Port.Amazing.Authority.2019.1080p")
    wrong_year = _result("Port.Authority.2020.1080p")
    conflicting_year = _result("Port.Authority.2019.Remaster.2024.1080p")
    collection = _result("Port.Authority.2019.IMDB.Top.250.Collection")

    validation = validate_imdb_results(
        [separated_title, wrong_year, conflicting_year, collection],
        imdb_id="tt7587282",
        canonical_title="Port Authority",
        title_aliases=None,
        year=2019,
        media_type="movie",
    )

    assert validation.verified_results == []
    assert validation.rejection_counts == {
        "collection_marker": 1,
        "conflicting_release_years": 1,
        "release_year_mismatch": 1,
        "title_mismatch": 1,
    }


def test_imdb_identity_gate_handles_year_in_movie_title_and_tv_without_release_year():
    movie = validate_imdb_results(
        [_result("1917.2019.1080p.BluRay")],
        imdb_id="tt8579674",
        canonical_title="1917",
        title_aliases=[],
        year=2019,
        media_type="movie",
    )
    tv = validate_imdb_results(
        [_result("Example.Show.S03.1080p.WEB-DL")],
        imdb_id="tt0017925",
        canonical_title="Example Show",
        title_aliases=[],
        year=None,
        media_type="tv",
    )

    assert len(movie.verified_results) == 1
    assert len(tv.verified_results) == 1
