import unittest
import tempfile
import warnings
from pathlib import Path

from mpilot.subtitles.srt import Cue, format_srt, parse_srt_text, read_srt


class SrtTests(unittest.TestCase):
    def test_parse_srt_preserves_cue_ids_timestamps_and_text(self):
        source = """1
00:00:01,000 --> 00:00:02,500
Hello there.

2
00:00:03.000 --> 00:00:04.250
- How are you?
- Fine.
"""

        cues = parse_srt_text(source)

        self.assertEqual(
            cues,
            [
                Cue("1", "00:00:01,000", "00:00:02,500", ["Hello there."]),
                Cue("2", "00:00:03,000", "00:00:04,250", ["- How are you?", "- Fine."]),
            ],
        )

    def test_format_srt_uses_existing_timeline_with_replacement_text(self):
        cues = [
            Cue("1", "00:00:01,000", "00:00:02,500", ["你好"]),
            Cue("2", "00:00:03,000", "00:00:04,250", ["你好吗", "我很好"]),
        ]

        text = format_srt(cues)

        self.assertEqual(
            text,
            """1
00:00:01,000 --> 00:00:02,500
你好

2
00:00:03,000 --> 00:00:04,250
你好吗
我很好

""",
        )

    def test_malformed_srt_raises_clear_error(self):
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with self.assertRaisesRegex(ValueError, "no valid SRT cues"):
                parse_srt_text("1\nHello without timing\n")

        self.assertEqual(len(captured), 1)
        self.assertIn("missing timestamp row", str(captured[0].message))

    def test_parse_srt_accepts_blocks_without_numeric_cue_id(self):
        source = """00:00:01,000 --> 00:00:02,500
Hello there.

2
00:00:03.000 --> 00:00:04.250
Still numbered.
"""

        cues = parse_srt_text(source)

        self.assertEqual(
            cues,
            [
                Cue("1", "00:00:01,000", "00:00:02,500", ["Hello there."]),
                Cue("2", "00:00:03,000", "00:00:04,250", ["Still numbered."]),
            ],
        )

    def test_parse_srt_skips_malformed_and_empty_text_blocks(self):
        source = """1
This block has no timestamp.

2
00:00:03,000 --> 00:00:04,250
Valid subtitle.

3
00:00:05,000 --> 00:00:06,000
"""

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            cues = parse_srt_text(source)

        self.assertEqual(cues, [Cue("2", "00:00:03,000", "00:00:04,250", ["Valid subtitle."])])
        self.assertEqual(len(captured), 2)
        self.assertTrue(all("Skipping malformed SRT block" in str(item.message) for item in captured))

    def test_read_srt_decodes_utf16_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "subtitle.srt"
            path.write_text(
                """1
00:00:01,000 --> 00:00:02,500
Hello there.
""",
                encoding="utf-16",
            )

            cues = read_srt(path)

        self.assertEqual(cues, [Cue("1", "00:00:01,000", "00:00:02,500", ["Hello there."])])


if __name__ == "__main__":
    unittest.main()
