import tempfile
import unittest
from pathlib import Path

from babelarr.normalize import normalize_to_srt, parse_microdvd_text
from babelarr.srt import Cue


class NormalizeTests(unittest.TestCase):
    def test_parse_microdvd_uses_declared_fps_and_pipe_line_breaks(self):
        source = """{1}{1}23.976
{24}{48}Hello|there.
{72}{96}Next line
"""

        cues = parse_microdvd_text(source)

        self.assertEqual(
            cues,
            [
                Cue("1", "00:00:01,001", "00:00:02,002", ["Hello", "there."]),
                Cue("2", "00:00:03,003", "00:00:04,004", ["Next line"]),
            ],
        )

    def test_normalize_microdvd_sub_writes_temporary_srt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Movie.en.sub"
            source.write_text("{1}{1}25.0\n{25}{50}Hello\n", encoding="utf-8")

            normalized = normalize_to_srt(source, root / "work")

            self.assertTrue(normalized.temporary)
            self.assertEqual(normalized.path, root / "work" / "Movie.en.normalized.srt")
            self.assertEqual(normalized.cues, [Cue("1", "00:00:01,000", "00:00:02,000", ["Hello"])])
            self.assertEqual(
                normalized.path.read_text(encoding="utf-8"),
                "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n",
            )

    def test_binary_sub_is_rejected_instead_of_treated_as_srt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Movie.en.sub"
            source.write_bytes(b"\x00VobSub\x00not text")

            with self.assertRaisesRegex(ValueError, "only MicroDVD text .sub"):
                normalize_to_srt(source, root / "work")


if __name__ == "__main__":
    unittest.main()
