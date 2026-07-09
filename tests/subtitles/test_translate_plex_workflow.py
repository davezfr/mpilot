import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mpilot.subtitles.cli import build_parser
from mpilot.subtitles.plex_resolver import PlexResolvedMedia
from mpilot.subtitles.provider_policy import ProviderDownloadSelection
from mpilot.subtitles.providers.base import DownloadedSubtitle, SubtitleCandidate
from mpilot.subtitles.source import AcquisitionResult, AcquisitionStatus
from mpilot.subtitles.workflow import WorkflowOptions, translate_plex_resolved


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:02,000
Hello there.

2
00:00:03,000 --> 00:00:04,000
How are you?

"""


class TranslatePlexWorkflowTests(unittest.TestCase):
    def test_translate_plex_parser_defaults_to_primary_use_case_but_stays_overridable(self):
        parser = build_parser()

        defaults = parser.parse_args(
            [
                "translate-plex",
                "--rating-key",
                "1468",
                "--plex-base-url",
                "http://127.0.0.1:32400",
                "--plex-token",
                "token",
            ]
        )
        overridden = parser.parse_args(
            [
                "translate-plex",
                "--rating-key",
                "1468",
                "--plex-base-url",
                "http://127.0.0.1:32400",
                "--plex-token",
                "token",
                "--source-language",
                "fr",
                "--target-language",
                "zh",
                "--output-mode",
                "single-srt",
            ]
        )

        self.assertEqual(defaults.source_language, "en")
        self.assertEqual(defaults.target_language, "zh")
        self.assertEqual(defaults.output_mode, "bilingual-ass")
        self.assertFalse(defaults.no_online_subtitle_fallback)
        self.assertFalse(defaults.assume_unlabeled_stream_language)
        self.assertEqual(defaults.subtitle_provider, "all")
        self.assertEqual(defaults.download_provider_priority, "subdl,opensubtitles")
        self.assertEqual(overridden.source_language, "fr")
        self.assertEqual(overridden.target_language, "zh")
        self.assertEqual(overridden.output_mode, "single-srt")

        assume_unlabeled = parser.parse_args(
            [
                "translate-plex",
                "--rating-key",
                "1468",
                "--plex-base-url",
                "http://127.0.0.1:32400",
                "--plex-token",
                "token",
                "--assume-unlabeled-stream-language",
            ]
        )

        self.assertTrue(assume_unlabeled.assume_unlabeled_stream_language)

    def test_translate_plex_parser_defaults_to_staging_and_accepts_write_back_controls(self):
        parser = build_parser()

        defaults = parser.parse_args(
            [
                "translate-plex",
                "--rating-key",
                "1468",
                "--plex-base-url",
                "http://127.0.0.1:32400",
                "--plex-token",
                "token",
            ]
        )
        write_back = parser.parse_args(
            [
                "translate-plex",
                "--rating-key",
                "1468",
                "--plex-base-url",
                "http://127.0.0.1:32400",
                "--plex-token",
                "token",
                "--write-back",
                "--refresh-plex",
            ]
        )

        self.assertFalse(defaults.write_back)
        self.assertFalse(defaults.refresh_plex)
        self.assertTrue(write_back.write_back)
        self.assertTrue(write_back.refresh_plex)

    def test_translate_plex_resolved_stages_output_by_default_without_write_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"not a real video because sidecar wins")
            sidecar = root / "Movie.en.srt"
            sidecar.write_text(SAMPLE_SRT, encoding="utf-8")
            resolved = PlexResolvedMedia(
                rating_key="1468",
                title="Movie",
                media_type="movie",
                plex_file="/server/media/Movies/Movie.mkv",
                local_file=str(video),
                path_mapping_applied=True,
                imdb="tt1234567",
            )

            summary = translate_plex_resolved(
                resolved,
                WorkflowOptions(
                    source_language="en",
                    target_language="zh",
                    output_mode="bilingual-ass",
                    backend="fake",
                    work_dir=root / "work",
                ),
            )

            output = root / "work" / "output" / "Movie.zh.ass"
            self.assertTrue(output.exists())
            self.assertFalse((root / "Movie.zh.ass").exists())
            self.assertEqual(summary["plex"]["ratingKey"], "1468")
            self.assertEqual(summary["input"], str(video))
            self.assertEqual(summary["output"], str(output))
            self.assertEqual(summary["write_back"]["requested"], False)
            self.assertEqual(summary["translation"]["source_acquisition"]["method"], "sidecar")
            self.assertIn("Style: Primary", output.read_text(encoding="utf-8"))
            self.assertIn("假译：Hello there", output.read_text(encoding="utf-8"))

    def test_translate_plex_resolved_writes_back_target_language_sidecar_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"not a real video because sidecar wins")
            sidecar = root / "Movie.en.srt"
            sidecar.write_text(SAMPLE_SRT, encoding="utf-8")
            resolved = PlexResolvedMedia(
                rating_key="1468",
                title="Movie",
                media_type="movie",
                plex_file="/server/media/Movies/Movie.mkv",
                local_file=str(video),
                path_mapping_applied=True,
                imdb="tt1234567",
            )

            summary = translate_plex_resolved(
                resolved,
                WorkflowOptions(
                    source_language="en",
                    target_language="zh",
                    output_mode="bilingual-ass",
                    backend="fake",
                    write_back=True,
                    work_dir=root / "work",
                ),
            )

            staged = root / "work" / "output" / "Movie.zh.ass"
            output = root / "Movie.zh.ass"
            self.assertTrue(staged.exists())
            self.assertTrue(output.exists())
            self.assertEqual(staged.read_text(encoding="utf-8"), output.read_text(encoding="utf-8"))
            self.assertEqual(summary["output"], str(staged))
            self.assertEqual(summary["write_back"]["requested"], True)
            self.assertEqual(summary["write_back"]["status"], "written")
            self.assertEqual(summary["write_back"]["path"], str(output))

    def test_translate_plex_resolved_copies_explicit_output_to_write_back_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"not a real video because sidecar wins")
            sidecar = root / "Movie.en.srt"
            sidecar.write_text(SAMPLE_SRT, encoding="utf-8")
            staged = root / "staged" / "Movie.preview.zh.ass"
            resolved = PlexResolvedMedia(
                rating_key="1468",
                title="Movie",
                media_type="movie",
                plex_file="/server/media/Movies/Movie.mkv",
                local_file=str(video),
                path_mapping_applied=True,
            )

            summary = translate_plex_resolved(
                resolved,
                WorkflowOptions(
                    source_language="en",
                    target_language="zh",
                    output_mode="bilingual-ass",
                    backend="fake",
                    output=staged,
                    write_back=True,
                ),
            )

            write_back = root / "Movie.zh.ass"
            self.assertTrue(staged.exists())
            self.assertTrue(write_back.exists())
            self.assertEqual(staged.read_text(encoding="utf-8"), write_back.read_text(encoding="utf-8"))
            self.assertEqual(summary["output"], str(staged))
            self.assertEqual(summary["write_back"]["path"], str(write_back))

    def test_translate_plex_resolved_keeps_no_overwrite_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"not a real video because sidecar wins")
            sidecar = root / "Movie.en.srt"
            sidecar.write_text(SAMPLE_SRT, encoding="utf-8")
            output = root / "Movie.zh.ass"
            output.write_text("keep me", encoding="utf-8")
            resolved = PlexResolvedMedia(
                rating_key="1468",
                title="Movie",
                media_type="movie",
                plex_file="/server/media/Movies/Movie.mkv",
                local_file=str(video),
                path_mapping_applied=True,
            )

            with self.assertRaisesRegex(FileExistsError, "output already exists"):
                translate_plex_resolved(
                    resolved,
                    WorkflowOptions(
                        source_language="en",
                        target_language="zh",
                        output_mode="bilingual-ass",
                        backend="fake",
                        write_back=True,
                    ),
                )

            self.assertEqual(output.read_text(encoding="utf-8"), "keep me")

    def test_translate_plex_resolved_refreshes_plex_after_write_back_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"not a real video because sidecar wins")
            sidecar = root / "Movie.en.srt"
            sidecar.write_text(SAMPLE_SRT, encoding="utf-8")
            resolved = PlexResolvedMedia(
                rating_key="1468",
                title="Movie",
                media_type="movie",
                plex_file="/server/media/Movies/Movie.mkv",
                local_file=str(video),
                path_mapping_applied=True,
                imdb="tt1234567",
                library_section_id="1",
            )
            calls = []

            def fake_refresher(resolved_media, write_back_path):
                calls.append((resolved_media, write_back_path))
                return {
                    "requested": True,
                    "status": "requested",
                    "method": "library-section-path-scan",
                    "library_section_id": resolved_media.library_section_id,
                    "path": "/server/media/Movies",
                }

            summary = translate_plex_resolved(
                resolved,
                WorkflowOptions(
                    source_language="en",
                    target_language="zh",
                    output_mode="bilingual-ass",
                    backend="fake",
                    write_back=True,
                    refresh_plex=True,
                    plex_refresher=fake_refresher,
                    work_dir=root / "work",
                ),
            )

            self.assertEqual(calls, [(resolved, root / "Movie.zh.ass")])
            self.assertEqual(summary["plex_refresh"]["status"], "requested")
            self.assertEqual(summary["plex_refresh"]["library_section_id"], "1")

    def test_translate_plex_refresh_requires_write_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"not a real video because sidecar wins")
            sidecar = root / "Movie.en.srt"
            sidecar.write_text(SAMPLE_SRT, encoding="utf-8")
            resolved = PlexResolvedMedia(
                rating_key="1468",
                title="Movie",
                media_type="movie",
                plex_file="/server/media/Movies/Movie.mkv",
                local_file=str(video),
                path_mapping_applied=True,
            )

            with self.assertRaisesRegex(ValueError, "--refresh-plex requires --write-back"):
                translate_plex_resolved(
                    resolved,
                    WorkflowOptions(
                        source_language="en",
                        target_language="zh",
                        output_mode="bilingual-ass",
                        backend="fake",
                        refresh_plex=True,
                    ),
                )


    def test_translate_plex_resolved_requires_local_file_to_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "Missing.mkv"
            resolved = PlexResolvedMedia(
                rating_key="1468",
                title="Missing",
                media_type="movie",
                plex_file="/server/media/Movies/Missing.mkv",
                local_file=str(missing),
                path_mapping_applied=True,
            )

            with self.assertRaisesRegex(FileNotFoundError, "resolved Plex local_file does not exist"):
                translate_plex_resolved(
                    resolved,
                    WorkflowOptions(
                        source_language="en",
                        target_language="zh",
                        output_mode="bilingual-ass",
                        backend="fake",
                    ),
                )

    def test_translate_plex_resolved_uses_online_provider_fallback_when_local_source_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"not a real video")
            resolved = PlexResolvedMedia(
                rating_key="392",
                title="Movie",
                media_type="movie",
                plex_file="/server/media/Movies/Movie.mkv",
                local_file=str(video),
                path_mapping_applied=True,
                imdb="tt1234567",
            )
            calls = []
            progress_events = []

            def fake_fetcher(resolved_media, source_language, output_dir, force=False):
                calls.append(
                    {
                        "resolved": resolved_media,
                        "source_language": source_language,
                        "output_dir": output_dir,
                        "force": force,
                    }
                )
                output_dir.mkdir(parents=True, exist_ok=True)
                downloaded = output_dir / "Movie.en.srt"
                downloaded.write_text(SAMPLE_SRT, encoding="utf-8")
                candidate = SubtitleCandidate(
                    provider="subdl",
                    provider_id="/subtitle/example",
                    language="en",
                    file_name="Movie.en.srt",
                    download={"url": "https://dl.subdl.com/subtitle/example"},
                )
                return ProviderDownloadSelection(
                    candidate=candidate,
                    download=DownloadedSubtitle(provider="subdl", path=downloaded),
                    attempts=[{"provider": "subdl", "provider_id": "/subtitle/example", "status": "ok"}],
                )

            with patch(
                "mpilot.subtitles.workflow.acquire_source_subtitle",
                return_value=AcquisitionResult(
                    status=AcquisitionStatus.NOT_FOUND,
                    method="none",
                    message="No source sidecar or embedded text subtitle was found.",
                ),
            ):
                summary = translate_plex_resolved(
                    resolved,
                    WorkflowOptions(
                        source_language="en",
                        target_language="zh",
                        output_mode="single-srt",
                        backend="fake",
                        work_dir=root / "work",
                        online_subtitle_fetcher=fake_fetcher,
                        progress_callback=progress_events.append,
                    ),
                )

            output = root / "Movie.zh.srt"
            staged_output = root / "work" / "output" / "Movie.zh.srt"
            self.assertFalse(output.exists())
            self.assertTrue(staged_output.exists())
            self.assertIn("假译：Hello there", staged_output.read_text(encoding="utf-8"))
            self.assertEqual(calls[0]["resolved"], resolved)
            self.assertEqual(calls[0]["source_language"], "en")
            self.assertEqual(calls[0]["output_dir"], root / "work" / "provider-source")
            self.assertFalse(calls[0]["force"])
            self.assertEqual(summary["translation"]["source_acquisition"]["method"], "provider:subdl")
            self.assertEqual(summary["translation"]["source_acquisition"]["provider_fallback"]["candidate"]["provider"], "subdl")
            stages = [event["stage"] for event in progress_events]
            self.assertIn("checking_local_subtitles", stages)
            self.assertIn("searching_online_subtitles", stages)
            self.assertIn("online_subtitle_selected", stages)
            self.assertIn("translating", stages)
            self.assertIn("rendering_output", stages)
            selected = next(event for event in progress_events if event["stage"] == "online_subtitle_selected")
            self.assertEqual(selected["details"]["provider"], "subdl")
            translating = [event for event in progress_events if event["stage"] == "translating"]
            self.assertEqual(translating[0]["details"]["total_chunks"], 1)
            self.assertEqual(translating[-1]["details"]["completed_chunks"], 1)

    def test_translate_plex_resolved_does_not_fetch_provider_when_output_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"not a real video")
            output = root / "Movie.zh.srt"
            output.write_text("keep me", encoding="utf-8")
            resolved = PlexResolvedMedia(
                rating_key="392",
                title="Movie",
                media_type="movie",
                plex_file="/server/media/Movies/Movie.mkv",
                local_file=str(video),
                path_mapping_applied=True,
                imdb="tt1234567",
            )
            calls = []

            def fake_fetcher(resolved_media, source_language, output_dir, force=False):
                calls.append(resolved_media)
                raise AssertionError("provider fetch should not run when output already exists")

            with patch(
                "mpilot.subtitles.workflow.acquire_source_subtitle",
                return_value=AcquisitionResult(
                    status=AcquisitionStatus.NOT_FOUND,
                    method="none",
                    message="No source sidecar or embedded text subtitle was found.",
                ),
            ):
                with self.assertRaisesRegex(FileExistsError, "output already exists"):
                    translate_plex_resolved(
                        resolved,
                        WorkflowOptions(
                            source_language="en",
                            target_language="zh",
                            output_mode="single-srt",
                            backend="fake",
                            work_dir=root / "work",
                            write_back=True,
                            online_subtitle_fetcher=fake_fetcher,
                        ),
                    )

            self.assertEqual(calls, [])
            self.assertEqual(output.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
