import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))
sys.modules.setdefault("httpx", MagicMock())
sys.modules.setdefault("PIL", MagicMock())
sys.modules.setdefault("PIL.Image", MagicMock())

import catalog


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        catalog._sessions.clear()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg = {
            "paths": {"catalog_path": str(root / "catalog.json"), "image_dir": str(root / "images")},
            "network": {"proxy": None},
            "limits": {"image_max_bytes": 1000, "image_max_width": 100},
            "youtube": {"allowed_thumbnail_hosts": ["i.ytimg.com"]},
        }
        self.log = logging.getLogger("test")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_session_dedup_quality_and_client(self):
        with patch.object(catalog, "_fetch_thumbnail", AsyncMock(return_value="/image.jpg")):
            self.assertEqual(await catalog.record_play(
                self.cfg, "video", "Title", "thumb", 125, "compat", "client-a", self.log,
                monotonic=lambda: 0), (True, True))
            self.assertEqual(await catalog.record_play(
                self.cfg, "video", "Title", "thumb", 125, "compat", "client-a", self.log,
                monotonic=lambda: 599), (False, False))
            await catalog.record_play(self.cfg, "video", "Title", "thumb", 125,
                                      "high", "client-a", self.log, monotonic=lambda: 599)
            await catalog.record_play(self.cfg, "video", "Title", "thumb", 125,
                                      "compat", "client-b", self.log, monotonic=lambda: 599)
            await catalog.record_play(self.cfg, "video", "Title", "thumb", 125,
                                      "compat", "client-a", self.log, monotonic=lambda: 600)
        entry = json.loads(Path(self.cfg["paths"]["catalog_path"]).read_text())["video"]
        self.assertEqual(entry["play_count"], 4)
        self.assertEqual(entry["last_quality"], "compat")
        self.assertNotIn("client", json.dumps(entry))

    async def test_thumbnail_uses_history_url(self):
        response = MagicMock()
        response.headers = {"content-type": "image/jpeg"}
        response.content = b"image"
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client
        image = MagicMock()
        image.width = 100
        image.height = 50
        image.convert.return_value = image

        with patch.object(catalog.httpx, "AsyncClient", return_value=context), \
                patch.object(catalog.Image, "open", return_value=image):
            url = await catalog._fetch_thumbnail(
                "https://i.ytimg.com/vi/video/default.jpg", None,
                self.cfg["paths"]["image_dir"], "video", 1000, 100,
                self.cfg["youtube"]["allowed_thumbnail_hosts"], self.log,
            )

        self.assertRegex(url, r"^/youtube/history/images/[0-9a-f]{16}\.jpg$")

    async def test_concurrent_updates_are_not_lost(self):
        with patch.object(catalog, "_fetch_thumbnail", AsyncMock(return_value="")):
            await asyncio.gather(
                *(catalog.record_play(
                    self.cfg, "video", "<script>x</script>", "", 60, "compat", f"c{i}",
                    self.log, monotonic=lambda: 1) for i in range(20)),
                *(catalog.record_pull(
                    self.cfg, "video", "Title", "", 60, "compat", self.log)
                  for _ in range(20)),
            )
        data = json.loads(Path(self.cfg["paths"]["catalog_path"]).read_text())
        self.assertEqual(data["video"]["play_count"], 20)
        self.assertEqual(data["video"]["pull_count"], 20)

    async def test_missing_new_fields_remain_compatible(self):
        Path(self.cfg["paths"]["catalog_path"]).write_text(json.dumps({"video": {
            "video_id": "video", "title": "Old", "image": "/old.jpg",
            "first_played_at": "2020", "last_played_at": "2020", "play_count": 7,
        }}))
        with patch.object(catalog, "_fetch_thumbnail", AsyncMock(return_value="")):
            await catalog.record_play(self.cfg, "video", "", "", None, "high", "new",
                                      self.log, monotonic=lambda: 1)
        entry = catalog.load_catalog(self.cfg["paths"]["catalog_path"])["video"]
        self.assertEqual((entry["title"], entry["image"], entry["play_count"]),
                         ("Old", "/old.jpg", 8))
        self.assertNotIn("pull_count", entry)

        await catalog.record_pull(self.cfg, "video", "", "", None, "high", self.log)
        entry = catalog.load_catalog(self.cfg["paths"]["catalog_path"])["video"]
        self.assertEqual(entry["pull_count"], 1)
        self.assertTrue(entry["pull_count_started_at"])

    async def test_pull_count_is_independent_from_session_dedup(self):
        with patch.object(catalog, "_fetch_thumbnail", AsyncMock(return_value="")):
            await catalog.record_play(
                self.cfg, "video", "Title", "", 60, "compat", "client",
                self.log, monotonic=lambda: 1)
            await catalog.record_pull(
                self.cfg, "video", "Title", "", 60, "compat", self.log)
            self.assertEqual(await catalog.record_play(
                self.cfg, "video", "Title", "", 60, "compat", "client",
                self.log, monotonic=lambda: 2), (False, False))
            await catalog.record_pull(
                self.cfg, "video", "Title", "", 60, "compat", self.log)
        entry = catalog.load_catalog(self.cfg["paths"]["catalog_path"])["video"]
        self.assertEqual((entry["play_count"], entry["pull_count"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
