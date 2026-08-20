#!/usr/bin/env python3
"""Small real-media probe for the deployed YouTube resolver."""

import httpx

import ytdlp_client
from config import load_config


TEST_VIDEO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
MINIMUM_BYTES = 65536


class SmokeError(Exception):
    pass


def probe_media(stage, url, headers, proxy):
    request_headers = dict(headers or {})
    request_headers["Range"] = f"bytes=0-{MINIMUM_BYTES - 1}"
    try:
        with httpx.Client(proxy=proxy, timeout=60, follow_redirects=True) as client:
            with client.stream("GET", url, headers=request_headers) as response:
                if response.status_code not in (200, 206):
                    raise SmokeError(f"{stage}:http_{response.status_code}")
                received = 0
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received >= MINIMUM_BYTES:
                        return
    except SmokeError:
        raise
    except Exception as exc:
        raise SmokeError(f"{stage}:{type(exc).__name__}") from None
    raise SmokeError(f"{stage}:short_response")


def run():
    cfg = load_config()
    youtube = cfg["youtube"]
    proxy = cfg["network"]["proxy"]
    ytdlp_client.validate_environment(youtube)

    for quality in ("compat", "high"):
        plan = ytdlp_client.resolve(
            TEST_VIDEO_URL,
            proxy,
            youtube[f"format_{quality}"],
            youtube,
        )
        probe_media(f"{quality}_video", plan["video_url"], plan.get("headers"), proxy)
        if quality == "high" and plan.get("audio_url"):
            probe_media("high_audio", plan["audio_url"], plan.get("headers"), proxy)


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, SmokeError) else type(exc).__name__
        raise SystemExit(f"youtube smoke test failed: {detail}") from None
    print("youtube smoke test passed")
