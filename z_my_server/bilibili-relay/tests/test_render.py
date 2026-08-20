import logging
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))

import render


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.publish_path = Path(self.temp.name) / "history" / "index.html"
        self.cfg = {"paths": {"publish_path": str(self.publish_path)}}
        self.log = logging.getLogger("test")

    def tearDown(self):
        self.temp.cleanup()

    def test_hostile_title_escaped_and_link_uses_bvid(self):
        catalog = {"BV1X57q6uESc": {
            "bvid": "BV1X57q6uESc", "title": "<script>alert(1)</script>",
            "image": "/bilibili/history/images/a.jpg", "duration_seconds": 784,
            "first_played_at": "2026-07-17 12:00", "last_played_at": "2026-07-17 12:00",
            "play_count": 1, "pull_count": 2,
        }}
        render.render_and_publish(self.cfg, catalog, self.log)
        html = self.publish_path.read_text(encoding="utf-8")
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("https://www.bilibili.com/video/BV1X57q6uESc", html)
        self.assertIn("转发用户：1", html)
        self.assertIn("观看次数：2 次", html)
        self.assertIn("13分4秒", html)
        self.assertIn("东云资讯 v1.1.1", html)

    def test_pull_only_entry_with_null_played_at_sorts_safely(self):
        catalog = {
            "BV_pull": {"bvid": "BV_pull", "title": "拉流优先", "image": "",
                        "first_played_at": None, "last_played_at": None,
                        "play_count": 0, "pull_count": 1},
            "BV_play": {"bvid": "BV_play", "title": "已播放", "image": "",
                        "first_played_at": "2026-07-17 12:00", "last_played_at": "2026-07-17 12:00",
                        "play_count": 1, "pull_count": 1},
        }
        render.render_and_publish(self.cfg, catalog, self.log)
        html = self.publish_path.read_text(encoding="utf-8")
        self.assertLess(html.index("已播放"), html.index("拉流优先"))

    def test_empty_catalog(self):
        render.render_and_publish(self.cfg, {}, self.log)
        self.assertIn("暂无播放记录", self.publish_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
