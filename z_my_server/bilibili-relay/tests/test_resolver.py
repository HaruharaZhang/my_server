import logging
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))

import resolver

CFG = {"bilibili": {
    "user_agent": "UA", "referer": "https://www.bilibili.com",
    "allowed_hosts": ["bilibili.com", "www.bilibili.com", "m.bilibili.com"],
    "short_link_hosts": ["b23.tv"],
}}
LOG = logging.getLogger("test")


def _response(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def _client_returning(payloads):
    client = AsyncMock()
    client.get.side_effect = [_response(p) for p in payloads]
    context = AsyncMock()
    context.__aenter__.return_value = client
    return client, context


VIEW_OK = {"code": 0, "data": {
    "cid": 42, "title": "标题", "pic": "http://i1.hdslb.com/bfs/archive/a.jpg",
    "duration": 100, "videos": 2,
    "pages": [{"cid": 42, "duration": 100}, {"cid": 43, "duration": 50}],
}}
PLAYURL_OK = {"code": 0, "data": {"durl": [{
    "url": "https://cn-x.bilivideo.com/a.mp4", "backup_url": ["https://cn-y.bilivideo.com/a.mp4"],
}]}}


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_normalizes_thumbnail_and_uses_direct_client(self):
        client, context = _client_returning([VIEW_OK, PLAYURL_OK])
        with patch.object(resolver.httpx, "AsyncClient", return_value=context) as ctor:
            plan = await resolver.resolve("BV1X57q6uESc", 1, CFG, LOG)
        self.assertEqual(plan["video_url"], "https://cn-x.bilivideo.com/a.mp4")
        self.assertEqual(plan["backup_urls"], ["https://cn-y.bilivideo.com/a.mp4"])
        self.assertEqual(plan["cid"], 42)
        self.assertEqual(plan["duration"], 100)
        self.assertEqual(plan["thumbnail"], "https://i1.hdslb.com/bfs/archive/a.jpg")
        self.assertEqual(plan["headers"]["Referer"], "https://www.bilibili.com")
        self.assertFalse(ctor.call_args.kwargs["trust_env"])
        self.assertNotIn("proxy", ctor.call_args.kwargs)
        playurl_params = client.get.call_args_list[1].kwargs["params"]
        self.assertEqual((playurl_params["qn"], playurl_params["fnval"], playurl_params["platform"]),
                         (64, 1, "html5"))

    async def test_multi_part_selects_page_cid(self):
        _, context = _client_returning([VIEW_OK, PLAYURL_OK])
        with patch.object(resolver.httpx, "AsyncClient", return_value=context):
            plan = await resolver.resolve("BV1X57q6uESc", 2, CFG, LOG)
        self.assertEqual(plan["cid"], 43)
        self.assertEqual(plan["duration"], 50)

    async def test_page_out_of_range(self):
        _, context = _client_returning([VIEW_OK])
        with patch.object(resolver.httpx, "AsyncClient", return_value=context):
            with self.assertRaises(resolver.BadPageError):
                await resolver.resolve("BV1X57q6uESc", 3, CFG, LOG)

    async def test_view_error_code(self):
        _, context = _client_returning([{"code": -404, "message": "啥都木有"}])
        with patch.object(resolver.httpx, "AsyncClient", return_value=context):
            with self.assertRaises(resolver.ResolveError):
                await resolver.resolve("BV1X57q6uESc", 1, CFG, LOG)

    async def test_playurl_error_and_empty_durl(self):
        for playurl in ({"code": -10403}, {"code": 0, "data": {"durl": []}}):
            _, context = _client_returning([VIEW_OK, playurl])
            with patch.object(resolver.httpx, "AsyncClient", return_value=context):
                with self.assertRaises(resolver.ResolveError):
                    await resolver.resolve("BV1X57q6uESc", 1, CFG, LOG)

    async def test_network_error_wrapped(self):
        contexts = []
        for _ in range(6):
            client = AsyncMock()
            client.get.side_effect = resolver.httpx.ConnectError("boom")
            context = AsyncMock()
            context.__aenter__.return_value = client
            contexts.append(context)
        with patch.object(resolver.httpx, "AsyncClient", side_effect=contexts), \
                patch.object(resolver.asyncio, "sleep", AsyncMock()) as sleep:
            with self.assertRaises(resolver.ResolveError):
                await resolver.resolve("BV1X57q6uESc", 1, CFG, LOG)
        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list],
            list(resolver.RETRY_DELAYS),
        )

    async def test_retry_then_success_uses_fresh_client(self):
        contexts = []
        for error in (
                resolver.httpx.ConnectTimeout("one"),
                resolver.httpx.ReadTimeout("two")):
            client = AsyncMock()
            client.get.side_effect = error
            context = AsyncMock()
            context.__aenter__.return_value = client
            contexts.append(context)
        _, success = _client_returning([VIEW_OK, PLAYURL_OK])
        contexts.append(success)

        with patch.object(resolver.httpx, "AsyncClient", side_effect=contexts) as ctor, \
                patch.object(resolver.asyncio, "sleep", AsyncMock()) as sleep:
            plan = await resolver.resolve("BV1X57q6uESc", 1, CFG, LOG)

        self.assertEqual(plan["cid"], 42)
        self.assertEqual(ctor.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list], [0.2, 0.4])

    async def test_retryable_http_status_and_nonretryable_status(self):
        retry_request = resolver.httpx.Request("GET", resolver.VIEW_API)
        retry_response = resolver.httpx.Response(429, request=retry_request)
        retry_error = resolver.httpx.HTTPStatusError(
            "limited", request=retry_request, response=retry_response)
        retry_client = AsyncMock()
        retry_client.get.side_effect = retry_error
        retry_context = AsyncMock()
        retry_context.__aenter__.return_value = retry_client
        _, success = _client_returning([VIEW_OK, PLAYURL_OK])

        with patch.object(
                resolver.httpx, "AsyncClient",
                side_effect=[retry_context, success]), \
                patch.object(resolver.asyncio, "sleep", AsyncMock()) as sleep:
            plan = await resolver.resolve("BV1X57q6uESc", 1, CFG, LOG)
        self.assertEqual(plan["cid"], 42)
        sleep.assert_awaited_once_with(0.2)

        bad_request = resolver.httpx.Request("GET", resolver.VIEW_API)
        bad_response = resolver.httpx.Response(404, request=bad_request)
        bad_error = resolver.httpx.HTTPStatusError(
            "missing", request=bad_request, response=bad_response)
        bad_client = AsyncMock()
        bad_client.get.side_effect = bad_error
        bad_context = AsyncMock()
        bad_context.__aenter__.return_value = bad_client
        with patch.object(resolver.httpx, "AsyncClient", return_value=bad_context), \
                patch.object(resolver.asyncio, "sleep", AsyncMock()) as sleep:
            with self.assertRaises(resolver.ResolveError):
                await resolver.resolve("BV1X57q6uESc", 1, CFG, LOG)
        sleep.assert_not_awaited()


