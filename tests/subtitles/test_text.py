import unittest

from mpilot.subtitles.text import flatten_subtitle_lines, strip_terminal_statement_punctuation


class TextTests(unittest.TestCase):
    def test_flatten_subtitle_lines_joins_cjk_without_extra_space_and_strips_terminal_punctuation(self):
        self.assertEqual(
            flatten_subtitle_lines(["这是第一行", "这是第二行。"], clean_terminal=True),
            "这是第一行这是第二行",
        )

    def test_strip_terminal_statement_punctuation_preserves_questions_and_initialisms(self):
        self.assertEqual(strip_terminal_statement_punctuation("你好吗？"), "你好吗？")
        self.assertEqual(strip_terminal_statement_punctuation("U.S."), "U.S.")

    def test_clean_terminal_punctuation_preserves_punctuation_only_cues(self):
        self.assertEqual(strip_terminal_statement_punctuation("。"), "。")
        self.assertEqual(strip_terminal_statement_punctuation(","), ",")
        self.assertEqual(flatten_subtitle_lines(["。"], clean_terminal=True), "。")


if __name__ == "__main__":
    unittest.main()
