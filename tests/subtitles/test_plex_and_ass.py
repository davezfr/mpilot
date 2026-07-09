import unittest
from pathlib import Path

from babelarr.ass import AssOptions, build_ass
from babelarr.languages import language_to_code
from babelarr.plex import plex_sidecar_path, strip_language_suffix
from babelarr.srt import Cue


class PlexAndAssTests(unittest.TestCase):
    def test_language_codes_use_plex_friendly_primary_language(self):
        self.assertEqual(language_to_code("Simplified Chinese"), "zh")
        self.assertEqual(language_to_code("Chinese"), "zh")
        self.assertEqual(language_to_code("French"), "fr")
        self.assertEqual(language_to_code("en"), "en")

    def test_plex_sidecar_path_uses_single_primary_language_code(self):
        video = Path("/media/Movie Name (2024).mkv")

        self.assertEqual(
            plex_sidecar_path(video, "zh", ".ass"),
            Path("/media/Movie Name (2024).zh.ass"),
        )
        self.assertNotIn("zh-en", plex_sidecar_path(video, "zh", ".ass").name)
        self.assertNotIn("bilingual", plex_sidecar_path(video, "zh", ".ass").name)
        self.assertNotIn(".cn.", plex_sidecar_path(video, "zh", ".ass").name)

    def test_strip_language_suffix_handles_dot_and_underscore_sidecars(self):
        self.assertEqual(strip_language_suffix(Path("Movie.en.srt"), ["en"]), "Movie")
        self.assertEqual(strip_language_suffix(Path("2_English.srt"), ["english"]), "2")
        self.assertEqual(strip_language_suffix(Path("Movie.srt"), ["en"]), "Movie")

    def test_bilingual_ass_uses_primary_and_secondary_styles(self):
        source = [Cue("1", "00:00:01,000", "00:00:02,000", ["Hello there."])]
        target = [Cue("1", "00:00:01,000", "00:00:02,000", ["你好。"])]

        ass = build_ass(source, target, AssOptions(mode="bilingual-ass", primary_script="cjk", secondary_script="latin", height=1080))

        self.assertIn("Style: Primary,PingFang SC,56", ass)
        self.assertIn("Style: Secondary,Arial,36", ass)
        self.assertIn("&H00D6F4FF", ass)
        self.assertIn("Dialogue: 1,0:00:01.00,0:00:02.00,Primary", ass)
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:02.00,Secondary", ass)
        self.assertIn("你好", ass)
        self.assertIn("Hello there", ass)

    def test_ass_rejects_timeline_mismatch(self):
        source = [Cue("1", "00:00:01,000", "00:00:02,000", ["Hello"])]
        target = [Cue("1", "00:00:01,500", "00:00:02,000", ["你好"])]

        with self.assertRaisesRegex(ValueError, "timestamp mismatch"):
            build_ass(source, target, AssOptions(mode="bilingual-ass"))


if __name__ == "__main__":
    unittest.main()
