import tempfile
import unittest
from pathlib import Path

from babelarr.cli import build_parser
from babelarr.plex_resolver import PlexResolvedMedia
from babelarr.season_workflow import translate_plex_season
from babelarr.workflow import WorkflowOptions


def resolved_episode(root, episode):
    video = root / ("Show.S02E%02d.mkv" % episode)
    return PlexResolvedMedia(
        rating_key=str(2000 + episode),
        title="Episode %02d" % episode,
        media_type="episode",
        plex_file="/server/media/TV/Show/S02E%02d.mkv" % episode,
        local_file=str(video),
        path_mapping_applied=True,
        imdb="tt7654321",
        season=2,
        episode=episode,
        show_title="Show",
        library_section_id="2",
    )


class FakeSeasonResolver:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def resolve(self, imdb=None, rating_key=None, season=None, episode=None):
        self.calls.append(
            {
                "imdb": imdb,
                "rating_key": rating_key,
                "season": season,
                "episode": episode,
            }
        )
        return resolved_episode(self.root, episode)


class SeasonWorkflowTests(unittest.TestCase):
    def test_parser_accepts_explicit_episode_range_and_defaults_to_three_episode_batch(self):
        parser = build_parser()

        args = parser.parse_args(
            [
                "translate-plex-season",
                "--imdb",
                "tt7654321",
                "--season",
                "2",
                "--episode-start",
                "1",
                "--episode-end",
                "12",
                "--plex-base-url",
                "http://127.0.0.1:32400",
                "--plex-token",
                "token",
            ]
        )

        self.assertEqual(args.command, "translate-plex-season")
        self.assertEqual(args.imdb, "tt7654321")
        self.assertEqual(args.season, 2)
        self.assertEqual(args.episode_start, 1)
        self.assertEqual(args.episode_end, 12)
        self.assertEqual(args.batch_size, 3)
        self.assertEqual(args.source_language, "en")
        self.assertEqual(args.target_language, "zh")
        self.assertEqual(args.output_mode, "bilingual-ass")
        self.assertFalse(hasattr(args, "output"))

    def test_translate_plex_season_runs_only_first_three_episodes_serially(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolver = FakeSeasonResolver(root)
            runner_calls = []

            def fake_runner(resolved_media, options):
                runner_calls.append((resolved_media.episode, options.work_dir))
                return {
                    "plex": resolved_media.to_dict(),
                    "output": str(Path(options.work_dir) / "output" / ("Show.S02E%02d.zh.ass" % resolved_media.episode)),
                    "write_back": {"requested": False, "status": "not_requested"},
                }

            summary = translate_plex_season(
                resolver,
                imdb="tt7654321",
                rating_key=None,
                season=2,
                episode_start=1,
                episode_end=8,
                batch_size=3,
                options=WorkflowOptions(
                    source_language="en",
                    target_language="zh",
                    output_mode="bilingual-ass",
                    backend="fake",
                    work_dir=root / "season-work",
                ),
                episode_runner=fake_runner,
            )

            self.assertEqual([call["episode"] for call in resolver.calls], [1, 2, 3])
            self.assertEqual([episode for episode, _work_dir in runner_calls], [1, 2, 3])
            self.assertEqual(
                [work_dir.name for _episode, work_dir in runner_calls],
                ["S02E01", "S02E02", "S02E03"],
            )
            self.assertEqual(summary["status"], "batch_complete")
            self.assertEqual(summary["batch"]["episode_start"], 1)
            self.assertEqual(summary["batch"]["episode_end"], 3)
            self.assertEqual(summary["batch"]["limit"], 3)
            self.assertEqual(summary["requested"]["episode_start"], 1)
            self.assertEqual(summary["requested"]["episode_end"], 8)
            self.assertEqual(summary["deferred_episodes"], [4, 5, 6, 7, 8])
            self.assertEqual(summary["next_batch"]["episode_start"], 4)
            self.assertEqual(summary["next_batch"]["episode_end"], 8)
            self.assertEqual([job["status"] for job in summary["jobs"]], ["succeeded", "succeeded", "succeeded"])

    def test_translate_plex_season_stops_after_first_failed_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolver = FakeSeasonResolver(root)

            def failing_runner(resolved_media, options):
                if resolved_media.episode == 2:
                    raise RuntimeError("provider quota exhausted")
                return {
                    "plex": resolved_media.to_dict(),
                    "output": str(Path(options.work_dir) / "output" / ("Show.S02E%02d.zh.ass" % resolved_media.episode)),
                }

            summary = translate_plex_season(
                resolver,
                imdb="tt7654321",
                rating_key=None,
                season=2,
                episode_start=1,
                episode_end=5,
                batch_size=3,
                options=WorkflowOptions(
                    source_language="en",
                    target_language="zh",
                    output_mode="bilingual-ass",
                    backend="fake",
                    work_dir=root / "season-work",
                ),
                episode_runner=failing_runner,
            )

            self.assertEqual([call["episode"] for call in resolver.calls], [1, 2])
            self.assertEqual(summary["status"], "failed")
            self.assertIsNone(summary["next_batch"])
            self.assertEqual([job["status"] for job in summary["jobs"]], ["succeeded", "failed"])
            self.assertEqual(summary["jobs"][1]["error"]["message"], "provider quota exhausted")

    def test_translate_plex_season_validates_range_and_batch_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = FakeSeasonResolver(Path(tmp))
            options = WorkflowOptions(source_language="en", target_language="zh", backend="fake")

            with self.assertRaisesRegex(ValueError, "episode-start must be <= episode-end"):
                translate_plex_season(
                    resolver,
                    imdb="tt7654321",
                    rating_key=None,
                    season=2,
                    episode_start=4,
                    episode_end=3,
                    batch_size=3,
                    options=options,
                    episode_runner=lambda _resolved, _options: {},
                )
            with self.assertRaisesRegex(ValueError, "batch-size must be between 1 and 3"):
                translate_plex_season(
                    resolver,
                    imdb="tt7654321",
                    rating_key=None,
                    season=2,
                    episode_start=1,
                    episode_end=3,
                    batch_size=4,
                    options=options,
                    episode_runner=lambda _resolved, _options: {},
                )


if __name__ == "__main__":
    unittest.main()
