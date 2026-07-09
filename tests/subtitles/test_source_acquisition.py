import tempfile
import unittest
import os
from subprocess import CompletedProcess
from pathlib import Path
from unittest.mock import patch

from mpilot.subtitles.source import (
    AcquisitionStatus,
    RemoteSubtitleCommandRunner,
    SubtitleStream,
    acquire_source_subtitle,
    build_ffprobe_command,
    find_source_sidecar,
    parse_ffmpeg_streams,
    probe_subtitle_streams,
    resolve_video_file_path,
    select_text_subtitle_stream,
)


class SourceAcquisitionTests(unittest.TestCase):
    def test_resolve_video_file_path_selects_largest_video_in_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = root / "Movie.Release"
            release.mkdir()
            (release / "sample.mp4").write_bytes(b"small")
            main = release / "Movie.Release.mkv"
            main.write_bytes(b"large-video")
            (release / "notes.txt").write_text("ignore", encoding="utf-8")

            self.assertEqual(resolve_video_file_path(release), main)

    def test_remote_runner_resolves_directory_to_largest_video_file(self):
        runner = RemoteSubtitleCommandRunner(
            ssh_host="nas-host",
            local_path_prefix="/mnt/media",
            remote_path_prefix="/server/media",
        )
        calls = []

        def fake_run(command, capture_output, timeout=None, text=False):
            calls.append(command)
            self.assertEqual(command[0], "ssh")
            self.assertIn("/server/media/Movies/Movie Folder", command[2])
            return CompletedProcess(
                command,
                0,
                stdout=(
                    "100\t/server/media/Movies/Movie Folder/sample.mp4\n"
                    "900\t/server/media/Movies/Movie Folder/Movie.mkv\n"
                ),
                stderr="",
            )

        with patch("mpilot.subtitles.source.subprocess.run", side_effect=fake_run):
            resolved = resolve_video_file_path(
                Path("/mnt/media/Movies/Movie Folder"),
                remote_runner=runner,
            )

        self.assertEqual(resolved, Path("/mnt/media/Movies/Movie Folder/Movie.mkv"))
        self.assertEqual(len(calls), 1)

    def test_remote_runner_falls_back_when_find_printf_is_unavailable(self):
        runner = RemoteSubtitleCommandRunner(
            ssh_host="nas-host",
            local_path_prefix="/mnt/media",
            remote_path_prefix="/server/media",
        )
        calls = []

        def fake_run(command, capture_output, timeout=None, text=False):
            calls.append(command)
            if len(calls) == 1:
                self.assertIn("-printf", command[2])
                return CompletedProcess(command, 1, stdout="", stderr="find: -printf: unknown primary")
            self.assertNotIn("-printf", command[2])
            return CompletedProcess(
                command,
                0,
                stdout=(
                    "100\t/server/media/Movies/Movie Folder/sample.mp4\n"
                    "900\t/server/media/Movies/Movie Folder/Movie.mkv\n"
                ),
                stderr="",
            )

        with patch("mpilot.subtitles.source.subprocess.run", side_effect=fake_run):
            resolved = resolve_video_file_path(
                Path("/mnt/media/Movies/Movie Folder"),
                remote_runner=runner,
            )

        self.assertEqual(resolved, Path("/mnt/media/Movies/Movie Folder/Movie.mkv"))
        self.assertEqual(len(calls), 2)

    def test_remote_runner_falls_back_when_busybox_find_printf_is_unrecognized(self):
        runner = RemoteSubtitleCommandRunner(
            ssh_host="nas-host",
            local_path_prefix="/mnt/media",
            remote_path_prefix="/server/media",
        )
        calls = []

        def fake_run(command, capture_output, timeout=None, text=False):
            calls.append(command)
            if len(calls) == 1:
                self.assertIn("-printf", command[2])
                return CompletedProcess(command, 1, stdout="", stderr="find: unrecognized: -printf")
            self.assertNotIn("-printf", command[2])
            return CompletedProcess(
                command,
                0,
                stdout=(
                    "100\t/server/media/Movies/Movie Folder/sample.mp4\n"
                    "900\t/server/media/Movies/Movie Folder/Movie.mkv\n"
                ),
                stderr="",
            )

        with patch("mpilot.subtitles.source.subprocess.run", side_effect=fake_run):
            resolved = resolve_video_file_path(
                Path("/mnt/media/Movies/Movie Folder"),
                remote_runner=runner,
            )

        self.assertEqual(resolved, Path("/mnt/media/Movies/Movie Folder/Movie.mkv"))
        self.assertEqual(len(calls), 2)

    def test_find_source_sidecar_prefers_same_directory_language_srt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            sidecar = root / "Movie.en.srt"
            sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n", encoding="utf-8")

            self.assertEqual(find_source_sidecar(video, "English"), sidecar)

    def test_find_source_sidecar_accepts_microdvd_sub_when_srt_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            sidecar = root / "Movie.en.sub"
            sidecar.write_text("{1}{1}23.976\n{24}{48}Hello\n", encoding="utf-8")

            self.assertEqual(find_source_sidecar(video, "English"), sidecar)

    def test_find_source_sidecar_prefers_srt_over_sub(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            srt = root / "Movie.en.srt"
            sub = root / "Movie.en.sub"
            srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n", encoding="utf-8")
            sub.write_text("{1}{1}23.976\n{24}{48}Hello\n", encoding="utf-8")

            self.assertEqual(find_source_sidecar(video, "English"), srt)

    def test_find_source_sidecar_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            sidecar = root / "Movie.EN.SRT"
            sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n", encoding="utf-8")

            self.assertEqual(find_source_sidecar(video, "en"), sidecar)

    def test_acquire_source_subtitle_returns_sidecar_before_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            sidecar = root / "Movie.en.srt"
            sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n", encoding="utf-8")

            result = acquire_source_subtitle(video, "en", root / "work", probe_runner=lambda _path: (_ for _ in ()).throw(AssertionError("ffprobe should not run")))

            self.assertEqual(result.status, AcquisitionStatus.READY)
            self.assertEqual(result.method, "sidecar")
            self.assertEqual(result.path, sidecar)

    def test_select_text_stream_prefers_matching_language_text_track(self):
        streams = [
            SubtitleStream(index=2, codec_name="hdmv_pgs_subtitle", language="en", title="English PGS"),
            SubtitleStream(index=3, codec_name="subrip", language="fr", title="French"),
            SubtitleStream(index=4, codec_name="ass", language="en", title="English ASS"),
        ]

        selected = select_text_subtitle_stream(streams, "English")

        self.assertEqual(selected.index, 4)
        self.assertEqual(selected.codec_name, "ass")

    def test_select_text_stream_does_not_fallback_to_wrong_language(self):
        streams = [
            SubtitleStream(index=3, codec_name="subrip", language="fr", title="French"),
        ]

        selected = select_text_subtitle_stream(streams, "English")

        self.assertIsNone(selected)

    def test_select_text_stream_can_explicitly_assume_single_unlabeled_stream_language(self):
        streams = [
            SubtitleStream(index=3, codec_name="subrip", language=None, title=None),
        ]

        selected = select_text_subtitle_stream(streams, "English", assume_unlabeled_language=True)

        self.assertEqual(selected.index, 3)

    def test_select_text_stream_does_not_assume_unlabeled_stream_when_multiple_text_streams_exist(self):
        streams = [
            SubtitleStream(index=3, codec_name="subrip", language=None, title=None),
            SubtitleStream(index=4, codec_name="ass", language=None, title=None),
        ]

        selected = select_text_subtitle_stream(streams, "English", assume_unlabeled_language=True)

        self.assertIsNone(selected)

    def test_acquire_source_subtitle_reports_not_found_when_text_stream_language_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            streams = [SubtitleStream(index=3, codec_name="subrip", language="fr", title="French")]
            extracted = []

            result = acquire_source_subtitle(
                video,
                "en",
                root / "work",
                probe_runner=lambda _path: streams,
                extractor=lambda *_args: extracted.append(True),
            )

            self.assertEqual(result.status, AcquisitionStatus.NOT_FOUND)
            self.assertEqual(extracted, [])

    def test_acquire_source_subtitle_warns_when_assuming_unlabeled_stream_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            streams = [SubtitleStream(index=3, codec_name="subrip", language=None, title=None)]
            extracted = []

            result = acquire_source_subtitle(
                video,
                "en",
                root / "work",
                probe_runner=lambda _path: streams,
                extractor=lambda *_args: extracted.append(True),
                assume_unlabeled_stream_language=True,
            )

            self.assertEqual(result.status, AcquisitionStatus.READY)
            self.assertEqual(result.method, "embedded-text-unlabeled")
            self.assertIn("unlabeled", result.warning)
            self.assertEqual(extracted, [True])

    def test_acquire_source_subtitle_emits_sidecar_probe_and_extract_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            streams = [SubtitleStream(index=3, codec_name="subrip", language="en", title="English")]
            events = []

            def fake_extract(_video_path, _stream, output_path):
                output_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n", encoding="utf-8")

            result = acquire_source_subtitle(
                video,
                "en",
                root / "work",
                probe_runner=lambda _path: streams,
                extractor=fake_extract,
                progress_callback=events.append,
                executor_name="local",
            )

            self.assertEqual(result.status, AcquisitionStatus.READY)
            self.assertEqual(result.execution, "local")
            self.assertEqual(
                [event["stage"] for event in events],
                [
                    "checking_source_sidecar",
                    "probing_embedded_subtitles",
                    "extracting_embedded_subtitle",
                ],
            )
            self.assertEqual(events[1]["details"]["execution"], "local")
            self.assertEqual(events[2]["details"]["stream_index"], 3)

    def test_remote_subtitle_runner_uses_ssh_for_probe_and_stdout_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "Movie.en.srt"
            runner = RemoteSubtitleCommandRunner(
                ssh_host="nas-host",
                local_path_prefix="/mnt/media",
                remote_path_prefix="/server/media",
            )
            calls = []

            def fake_run(command, capture_output, timeout=None, text=False):
                calls.append((command, capture_output, timeout, text))
                self.assertEqual(command[0], "ssh")
                self.assertEqual(command[1], "nas-host")
                self.assertEqual(len(command), 3)
                remote_command = command[2]
                self.assertIn("/server/media/Movies/Movie.mkv", remote_command)
                if "/usr/bin/ffprobe" in remote_command:
                    return CompletedProcess(
                        command,
                        0,
                        stdout='{"streams":[{"index":3,"codec_name":"subrip","tags":{"language":"eng","title":"English"}}]}',
                        stderr="",
                    )
                self.assertIn("/usr/bin/ffmpeg", remote_command)
                self.assertIn("-f srt -", remote_command)
                return CompletedProcess(
                    command,
                    0,
                    stdout=b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n",
                    stderr=b"",
                )

            with patch("mpilot.subtitles.source.subprocess.run", side_effect=fake_run):
                streams = runner.probe_subtitle_streams(Path("/mnt/media/Movies/Movie.mkv"))
                runner.extract_text_subtitle_to_srt(
                    Path("/mnt/media/Movies/Movie.mkv"),
                    streams[0],
                    output,
                )

            self.assertEqual(streams, [SubtitleStream(index=3, codec_name="subrip", language="eng", title="English")])
            self.assertEqual(output.read_text(encoding="utf-8"), "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")
            self.assertEqual(len(calls), 2)

    def test_remote_runner_checks_and_copies_output_with_ssh(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Movie.zh.ass"
            source.write_text("subtitle output", encoding="utf-8")
            runner = RemoteSubtitleCommandRunner(
                ssh_host="nas-host",
                local_path_prefix="/mnt/media",
                remote_path_prefix="/server/media",
            )
            calls = []
            copied = []

            def fake_run(command, *args, **kwargs):
                calls.append((command, kwargs))
                self.assertEqual(command[0], "ssh")
                self.assertEqual(command[1], "nas-host")
                self.assertIn("/server/media/Movies/Movie.zh.ass", command[2])
                if "cat >" in command[2]:
                    self.assertIn("trap", command[2])
                    copied.append(kwargs["stdin"].read())
                    return CompletedProcess(command, 0, stdout=b"", stderr=b"")
                self.assertIn("mkdir -p", command[2])
                return CompletedProcess(command, 0, stdout="", stderr="")

            with patch("mpilot.subtitles.source.subprocess.run", side_effect=fake_run):
                runner.ensure_remote_can_write(Path("/mnt/media/Movies/Movie.zh.ass"))
                runner.copy_file_to_remote(source, Path("/mnt/media/Movies/Movie.zh.ass"))

            self.assertEqual(copied, [b"subtitle output"])
            self.assertEqual(len(calls), 2)

    def test_remote_runner_prefers_babelarr_env_names(self):
        env = {
            "BABELARR_SOURCE_REMOTE_SSH_HOST": "nas-host",
            "BABELARR_SOURCE_LOCAL_PATH_PREFIX": "/mnt/media",
            "BABELARR_SOURCE_REMOTE_PATH_PREFIX": "/server/media",
            "BABELARR_SOURCE_REMOTE_SSH_BIN": "ssh-custom",
            "BABELARR_SOURCE_REMOTE_FFPROBE": "/opt/bin/ffprobe",
            "BABELARR_SOURCE_REMOTE_FFMPEG": "/opt/bin/ffmpeg",
            "MST_SOURCE_REMOTE_SSH_HOST": "legacy-host",
            "MST_SOURCE_LOCAL_PATH_PREFIX": "/legacy/local",
            "MST_SOURCE_REMOTE_PATH_PREFIX": "/legacy/remote",
        }
        with patch.dict(os.environ, env, clear=False):
            runner = RemoteSubtitleCommandRunner.from_env()

        self.assertIsNotNone(runner)
        self.assertEqual(runner.ssh_host, "nas-host")
        self.assertEqual(runner.local_path_prefix, "/mnt/media")
        self.assertEqual(runner.remote_path_prefix, "/server/media")
        self.assertEqual(runner.ssh_bin, "ssh-custom")
        self.assertEqual(runner.ffprobe, "/opt/bin/ffprobe")
        self.assertEqual(runner.ffmpeg, "/opt/bin/ffmpeg")

    def test_remote_runner_prefers_mpilot_env_names(self):
        env = {
            "MPILOT_SOURCE_REMOTE_SSH_HOST": "mpilot-host",
            "MPILOT_SOURCE_LOCAL_PATH_PREFIX": "/mpilot/local",
            "MPILOT_SOURCE_REMOTE_PATH_PREFIX": "/mpilot/remote",
            "BABELARR_SOURCE_REMOTE_SSH_HOST": "babelarr-host",
            "BABELARR_SOURCE_LOCAL_PATH_PREFIX": "/babelarr/local",
            "BABELARR_SOURCE_REMOTE_PATH_PREFIX": "/babelarr/remote",
        }
        with patch.dict(os.environ, env, clear=False):
            runner = RemoteSubtitleCommandRunner.from_env()

        self.assertIsNotNone(runner)
        self.assertEqual(runner.ssh_host, "mpilot-host")
        self.assertEqual(runner.local_path_prefix, "/mpilot/local")
        self.assertEqual(runner.remote_path_prefix, "/mpilot/remote")

    def test_only_image_subtitles_are_unsupported_for_mvp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "Movie.mkv"
            video.write_bytes(b"")
            streams = [SubtitleStream(index=2, codec_name="hdmv_pgs_subtitle", language="en", title="English PGS")]

            result = acquire_source_subtitle(video, "en", root / "work", probe_runner=lambda _path: streams)

            self.assertEqual(result.status, AcquisitionStatus.UNSUPPORTED)
            self.assertIn("OCR", result.message)

    def test_ffprobe_command_selects_subtitle_streams_as_json(self):
        command = build_ffprobe_command(Path("Movie.mkv"))

        self.assertIn("-select_streams", command)
        self.assertIn("s", command)
        self.assertIn("-of", command)
        self.assertIn("json", command)

    def test_parse_ffmpeg_streams_reads_subtitle_lines(self):
        output = """
Input #0, matroska,webm, from 'Movie.mkv':
  Stream #0:0: Video: h264, 1280x720
  Stream #0:1(eng): Audio: aac, 48000 Hz, stereo (default)
  Stream #0:2(eng): Subtitle: subrip (default)
  Stream #0:3(fre): Subtitle: ass
"""

        streams = parse_ffmpeg_streams(output)

        self.assertEqual(
            streams,
            [
                SubtitleStream(index=2, codec_name="subrip", language="eng", title=None),
                SubtitleStream(index=3, codec_name="ass", language="fre", title=None),
            ],
        )

    def test_probe_subtitle_streams_falls_back_to_ffmpeg_when_ffprobe_is_missing(self):
        ffmpeg_output = "Stream #0:2(eng): Subtitle: subrip (default)\n"

        def fake_run(command, text, capture_output, timeout=None):
            if command[0] == "ffprobe":
                raise FileNotFoundError("ffprobe")
            self.assertEqual(command[0], "ffmpeg")
            return CompletedProcess(command, 1, stdout="", stderr=ffmpeg_output)

        with patch("mpilot.subtitles.source.subprocess.run", side_effect=fake_run):
            streams = probe_subtitle_streams(Path("Movie.mkv"))

        self.assertEqual(streams, [SubtitleStream(index=2, codec_name="subrip", language="eng", title=None)])


if __name__ == "__main__":
    unittest.main()
