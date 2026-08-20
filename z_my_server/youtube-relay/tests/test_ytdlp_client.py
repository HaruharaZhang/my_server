import unittest
from unittest import mock

import ytdlp_client


YOUTUBE_CONFIG = {
    "player_client": "mweb",
    "node_runtime_path": "/opt/dyyjs-youtube/node24-runtime/bin/node",
    "socket_timeout_seconds": 60,
}


class YtdlpClientTests(unittest.TestCase):
    @mock.patch("ytdlp_client.yt_dlp.YoutubeDL")
    def test_resolve_enables_mweb_node_and_timeout(self, youtube_dl):
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.return_value = {
            "title": "test",
            "url": "https://media.example.invalid/video",
            "vcodec": "avc1.4d401f",
            "acodec": "mp4a.40.2",
        }

        plan = ytdlp_client.resolve(
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "http://127.0.0.1:7890",
            "best",
            YOUTUBE_CONFIG,
        )

        options = youtube_dl.call_args.args[0]
        self.assertEqual(options["extractor_args"], {"youtube": {"player_client": ["mweb"]}})
        self.assertEqual(
            options["js_runtimes"],
            {"node": {"path": "/opt/dyyjs-youtube/node24-runtime/bin/node"}},
        )
        self.assertEqual(options["socket_timeout"], 60)
        self.assertEqual(plan["mode"], "passthrough")

    @mock.patch("ytdlp_client.importlib.metadata.version", return_value="0.8.0")
    @mock.patch("ytdlp_client.os.access", return_value=True)
    @mock.patch("ytdlp_client.Path.is_file", return_value=True)
    def test_environment_requires_runtime_and_ejs(self, _is_file, _access, version):
        ytdlp_client.validate_environment(YOUTUBE_CONFIG)
        version.assert_called_once_with("yt-dlp-ejs")

    @mock.patch("ytdlp_client.Path.is_file", return_value=False)
    def test_environment_rejects_missing_runtime(self, _is_file):
        with self.assertRaisesRegex(RuntimeError, "Node runtime"):
            ytdlp_client.validate_environment(YOUTUBE_CONFIG)

    @mock.patch(
        "ytdlp_client.importlib.metadata.version",
        side_effect=ytdlp_client.importlib.metadata.PackageNotFoundError,
    )
    @mock.patch("ytdlp_client.os.access", return_value=True)
    @mock.patch("ytdlp_client.Path.is_file", return_value=True)
    def test_environment_rejects_missing_ejs(self, _is_file, _access, _version):
        with self.assertRaisesRegex(RuntimeError, "yt-dlp-ejs"):
            ytdlp_client.validate_environment(YOUTUBE_CONFIG)


if __name__ == "__main__":
    unittest.main()
