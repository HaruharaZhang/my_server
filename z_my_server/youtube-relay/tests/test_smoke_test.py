import unittest
from unittest import mock

import smoke_test


class FakeResponse:
    status_code = 206

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_bytes(self):
        yield b"x" * smoke_test.MINIMUM_BYTES


class FakeClient:
    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stream(self, *_args, **_kwargs):
        return FakeResponse()


class ForbiddenClient(FakeClient):
    def stream(self, *_args, **_kwargs):
        response = FakeResponse()
        response.status_code = 403
        return response


class SmokeTestTests(unittest.TestCase):
    @mock.patch("smoke_test.httpx.Client", FakeClient)
    def test_probe_accepts_partial_media(self):
        smoke_test.probe_media(
            "compat_video",
            "https://media.example.invalid/signed?opaque=test-value",
            {},
            "http://127.0.0.1:7890",
        )

    @mock.patch("smoke_test.httpx.Client", side_effect=RuntimeError("sensitive-detail"))
    def test_probe_error_does_not_include_exception_text(self, _client):
        with self.assertRaisesRegex(smoke_test.SmokeError, "compat_video:RuntimeError") as caught:
            smoke_test.probe_media(
                "compat_video",
                "https://media.example.invalid/signed?opaque=test-value",
                {},
                "http://127.0.0.1:7890",
            )
        self.assertNotIn("sensitive-detail", str(caught.exception))

    @mock.patch("smoke_test.httpx.Client", ForbiddenClient)
    def test_probe_rejects_upstream_forbidden(self):
        with self.assertRaisesRegex(smoke_test.SmokeError, "compat_video:http_403"):
            smoke_test.probe_media(
                "compat_video",
                "https://media.example.invalid/signed?opaque=test-value",
                {},
                "http://127.0.0.1:7890",
            )


if __name__ == "__main__":
    unittest.main()
