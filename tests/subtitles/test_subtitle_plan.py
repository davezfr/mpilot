import tempfile
import unittest
from pathlib import Path

from babelarr.cli import build_parser
from babelarr.planner import build_subtitle_plan, infer_sidecar_language
from babelarr.plex_resolver import PlexResolvedMedia
from babelarr.source import SubtitleStream


def resolved_for(video: Path) -> PlexResolvedMedia:
    return PlexResolvedMedia(
        rating_key="1468",
        title="Movie",
        media_type="movie",
        plex_file="/server/media/Movies/Movie.mkv",
        local_file=str(video),
        path_mapping_applied=True,
    )


class SubtitlePlanTests(unittest.TestCase):
    def test_subtitle_plan_parser_requires_target_language_and_defaults_preferred_source(self):
        parser = build_parser()

        args = parser.parse_args(
            [
                "subtitle-plan",
                "--rating-key",
                "1468",
                "--target-language",
                "zh",
                "--plex-base-url",
                "http://127.0.0.1:32400",
                "--plex-token",
                "token",
            ]
        )

        self.assertEqual(args.target_language, "zh")
        self.assertEqual(args.preferred_source_language, "en")

    def test_plan_uses_existing_target_language_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            sidecar = root / "Movie.zh.srt"
            sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n\n", encoding="utf-8")

            plan = build_subtitle_plan(resolved_for(video), "zh", probe_runner=lambda _path: [])

            self.assertEqual(plan["proposal"]["action"], "use_existing")
            self.assertEqual(plan["proposal"]["target_language"], "zh")
            self.assertEqual(plan["proposal"]["source"]["kind"], "sidecar")
            self.assertEqual(plan["proposal"]["source"]["path"], str(sidecar))
            self.assertEqual(plan["events"][-1]["event"], "target_subtitle_found")

    def test_plan_proposes_translation_from_embedded_preferred_source_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            streams = [
                SubtitleStream(index=2, codec_name="subrip", language="eng", title="English"),
                SubtitleStream(index=3, codec_name="ass", language="fra", title="French"),
            ]

            plan = build_subtitle_plan(resolved_for(video), "zh", preferred_source_language="en", probe_runner=lambda _path: streams)

            self.assertEqual(plan["proposal"]["action"], "translate")
            self.assertEqual(plan["proposal"]["source_language"], "en")
            self.assertEqual(plan["proposal"]["target_language"], "zh")
            self.assertEqual(plan["proposal"]["source"]["kind"], "embedded-text")
            self.assertEqual(plan["proposal"]["source"]["stream_index"], 2)
            self.assertEqual(plan["proposal"]["output_modes"], ["single-srt", "bilingual-ass"])
            self.assertEqual(plan["events"][-1]["event"], "translation_source_found")

    def test_plan_respects_preferred_source_language_when_multiple_sources_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            streams = [
                SubtitleStream(index=2, codec_name="subrip", language="eng", title="English"),
                SubtitleStream(index=3, codec_name="ass", language="fra", title="French"),
            ]

            plan = build_subtitle_plan(resolved_for(video), "zh", preferred_source_language="fr", probe_runner=lambda _path: streams)

            self.assertEqual(plan["proposal"]["action"], "translate")
            self.assertEqual(plan["proposal"]["source_language"], "fr")
            self.assertEqual(plan["proposal"]["source"]["stream_index"], 3)

    def test_plan_requests_online_search_when_only_image_subtitles_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            streams = [SubtitleStream(index=2, codec_name="hdmv_pgs_subtitle", language="eng", title="English PGS")]

            plan = build_subtitle_plan(resolved_for(video), "zh", probe_runner=lambda _path: streams)

            self.assertEqual(plan["proposal"]["action"], "online_search")
            self.assertEqual(plan["proposal"]["reason"], "only_image_subtitles_found")
            self.assertEqual(plan["proposal"]["providers"], ["subdl", "opensubtitles"])
            self.assertEqual(plan["proposal"]["download_provider_priority"], ["subdl", "opensubtitles"])
            self.assertEqual(plan["events"][-1]["event"], "online_search_needed")
            self.assertEqual(plan["local_sources"]["embedded_image"][0]["codec"], "hdmv_pgs_subtitle")

    def test_plan_includes_available_source_languages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            streams = [
                SubtitleStream(index=2, codec_name="subrip", language="eng", title="English"),
                SubtitleStream(index=3, codec_name="ass", language="fra", title="French"),
            ]

            plan = build_subtitle_plan(resolved_for(video), "zh", probe_runner=lambda _path: streams)

            self.assertEqual(plan["available_source_languages"], ["en", "fr"])

    def test_plan_available_source_languages_excludes_target_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            streams = [
                SubtitleStream(index=2, codec_name="subrip", language="eng", title="English"),
                SubtitleStream(index=3, codec_name="subrip", language="zho", title="Chinese"),
            ]

            plan = build_subtitle_plan(resolved_for(video), "zh", probe_runner=lambda _path: streams)

            self.assertEqual(plan["available_source_languages"], ["en"])

    def test_plan_translate_proposal_flags_source_language_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            streams = [SubtitleStream(index=3, codec_name="ass", language="fra", title="French")]

            plan = build_subtitle_plan(resolved_for(video), "zh", preferred_source_language="en", probe_runner=lambda _path: streams)

            self.assertEqual(plan["proposal"]["action"], "translate")
            self.assertEqual(plan["proposal"]["source_language"], "fr")
            self.assertTrue(plan["proposal"]["confirmation_needed"])
            self.assertEqual(plan["proposal"]["confirmation_reason"], "source_language_mismatch")
            self.assertEqual(plan["proposal"]["preferred_source_language"], "en")

    def test_plan_translate_proposal_has_no_mismatch_when_source_matches_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            streams = [SubtitleStream(index=2, codec_name="subrip", language="eng", title="English")]

            plan = build_subtitle_plan(resolved_for(video), "zh", preferred_source_language="en", probe_runner=lambda _path: streams)

            self.assertNotIn("confirmation_needed", plan["proposal"])
            self.assertNotIn("confirmation_reason", plan["proposal"])

    def test_plan_requires_resolved_local_file_to_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "Missing.mkv"

            with self.assertRaisesRegex(FileNotFoundError, "resolved Plex local_file does not exist"):
                build_subtitle_plan(resolved_for(missing), "zh", probe_runner=lambda _path: [])

    def test_infer_sidecar_language_handles_case_and_language_words(self):
        self.assertEqual(infer_sidecar_language(Path("Movie.EN.SRT"), "Movie"), "en")
        self.assertEqual(infer_sidecar_language(Path("Movie.French.srt"), "Movie"), "fr")
        self.assertIsNone(infer_sidecar_language(Path("Other.en.srt"), "Movie"))


if __name__ == "__main__":
    unittest.main()
