import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:02,000
Hello there.

2
00:00:03,000 --> 00:00:04,000
How are you?

"""

MULTILINE_SAMPLE_SRT = """1
00:00:01,000 --> 00:00:02,000
First
line.

"""


class WorkflowCliTests(unittest.TestCase):
    def run_cli(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, "-m", "mpilot.subtitles", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
        )

    def test_translate_srt_writes_single_language_srt_without_changing_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Movie.en.srt"
            output = root / "Movie.zh.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")

            result = self.run_cli(
                "translate-srt",
                str(source),
                "--source-language",
                "en",
                "--target-language",
                "zh",
                "--backend",
                "fake",
                "--output",
                str(output),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            text = output.read_text(encoding="utf-8")
            self.assertIn("00:00:01,000 --> 00:00:02,000", text)
            self.assertIn("假译：Hello there", text)

    def test_translate_srt_prettifies_chinese_target_before_single_language_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Movie.en.srt"
            output = root / "Movie.zh.srt"
            source.write_text(MULTILINE_SAMPLE_SRT, encoding="utf-8")

            result = self.run_cli(
                "translate-srt",
                str(source),
                "--source-language",
                "en",
                "--target-language",
                "zh",
                "--backend",
                "fake",
                "--output",
                str(output),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                """1
00:00:01,000 --> 00:00:02,000
假译：First line

""",
            )

    def test_translate_srt_keeps_non_chinese_target_line_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Movie.en.srt"
            output = root / "Movie.fr.srt"
            source.write_text(MULTILINE_SAMPLE_SRT, encoding="utf-8")

            result = self.run_cli(
                "translate-srt",
                str(source),
                "--source-language",
                "en",
                "--target-language",
                "fr",
                "--backend",
                "fake",
                "--output",
                str(output),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                """1
00:00:01,000 --> 00:00:02,000
假译：First
line.

""",
            )

    def test_translate_srt_bilingual_ass_uses_target_language_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")

            result = self.run_cli(
                "translate-srt",
                str(source),
                "--source-language",
                "en",
                "--target-language",
                "fr",
                "--backend",
                "fake",
                "--output-mode",
                "bilingual-ass",
            )

            output = root / "Movie.fr.ass"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
            self.assertFalse((root / "Movie.fr-en.ass").exists())
            self.assertFalse((root / "Movie.bilingual.ass").exists())
            self.assertIn("Style: Primary", output.read_text(encoding="utf-8"))

    def test_translate_srt_bilingual_ass_still_flattens_primary_and_secondary_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Movie.en.srt"
            source.write_text(MULTILINE_SAMPLE_SRT, encoding="utf-8")

            result = self.run_cli(
                "translate-srt",
                str(source),
                "--source-language",
                "en",
                "--target-language",
                "zh",
                "--backend",
                "fake",
                "--output-mode",
                "bilingual-ass",
            )

            output = root / "Movie.zh.ass"
            self.assertEqual(result.returncode, 0, result.stderr)
            ass = output.read_text(encoding="utf-8")
            self.assertIn("Dialogue: 1,0:00:01.00,0:00:02.00,Primary,,0,0,0,,假译：First line", ass)
            self.assertIn("Dialogue: 0,0:00:01.00,0:00:02.00,Secondary,,0,0,0,,First line", ass)
            self.assertNotIn(r"\N", ass)

    def test_translate_video_uses_source_sidecar_and_writes_plex_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"not a real video because sidecar wins")
            source = root / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")

            result = self.run_cli(
                "translate-video",
                str(video),
                "--source-language",
                "en",
                "--target-language",
                "zh",
                "--backend",
                "fake",
            )

            output = root / "Movie.zh.srt"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
            self.assertIn("假译：Hello there", output.read_text(encoding="utf-8"))

    def test_translate_video_converts_microdvd_sub_sidecar_before_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"not a real video because sidecar wins")
            source = root / "Movie.en.sub"
            source.write_text("{1}{1}25.0\n{25}{50}Hello there.\n", encoding="utf-8")

            result = self.run_cli(
                "translate-video",
                str(video),
                "--source-language",
                "en",
                "--target-language",
                "zh",
                "--backend",
                "fake",
            )

            output = root / "Movie.zh.srt"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
            self.assertIn("00:00:01,000 --> 00:00:02,000", output.read_text(encoding="utf-8"))
            self.assertIn("假译：Hello there", output.read_text(encoding="utf-8"))

    def test_translate_video_accepts_download_directory_and_uses_main_video_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = root / "Movie.Release"
            release.mkdir()
            (release / "sample.mp4").write_bytes(b"small")
            video = release / "Movie.Release.mkv"
            video.write_bytes(b"not a real video because sidecar wins")
            source = release / "Movie.Release.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")

            result = self.run_cli(
                "translate-video",
                str(release),
                "--source-language",
                "en",
                "--target-language",
                "zh",
                "--backend",
                "fake",
            )

            output = release / "Movie.Release.zh.srt"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
            self.assertIn("假译：Hello there", output.read_text(encoding="utf-8"))
            self.assertFalse((root / "Movie.Release.zh.srt").exists())

    def test_existing_output_is_not_overwritten_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Movie.en.srt"
            output = root / "Movie.zh.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            output.write_text("keep me", encoding="utf-8")

            result = self.run_cli(
                "translate-srt",
                str(source),
                "--source-language",
                "en",
                "--target-language",
                "zh",
                "--backend",
                "fake",
                "--output",
                str(output),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already exists", result.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
