import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))
sys.modules.setdefault("PIL", MagicMock())
sys.modules.setdefault("PIL.Image", MagicMock())

os.environ["BILIBILI_RELAY_TOKENS"] = "test-token"

_temp = tempfile.TemporaryDirectory()
_root = Path(_temp.name)
TEST_CFG = {
    "paths": {
        "state_dir": str(_root / "state"),
        "log_dir": str(_root / "logs"),
        "catalog_path": str(_root / "state" / "catalog.json"),
        "killswitch_path": str(_root / "state" / "killswitch.flag"),
        "publish_path": str(_root / "www" / "index.html"),
        "image_dir": str(_root / "www" / "images"),
    },
    "network": {"listen_host": "127.0.0.1", "listen_port": 8091},
    "limits": {
        "max_concurrent_streams": 2, "auth_window_seconds": 300, "auth_max_failures": 20,
        "max_duration_seconds": 14400, "image_max_bytes": 1000, "image_max_width": 100,
        "chunk_size": 65536, "plan_cache_seconds": 600,
    },
    "bilibili": {
        "allowed_hosts": ["bilibili.com", "www.bilibili.com", "m.bilibili.com"],
        "short_link_hosts": ["b23.tv"],
        "allowed_thumbnail_host_suffix": ".hdslb.com",
        "user_agent": "UA", "referer": "https://www.bilibili.com",
    },
}

import config as config_module
with patch.object(config_module, "load_config", return_value=TEST_CFG):
    import main

import catalog
import httpx

PLAN = {
    "bvid": "BV1X57q6uESc", "page": 1, "cid": 42,
    "video_url": "https://cn-x.bilivideo.com/a.mp4", "backup_urls": [],
    "headers": {"User-Agent": "UA", "Referer": "https://www.bilibili.com"},
    "title": "标题", "thumbnail": "", "duration": 784,
}
GOOD_QUERY = "token=test-token&link=https://www.bilibili.com/video/BV1X57q6uESc/?spm_id_from=333.1007"


def fake_request(query=GOOD_QUERY, client_host="1.2.3.4", headers=None):
    return SimpleNamespace(
        scope={"query_string": query.encode("utf-8")},
        headers=headers or {},
        client=SimpleNamespace(host=client_host),
    )


class MainFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.plan_cache.clear()
        main.resolve_tasks.clear()
        main.short_link_cache.clear()
        main._active_streams = 0
        main.guard._failures.clear()
        catalog._sessions.clear()
        Path(TEST_CFG["paths"]["killswitch_path"]).unlink(missing_ok=True)
        Path(TEST_CFG["paths"]["catalog_path"]).unlink(missing_ok=True)

    def _catalog_entry(self):
        return json.loads(Path(TEST_CFG["paths"]["catalog_path"]).read_text())["BV1X57q6uESc"]

    async def test_killswitch_returns_503(self):
        Path(TEST_CFG["paths"]["killswitch_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(TEST_CFG["paths"]["killswitch_path"]).write_text("{}")
        response = await main.bilibili_relay(fake_request())
        self.assertEqual(response.status_code, 503)

    async def test_bad_token_403(self):
        response = await main.bilibili_relay(fake_request(query="token=wrong&link=https://www.bilibili.com/video/BV1X57q6uESc"))
        self.assertEqual(response.status_code, 403)

    async def test_missing_and_bad_link_400(self):
        self.assertEqual((await main.bilibili_relay(fake_request(query="token=test-token"))).status_code, 400)
        self.assertEqual((await main.bilibili_relay(
            fake_request(query="token=test-token&link=https://evil.com/video/BV1X57q6uESc"))).status_code, 400)

    async def test_resolve_error_502_and_bad_page_400(self):
        with patch.object(main.resolver, "resolve", AsyncMock(side_effect=main.resolver.ResolveError("x"))):
            self.assertEqual((await main.bilibili_relay(fake_request())).status_code, 502)
        with patch.object(main.resolver, "resolve", AsyncMock(side_effect=main.resolver.BadPageError("x"))):
            self.assertEqual((await main.bilibili_relay(fake_request())).status_code, 400)

    async def test_duration_cap_413(self):
        long_plan = dict(PLAN, duration=99999)
        with patch.object(main.resolver, "resolve", AsyncMock(return_value=long_plan)):
            self.assertEqual((await main.bilibili_relay(fake_request())).status_code, 413)

    async def test_pull_recorded_once_across_range_probes(self):
        sentinel = object()
        with patch.object(main.resolver, "resolve", AsyncMock(return_value=dict(PLAN))) as resolve, \
                patch.object(main, "_direct_passthrough_response", AsyncMock(return_value=sentinel)):
            for start in (0, 1024, 4096):
                response = await main.bilibili_relay(
                    fake_request(headers={"range": f"bytes={start}-"}))
                self.assertIs(response, sentinel)
                main._active_streams = 0
        self.assertEqual(resolve.await_count, 1)
        entry = self._catalog_entry()
        self.assertEqual(entry["pull_count"], 1)
        self.assertEqual(entry["play_count"], 1)

        with patch.object(main, "_direct_passthrough_response", AsyncMock(return_value=sentinel)):
            await main.bilibili_relay(fake_request(client_host="5.6.7.8"))
            main._active_streams = 0
        entry = self._catalog_entry()
        self.assertEqual(entry["pull_count"], 1)
        self.assertEqual(entry["play_count"], 2)

    async def test_upstream_failure_re_resolves_once(self):
        sentinel = object()
        passthrough = AsyncMock(side_effect=[httpx.ConnectError("expired"), sentinel])
        with patch.object(main.resolver, "resolve", AsyncMock(return_value=dict(PLAN))) as resolve, \
                patch.object(main, "_direct_passthrough_response", passthrough):
            response = await main.bilibili_relay(fake_request())
        self.assertIs(response, sentinel)
        self.assertEqual(resolve.await_count, 2)
        self.assertEqual(self._catalog_entry()["pull_count"], 1)

    async def test_upstream_failure_twice_returns_502_and_releases_slot(self):
        passthrough = AsyncMock(side_effect=httpx.ConnectError("down"))
        with patch.object(main.resolver, "resolve", AsyncMock(return_value=dict(PLAN))), \
                patch.object(main, "_direct_passthrough_response", passthrough):
            response = await main.bilibili_relay(fake_request())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(main._active_streams, 0)

    async def test_short_link_expanded_and_cached(self):
        sentinel = object()
        expand = AsyncMock(return_value="https://www.bilibili.com/video/BV1X57q6uESc?p=1")
        with patch.object(main.resolver, "expand_short_link", expand), \
                patch.object(main.resolver, "resolve", AsyncMock(return_value=dict(PLAN))), \
                patch.object(main, "_direct_passthrough_response", AsyncMock(return_value=sentinel)):
            for _ in range(2):
                response = await main.bilibili_relay(
                    fake_request(query="token=test-token&link=https://b23.tv/xYzAbC?share_source=copy"))
                self.assertIs(response, sentinel)
                main._active_streams = 0
        self.assertEqual(expand.await_count, 1)
        self.assertEqual(self._catalog_entry()["pull_count"], 1)

    async def test_short_link_to_unexpected_target_400(self):
        with patch.object(main.resolver, "expand_short_link", AsyncMock(return_value="")):
            response = await main.bilibili_relay(
                fake_request(query="token=test-token&link=https://b23.tv/xYzAbC"))
        self.assertEqual(response.status_code, 400)

    async def test_short_link_expand_failure_502(self):
        with patch.object(main.resolver, "expand_short_link",
                          AsyncMock(side_effect=main.resolver.ResolveError("down"))):
            response = await main.bilibili_relay(
                fake_request(query="token=test-token&link=https://b23.tv/xYzAbC"))
        self.assertEqual(response.status_code, 502)

    async def test_concurrency_cap_503(self):
        main._active_streams = TEST_CFG["limits"]["max_concurrent_streams"]
        with patch.object(main.resolver, "resolve", AsyncMock(return_value=dict(PLAN))):
            response = await main.bilibili_relay(fake_request())
        self.assertEqual(response.status_code, 503)

    async def test_concurrent_cold_requests_share_one_resolve(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def resolve(*_args):
            started.set()
            await release.wait()
            return dict(PLAN)

        with patch.object(main.resolver, "resolve", AsyncMock(side_effect=resolve)) as mocked:
            requests = [
                asyncio.create_task(main._resolve_and_cache(
                    PLAN["bvid"], 1, f"{PLAN['bvid']}:1"))
                for _ in range(5)
            ]
            await started.wait()
            release.set()
            entries = await asyncio.gather(*requests)

        self.assertEqual(mocked.await_count, 1)
        self.assertTrue(all(entry is entries[0] for entry in entries))

    async def test_cancelled_waiter_does_not_cancel_shared_resolve(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def resolve(*_args):
            started.set()
            await release.wait()
            return dict(PLAN)

        with patch.object(main.resolver, "resolve", AsyncMock(side_effect=resolve)) as mocked:
            first = asyncio.create_task(main._resolve_and_cache(
                PLAN["bvid"], 1, f"{PLAN['bvid']}:1"))
            await started.wait()
            second = asyncio.create_task(main._resolve_and_cache(
                PLAN["bvid"], 1, f"{PLAN['bvid']}:1"))
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            release.set()
            entry = await first

        self.assertEqual(entry["plan"]["bvid"], PLAN["bvid"])
        self.assertEqual(mocked.await_count, 1)

    async def test_failed_shared_resolve_is_cleaned_up(self):
        with patch.object(
                main.resolver, "resolve",
                AsyncMock(side_effect=[
                    main.resolver.ResolveError("down"), dict(PLAN),
                ])) as mocked:
            first = await main._resolve_and_cache(
                PLAN["bvid"], 1, f"{PLAN['bvid']}:1")
            await asyncio.sleep(0)
            second = await main._resolve_and_cache(
                PLAN["bvid"], 1, f"{PLAN['bvid']}:1")

        self.assertEqual(first.status_code, 502)
        self.assertEqual(second["plan"]["bvid"], PLAN["bvid"])
        self.assertEqual(mocked.await_count, 2)


if __name__ == "__main__":
    unittest.main()