class DirectClientTests(unittest.TestCase):
    def test_direct_client_configuration(self):
        with patch.object(resolver.httpx, "AsyncClient") as ctor, \
                patch.object(resolver.httpx, "AsyncHTTPTransport") as transport:
            resolver.direct_client(15.0)
        kwargs = ctor.call_args.kwargs
        self.assertFalse(kwargs["trust_env"])
        self.assertTrue(kwargs["follow_redirects"])
        self.assertEqual(kwargs["timeout"].connect, 5.0)
        transport.assert_called_once_with(retries=2)

    def test_api_client_disables_hidden_connection_retries(self):
        with patch.object(resolver.httpx, "AsyncClient"), \
                patch.object(resolver.httpx, "AsyncHTTPTransport") as transport:
            resolver.direct_client(15.0, connect_retries=0)
        transport.assert_called_once_with(retries=0)


def _redirect_client(locations):
    """依次返回带（或不带）Location 头的响应。"""
    client = AsyncMock()
    responses = []
    for location in locations:
        resp = MagicMock()
        resp.headers = {"location": location} if location else {}
        responses.append(resp)
    client.get.side_effect = responses
    context = AsyncMock()
    context.__aenter__.return_value = client
    return client, context


class ExpandShortLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_hop_to_bilibili(self):
        client, context = _redirect_client(["https://www.bilibili.com/video/BV1GJ411x7h7?p=2"])
        with patch.object(resolver.httpx, "AsyncClient", return_value=context) as ctor:
            expanded = await resolver.expand_short_link("https://b23.tv/xYzAbC", CFG, LOG)
        self.assertEqual(expanded, "https://www.bilibili.com/video/BV1GJ411x7h7?p=2")
        self.assertFalse(ctor.call_args.kwargs["trust_env"])
        self.assertFalse(ctor.call_args.kwargs["follow_redirects"])
        self.assertEqual(client.get.await_count, 1)

    async def test_redirect_to_unexpected_host_returns_empty(self):
        _, context = _redirect_client(["https://evil.com/video/BV1GJ411x7h7"])
        with patch.object(resolver.httpx, "AsyncClient", return_value=context):
            self.assertEqual(await resolver.expand_short_link("https://b23.tv/xYzAbC", CFG, LOG), "")

    async def test_no_location_returns_empty(self):
        _, context = _redirect_client([None])
        with patch.object(resolver.httpx, "AsyncClient", return_value=context):
            self.assertEqual(await resolver.expand_short_link("https://b23.tv/xYzAbC", CFG, LOG), "")

    async def test_hop_limit(self):
        _, context = _redirect_client(["https://b23.tv/a", "https://b23.tv/b", "https://b23.tv/c"])
        with patch.object(resolver.httpx, "AsyncClient", return_value=context):
            self.assertEqual(await resolver.expand_short_link("https://b23.tv/xYzAbC", CFG, LOG), "")

    async def test_network_error_wrapped(self):
        client = AsyncMock()
        client.get.side_effect = resolver.httpx.ConnectError("boom")
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch.object(resolver.httpx, "AsyncClient", return_value=context):
            with self.assertRaises(resolver.ResolveError):
                await resolver.expand_short_link("https://b23.tv/xYzAbC", CFG, LOG)


if __name__ == "__main__":
    unittest.main()
