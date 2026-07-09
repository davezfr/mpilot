import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from babelarr.srt import Cue
from babelarr.translate import Chunk, TranslationOptions, run_codex_cli, translate_cues


class TranslateBackendTests(unittest.TestCase):
    def test_translate_cues_runs_eight_chunks_concurrently_and_preserves_output_order(self):
        source_cues = [
            Cue(
                number=str(index),
                start="00:00:01,000",
                end="00:00:02,000",
                text_lines=["Line %s" % index],
            )
            for index in range(1, 1001)
        ]
        lock = threading.Lock()
        first_wave_started = 0
        first_wave_release = threading.Event()
        active_chunks = 0
        max_active_chunks = 0

        def fake_backend(options, chunk, prompt, schema_path, chunk_dir):
            nonlocal active_chunks, first_wave_started, max_active_chunks
            with lock:
                active_chunks += 1
                if chunk.index <= 8:
                    first_wave_started += 1
                    if first_wave_started == 8:
                        first_wave_release.set()
                max_active_chunks = max(max_active_chunks, active_chunks)
            try:
                if chunk.index <= 8:
                    first_wave_release.wait(0.5)
                if chunk.index == 1:
                    time.sleep(0.02)
                else:
                    time.sleep(0.005)
                return {
                    "translations": [
                        {
                            "number": cue.number,
                            "translation": "chunk-%03d-cue-%s" % (chunk.index, cue.number),
                        }
                        for cue in chunk.cues
                    ]
                }
            finally:
                with lock:
                    active_chunks -= 1

        with tempfile.TemporaryDirectory() as tmp, patch(
            "babelarr.translate.run_backend",
            side_effect=fake_backend,
        ), patch("babelarr.translate._stagger_codex_chunk_start"):
            run = translate_cues(
                source_cues,
                TranslationOptions(
                    source_language="en",
                    target_language="zh",
                    backend="codex-cli",
                    work_dir=Path(tmp),
                ),
            )

        self.assertEqual(run.summary["chunks"], 10)
        self.assertEqual(run.summary["max_concurrent_chunks"], 8)
        self.assertEqual(max_active_chunks, 8)
        self.assertEqual([summary["index"] for summary in run.summary["chunk_summaries"]], list(range(1, 11)))
        self.assertEqual(run.cues[0].text, "chunk-001-cue-1")
        self.assertEqual(run.cues[100].text, "chunk-002-cue-101")
        self.assertEqual(run.cues[-1].text, "chunk-010-cue-1000")

    def test_codex_cli_chunk_submission_is_staggered_between_half_and_one_second(self):
        source_cues = [
            Cue(
                number=str(index),
                start="00:00:01,000",
                end="00:00:02,000",
                text_lines=["Line %s" % index],
            )
            for index in range(1, 301)
        ]

        def fake_backend(options, chunk, prompt, schema_path, chunk_dir):
            return {
                "translations": [
                    {
                        "number": cue.number,
                        "translation": "chunk-%03d-cue-%s" % (chunk.index, cue.number),
                    }
                    for cue in chunk.cues
                ]
            }

        with tempfile.TemporaryDirectory() as tmp, patch(
            "babelarr.translate.run_backend",
            side_effect=fake_backend,
        ), patch("babelarr.translate.time.sleep") as sleep:
            translate_cues(
                source_cues,
                TranslationOptions(
                    source_language="en",
                    target_language="zh",
                    backend="codex-cli",
                    work_dir=Path(tmp),
                ),
            )

        self.assertEqual(sleep.call_count, 2)
        for call in sleep.call_args_list:
            delay = call.args[0]
            self.assertGreaterEqual(delay, 0.5)
            self.assertLessEqual(delay, 1.0)

    def test_codex_cli_removes_copied_auth_from_kept_work_dir_and_uses_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            auth = home / ".codex" / "auth.json"
            auth.parent.mkdir(parents=True)
            auth.write_text('{"token":"secret"}', encoding="utf-8")
            chunk_dir = root / "work" / "chunk-001"
            chunk_dir.mkdir(parents=True)
            schema_path = root / "work" / "translation.schema.json"
            schema_path.write_text("{}", encoding="utf-8")
            calls = []

            def fake_run(command, stdin, stdout, stderr, env, timeout):
                calls.append(
                    {
                        "command": list(command),
                        "env": dict(env),
                        "timeout": timeout,
                    }
                )
                output_path = Path(command[command.index("-o") + 1])
                output_path.write_text(
                    json.dumps({"translations": [{"number": "1", "translation": "Bonjour"}]}),
                    encoding="utf-8",
                )
                return CompletedProcess(command, 0)

            options = TranslationOptions(
                source_language="en",
                target_language="fr",
                backend="codex-cli",
                keep_work_dir=True,
                codex_timeout=12,
            )
            chunk = Chunk(index=1, cues=[Cue(number="1", start="00:00:01,000", end="00:00:02,000", text_lines=["Hello"])])

            with patch("babelarr.translate.Path.home", return_value=home), patch(
                "babelarr.translate.subprocess.run",
                side_effect=fake_run,
            ):
                result = run_codex_cli(options, chunk, "prompt", schema_path, chunk_dir)

            self.assertEqual(result["translations"][0]["translation"], "Bonjour")
            self.assertEqual(calls[0]["timeout"], 12)
            self.assertFalse((chunk_dir / "codex-home" / "auth.json").exists())


if __name__ == "__main__":
    unittest.main()
