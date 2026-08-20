import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))

import relay


class RelayPullCountTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_pull_for_multiple_tracks_retries_and_subscribers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = {
                "paths": {"cache_dir": tmpdir, "state_dir": tmpdir},
                "network": {"proxy": None},
                "limits": {"chunk_size": 64, "max_cached_videos": 1},
            }
            broadcaster = AsyncMock()
            registry = AsyncMock()
            admission = AsyncMock()
            on_success = AsyncMock()
            on_pull_started = AsyncMock()

            async def fake_ffmpeg(plan, proxy, part_path, broadcaster, chunk_size, log,
                                  on_first_chunk, on_proxy_bytes):
                # Multiple output chunks stand in for two tracks plus resumed/retried reads.
                await asyncio.gather(on_first_chunk(), on_first_chunk())
                await on_first_chunk()
                on_proxy_bytes(123)
                Path(part_path).write_bytes(b"media")

            async def fake_faststart(part_path, final_path, log):
                Path(part_path).replace(final_path)

            with patch.object(relay, "_relay_ffmpeg", fake_ffmpeg), \
                    patch.object(relay, "_faststart_remux", fake_faststart), \
                    patch.object(relay.runtime_status, "write") as status_write:
                await relay.run_relay(
                    "video", broadcaster, {"mode": "mux_copy"}, cfg, registry,
                    admission, on_success, on_pull_started, logging.getLogger("test"),
                )

            on_pull_started.assert_awaited_once()
            broadcaster.finish.assert_awaited_once_with()
            admission.release.assert_awaited_once()
            self.assertEqual(status_write.call_args_list[-1].args[3], 123)
            self.assertEqual(relay.runtime_status.proxy_total_bytes(cfg), 123)


if __name__ == "__main__":
    unittest.main()
