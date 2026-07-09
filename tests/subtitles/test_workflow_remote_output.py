import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mpilot.subtitles.plex_resolver import PlexResolvedMedia
from mpilot.subtitles.source import RemoteSubtitleCommandRunner, SubtitleStream
from mpilot.subtitles.workflow import WorkflowOptions, translate_plex_resolved, translate_video_file


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:02,000
Hello there.

"""


class WorkflowRemoteOutputTests(unittest.TestCase):
    def test_translate_video_writes_output_remotely_when_local_media_library_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"not a real video because sidecar wins")
            source = root / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            remote_output = Path("/mnt/media/Movies/Movie.zh.ass")
            events = []
            copied = {}

            def fake_copy(_runner, source_path, destination_path, force=False):
                copied["destination"] = destination_path
                copied["force"] = force
                copied["text"] = Path(source_path).read_text(encoding="utf-8")

            env = {
                "MPILOT_SOURCE_REMOTE_SSH_HOST": "nas-host",
                "MPILOT_SOURCE_LOCAL_PATH_PREFIX": "/mnt/media",
                "MPILOT_SOURCE_REMOTE_PATH_PREFIX": "/server/media",
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(RemoteSubtitleCommandRunner, "ensure_remote_can_write", autospec=True) as ensure_write:
                    with patch.object(
                        RemoteSubtitleCommandRunner,
                        "copy_file_to_remote",
                        autospec=True,
                        side_effect=fake_copy,
                    ) as copy_file:
                        summary = translate_video_file(
                            video,
                            WorkflowOptions(
                                source_language="en",
                                target_language="zh",
                                output_mode="bilingual-ass",
                                backend="fake",
                                output=remote_output,
                                progress_callback=events.append,
                            ),
                        )

            ensure_write.assert_called_once()
            copy_file.assert_called_once()
            self.assertEqual(copied["destination"], remote_output)
            self.assertFalse(copied["force"])
            self.assertIn("Dialogue:", copied["text"])
            self.assertEqual(summary["output"], str(remote_output))
            self.assertEqual(summary["output_delivery"]["execution"], "remote")
            self.assertEqual(summary["output_delivery"]["remote_path"], "/server/media/Movies/Movie.zh.ass")
            self.assertEqual(
                [event["stage"] for event in events if event["stage"] in {"using_remote_output_writer", "writing_remote_output", "output_ready"}],
                ["using_remote_output_writer", "writing_remote_output", "output_ready"],
            )

    def test_translate_plex_write_back_uses_remote_writer_when_library_is_not_mounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / "work"
            local_prefix = root / "missing-media"
            local_video = local_prefix / "Movies" / "Movie.mkv"
            copied = {}
            ensured = []

            class FakeRemoteRunner:
                ssh_host = "nas-host"

                def can_handle(self, path):
                    return str(path).startswith(str(local_prefix))

                def remote_path_for(self, path):
                    value = str(path)
                    if not value.startswith(str(local_prefix)):
                        return None
                    return "/server/media" + value.removeprefix(str(local_prefix))

                def resolve_video_file_path(self, path):
                    return Path(path)

                def probe_subtitle_streams(self, _path):
                    return [SubtitleStream(index=2, codec_name="subrip", language="eng")]

                def extract_text_subtitle_to_srt(self, _video_path, _stream, output_path):
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(SAMPLE_SRT, encoding="utf-8")

                def ensure_remote_can_write(self, path, force=False):
                    ensured.append((Path(path), force))

                def copy_file_to_remote(self, source_path, destination_path, force=False):
                    copied["destination"] = Path(destination_path)
                    copied["force"] = force
                    copied["text"] = Path(source_path).read_text(encoding="utf-8")

            resolved = PlexResolvedMedia(
                rating_key="1468",
                title="Movie",
                media_type="movie",
                plex_file="/server/media/Movies/Movie.mkv",
                local_file=str(local_video),
                path_mapping_applied=True,
                imdb="tt1234567",
            )
            runner = FakeRemoteRunner()

            with patch.object(RemoteSubtitleCommandRunner, "from_env", return_value=runner):
                summary = translate_plex_resolved(
                    resolved,
                    WorkflowOptions(
                        source_language="en",
                        target_language="zh",
                        output_mode="bilingual-ass",
                        backend="fake",
                        work_dir=work_dir,
                        write_back=True,
                    ),
                )

            expected_write_back = local_prefix / "Movies" / "Movie.zh.ass"
            self.assertEqual(copied["destination"], expected_write_back)
            self.assertFalse(copied["force"])
            self.assertIn("Dialogue:", copied["text"])
            self.assertIn((expected_write_back, False), ensured)
            self.assertEqual(summary["write_back"]["execution"], "remote")
            self.assertEqual(summary["write_back"]["remote_path"], "/server/media/Movies/Movie.zh.ass")
            self.assertFalse(local_prefix.exists())


if __name__ == "__main__":
    unittest.main()
