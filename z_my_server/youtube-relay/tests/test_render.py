import logging
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))

import render


class RenderTests(unittest.TestCase):
    def test_cards_are_safe_links_and_legacy_pull_count_is_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "index.html"
            cfg = {"paths": {"publish_path": str(target)}}
            catalog = {
                "legacy_id": {
                    "video_id": "legacy_id", "title": "Legacy", "play_count": 3,
                    "last_played_at": "2026-01-01 00:00",
                },
                "new_id": {
                    "video_id": "new_id", "title": "New", "play_count": 4,
                    "pull_count": 2, "last_played_at": "2026-01-02 00:00",
                },
            }
            render.render_and_publish(cfg, catalog, logging.getLogger("test"))
            html = target.read_text(encoding="utf-8")

        self.assertIn('href="https://www.youtube.com/watch?v=legacy_id"', html)
        self.assertIn('target="_blank" rel="noopener noreferrer"', html)
        self.assertIn("转发用户：3 · 观看次数：待统计", html)
        self.assertIn("转发用户：4 · 观看次数：2 次", html)
        self.assertIn(".card:focus-visible", html)
        self.assertIn("东云资讯 v1.2.1", html)

    def test_pulled_but_never_played_entry_sorts_last_without_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "index.html"
            cfg = {"paths": {"publish_path": str(target)}}
            catalog = {
                "played_id": {
                    "video_id": "played_id", "title": "Played", "play_count": 1,
                    "last_played_at": "2026-01-01 00:00",
                },
                "pulled_id": {
                    "video_id": "pulled_id", "title": "PulledOnly", "play_count": 0,
                    "pull_count": 1, "first_played_at": None, "last_played_at": None,
                },
            }
            render.render_and_publish(cfg, catalog, logging.getLogger("test"))
            html = target.read_text(encoding="utf-8")

        self.assertLess(html.index("played_id"), html.index("pulled_id"))
        self.assertIn("最近播放：待确认", html)


if __name__ == "__main__":
    unittest.main()
