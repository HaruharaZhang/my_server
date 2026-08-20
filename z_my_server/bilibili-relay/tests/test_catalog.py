import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))
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
            "limits": {"image_max_bytes": 1000, "image_max_width": 100},
            "bilibili": {"allowed_thumbnail_host_suffix": ".hdslb.com"},
        }
        self.log = logging.getLogger("test")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_session_dedup_per_client(self):
        with patch.object(catalog, "_fetch_thumbnail", AsyncMock(return_value="/image.jpg")):
            self.assertEqual(await catalog.record_play(
                self.cfg, "BV1", "Title", "thumb", 125, "client-a", self.log,
                monotonic=lambda: 0), (True, True))
            self.assertEqual(await catalog.record_play(
                self.cfg, "BV1", "Title", "thumb", 125, "client-a", self.log,
                monotonic=lambda: 599), (False, False))
            await catalog.record_play(self.cfg, "BV1", "Title", "thumb", 125,
                                      "client-b", self.log, monotonic=lambda: 599)
            await catalog.record_play(self.cfg, "BV1", "Title", "thumb", 125,
                                      "client-a", self.log, monotonic=lambda: 600)
        entry = json.loads(Path(self.cfg["paths"]["catalog_path"]).read_text())["BV1"]
        self.assertEqual(entry["play_count"], 3)
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

        with patch.object(catalog, "direct_client", return_value=context), \
                patch.object(catalog.Image, "open", return_value=image):
            url = await catalog._fetch_thumbnail(
                "https://i1.hdslb.com/bfs/archive/a.jpg",
                self.cfg["paths"]["image_dir"], "BV1", 1000, 100, ".hdslb.com", self.log,
            )

        self.assertRegex(url, r"^/bilibili/history/images/[0-9a-f]{16}\.jpg$")

    async def test_disallowed_thumbnail_host_skipped(self):
        url = await catalog._fetch_thumbnail(
            "https://evil.com/a.jpg", self.cfg["paths"]["image_dir"],
            "BV1", 1000, 100, ".hdslb.com", self.log,
        )
        self.assertEqual(url, "")

    async def test_concurrent_updates_are_not_lost(self):
        with patch.object(catalog, "_fetch_thumbnail", AsyncMock(return_value="")):
            await asyncio.gather(
                *(catalog.record_play(
                    self.cfg, "BV1", "<script>x</script>", "", 60, f"c{i}",
                    self.log, monotonic=lambda: 1) for i in range(20)),
                *(catalog.record_pull(
                    self.cfg, "BV1", "Title", "", 60, self.log)
                  for _ in range(20)),
            )
        data = json.loads(Path(self.cfg["paths"]["catalog_path"]).read_text())
        self.assertEqual(data["BV1"]["play_count"], 20)
        self.assertEqual(data["BV1"]["pull_count"], 20)

    async def test_pull_count_is_independent_from_session_dedup(self):
        with patch.object(catalog, "_fetch_thumbnail", AsyncMock(return_value="")):
            await catalog.record_play(
                self.cfg, "BV1", "Title", "", 60, "client", self.log, monotonic=lambda: 1)
            await catalog.record_pull(self.cfg, "BV1", "Title", "", 60, self.log)
            self.assertEqual(await catalog.record_play(
                self.cfg, "BV1", "Title", "", 60, "client",
                self.log, monotonic=lambda: 2), (False, False))
            await catalog.record_pull(self.cfg, "BV1", "Title", "", 60, self.log)
        entry = catalog.load_catalog(self.cfg["paths"]["catalog_path"])["BV1"]
        self.assertEqual((entry["play_count"], entry["pull_count"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
