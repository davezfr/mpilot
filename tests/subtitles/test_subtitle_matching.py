import unittest

from mpilot.subtitles.providers.base import SubtitleCandidate
from mpilot.subtitles.subtitle_matching import (
    ReleaseInfo,
    confidence_for_score,
    parse_release_info,
    rank_subtitle_candidates,
    score_subtitle_candidate,
)


def candidate(release_name, provider="subdl", score=None):
    return SubtitleCandidate(
        provider=provider,
        provider_id="%s:%s" % (provider, release_name),
        language="en",
        release_name=release_name,
        file_name=release_name + ".srt",
        score=score,
        download={"method": "direct-url", "url": "https://example.test/%s.zip" % release_name},
    )


class SubtitleMatchingTests(unittest.TestCase):
    def test_parses_web_family_from_common_release_names(self):
        examples = [
            "Movie.2018.1080p.WEBRip.x264-GRP",
            "Movie.2018.720p.WEB-DL.DDP5.1.x264-GRP",
            "Movie.2018.1080p.NF.WEB-DL.DDP5.1.x264-GRP",
            "Movie.2018.2160p.AMZN.WEB.x265-GRP",
        ]

        for value in examples:
            with self.subTest(value=value):
                self.assertEqual(parse_release_info(value).source_family, "web")

    def test_parses_bluray_family_from_common_release_names(self):
        examples = [
            "Movie.2018.1080p.BluRay.x264-GRP",
            "Movie.2018.720p.BDRip.x264-GRP",
            "Movie.2018.1080p.BRRip.x264-GRP",
            "Movie.2018.2160p.REMUX.HEVC-GRP",
        ]

        for value in examples:
            with self.subTest(value=value):
                self.assertEqual(parse_release_info(value).source_family, "bluray")

    def test_webdl_and_webrip_are_high_confidence_even_with_resolution_difference(self):
        media = parse_release_info("The.Ballad.Of.Buster.Scruggs.2018.1080p.WEBRip.x264-[YTS.AM].mp4")
        subtitle = candidate("The.Ballad.of.Buster.Scruggs.2018.720p.NF.WEB-DL.DDP5.1.x264-KamiKaze")

        result = score_subtitle_candidate(media, subtitle)

        self.assertGreaterEqual(result.score, 80)
        self.assertEqual(result.confidence, "high")
        self.assertIn("same_source_family:web", result.reasons)

    def test_web_to_bluray_is_low_confidence_even_when_title_and_year_match(self):
        media = parse_release_info("Movie.2018.1080p.WEBRip.x264-GRP")
        subtitle = candidate("Movie.2018.1080p.BluRay.x264-GRP")

        result = score_subtitle_candidate(media, subtitle)

        self.assertLess(result.score, 40)
        self.assertEqual(result.confidence, "low")
        self.assertIn("source_family_mismatch:web->bluray", result.reasons)

    def test_bluray_to_bluray_is_high_confidence_despite_resolution_difference(self):
        media = parse_release_info("Movie.2018.2160p.BluRay.x265-GRP")
        subtitle = candidate("Movie.2018.720p.BDRip.x264-OTHER")

        result = score_subtitle_candidate(media, subtitle)

        self.assertGreaterEqual(result.score, 70)
        self.assertEqual(result.confidence, "high")
        self.assertIn("same_source_family:bluray", result.reasons)

    def test_resolution_and_codec_do_not_override_source_family_mismatch(self):
        media = parse_release_info("Movie.2018.1080p.WEBRip.x264-GRP")
        subtitle = candidate("Movie.2018.1080p.BluRay.x264-GRP")

        result = score_subtitle_candidate(media, subtitle)

        self.assertLess(result.score, 40)
        self.assertEqual(result.confidence, "low")

    def test_edition_mismatch_is_low_confidence_for_directors_cut(self):
        media = parse_release_info("Movie.2018.1080p.WEBRip.x264-GRP")
        subtitle = candidate("Movie.2018.Directors.Cut.1080p.WEBRip.x264-GRP")

        result = score_subtitle_candidate(media, subtitle)

        self.assertLess(result.score, 40)
        self.assertEqual(result.confidence, "low")
        self.assertIn("edition_mismatch:director", result.reasons)

    def test_same_edition_keeps_high_confidence(self):
        media = parse_release_info("Movie.2018.Extended.1080p.WEBRip.x264-GRP")
        subtitle = candidate("Movie.2018.Extended.720p.WEB-DL.x264-OTHER")

        result = score_subtitle_candidate(media, subtitle)

        self.assertGreaterEqual(result.score, 80)
        self.assertEqual(result.confidence, "high")
        self.assertIn("same_edition:extended", result.reasons)

    def test_exact_release_name_beats_provider_priority(self):
        media = parse_release_info("Movie.2018.1080p.WEBRip.x264-YTS")
        exact_opensubtitles = candidate("Movie.2018.1080p.WEBRip.x264-YTS", provider="opensubtitles")
        weaker_subdl = candidate("Movie.2018.1080p.WEBRip.x264-OTHER", provider="subdl")

        ranked = rank_subtitle_candidates(media, [weaker_subdl, exact_opensubtitles])

        self.assertEqual(ranked[0].candidate.provider, "opensubtitles")
        self.assertIn("exact_release_match", ranked[0].reasons)

    def test_provider_priority_breaks_close_ties_only(self):
        media = parse_release_info("Movie.2018.1080p.WEBRip.x264-YTS")
        subdl_candidate = candidate("Movie.2018.720p.WEB-DL.x264-OTHER", provider="subdl")
        opensubtitles_candidate = candidate("Movie.2018.720p.WEB-DL.x264-OTHER", provider="opensubtitles")

        ranked = rank_subtitle_candidates(media, [opensubtitles_candidate, subdl_candidate])

        self.assertEqual(ranked[0].candidate.provider, "subdl")
        self.assertEqual(ranked[0].score, ranked[1].score)

    def test_confidence_thresholds_are_explicit(self):
        self.assertEqual(confidence_for_score(80), "high")
        self.assertEqual(confidence_for_score(60), "medium")
        self.assertEqual(confidence_for_score(39), "low")

    def test_release_info_title_tokens_ignore_resolution_source_codec_and_group(self):
        info = parse_release_info("The.Ballad.Of.Buster.Scruggs.2018.1080p.WEBRip.x264-[YTS.AM].mp4")

        self.assertEqual(info.year, 2018)
        self.assertEqual(info.source_family, "web")
        self.assertEqual(info.resolution, "1080p")
        self.assertEqual(info.codec, "x264")
        self.assertEqual(info.group, "yts.am")
        self.assertEqual(info.title_tokens, ("ballad", "buster", "scruggs"))

    def test_manual_release_info_can_be_scored_without_filename_parsing(self):
        media = ReleaseInfo(
            raw="Movie",
            title_tokens=("movie",),
            year=2018,
            season=None,
            episode=None,
            source_family="web",
            resolution=None,
            codec=None,
            group=None,
            editions=frozenset(),
        )

        result = score_subtitle_candidate(media, candidate("Movie.2018.WEB-DL-GRP"))

        self.assertEqual(result.confidence, "high")


if __name__ == "__main__":
    unittest.main()
