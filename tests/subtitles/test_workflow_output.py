import unittest

from babelarr.srt import Cue
from babelarr.workflow import prepare_target_cues_for_output


class WorkflowOutputTests(unittest.TestCase):
    def test_chinese_output_preparation_preserves_punctuation_only_translation(self):
        cues = [
            Cue(
                "734",
                "00:12:14,000",
                "00:12:15,000",
                ["。"],
            )
        ]

        prepared = prepare_target_cues_for_output(cues, "zh")

        self.assertEqual(prepared[0].text_lines, ["。"])


if __name__ == "__main__":
    unittest.main()
