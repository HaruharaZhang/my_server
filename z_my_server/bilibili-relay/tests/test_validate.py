import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))

from validate import canonical_video_url, extract_bvid, is_allowed_thumbnail, is_short_link, parse_query

HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}


class ParseQueryTests(unittest.TestCase):
    def test_link_keeps_raw_remainder_with_ampersands(self):
        token, link = parse_query(
            "token=abc&link=https://www.bilibili.com/video/BV1X57q6uESc/?spm_id_from=333.1007&vd_source=x")
        self.assertEqual(token, "abc")
        self.assertEqual(link, "https://www.bilibili.com/video/BV1X57q6uESc/?spm_id_from=333.1007&vd_source=x")

    def test_missing_parts(self):
        self.assertEqual(parse_query("link=https://www.bilibili.com/video/BV1X57q6uESc"),
                         ("", "https://www.bilibili.com/video/BV1X57q6uESc"))
        self.assertEqual(parse_query("token=abc"), ("abc", ""))
        self.assertEqual(parse_query(""), ("", ""))

    def test_url_encoded_link(self):
        _, link = parse_query("token=t&link=https%3A%2F%2Fwww.bilibili.com%2Fvideo%2FBV1X57q6uESc%2F")
        self.assertEqual(link, "https://www.bilibili.com/video/BV1X57q6uESc/")


class ExtractBvidTests(unittest.TestCase):
    def test_standard_link_with_junk(self):
        self.assertEqual(
            extract_bvid("https://www.bilibili.com/video/BV1X57q6uESc/?spm_id_from=333.1007.tianma.5-4-18.click", HOSTS),
            ("BV1X57q6uESc", 1))

    def test_mobile_host_and_page(self):
        self.assertEqual(
            extract_bvid("https://m.bilibili.com/video/BV1GJ411x7h7?p=3", HOSTS),
            ("BV1GJ411x7h7", 3))

    def test_invalid_page_falls_back_to_one(self):
        self.assertEqual(extract_bvid("https://www.bilibili.com/video/BV1GJ411x7h7?p=abc", HOSTS),
                         ("BV1GJ411x7h7", 1))
        self.assertEqual(extract_bvid("https://www.bilibili.com/video/BV1GJ411x7h7?p=0", HOSTS),
                         ("BV1GJ411x7h7", 1))

    def test_rejects_wrong_host_even_with_bvid(self):
        self.assertEqual(extract_bvid("https://evil.com/video/BV1X57q6uESc", HOSTS), ("", 1))
        self.assertEqual(extract_bvid("https://b23.tv/BV1X57q6uESc", HOSTS), ("", 1))

    def test_rejects_bad_scheme_and_no_bvid(self):
        self.assertEqual(extract_bvid("ftp://www.bilibili.com/video/BV1X57q6uESc", HOSTS), ("", 1))
        self.assertEqual(extract_bvid("https://www.bilibili.com/bangumi/play/ep123456", HOSTS), ("", 1))
        self.assertEqual(extract_bvid("", HOSTS), ("", 1))

    def test_canonical_url(self):
        self.assertEqual(canonical_video_url("BV1X57q6uESc"), "https://www.bilibili.com/video/BV1X57q6uESc")
        self.assertEqual(canonical_video_url("BV1X57q6uESc", 2), "https://www.bilibili.com/video/BV1X57q6uESc?p=2")


class ShortLinkTests(unittest.TestCase):
    SHORT_HOSTS = {"b23.tv"}

    def test_b23_links_detected(self):
        self.assertTrue(is_short_link("https://b23.tv/BV1GJ411x7h7", self.SHORT_HOSTS))
        self.assertTrue(is_short_link("https://b23.tv/xYzAbC?share_source=copy", self.SHORT_HOSTS))
        self.assertTrue(is_short_link("http://b23.tv:80/xYzAbC", self.SHORT_HOSTS))

    def test_non_short_links_rejected(self):
        self.assertFalse(is_short_link("https://www.bilibili.com/video/BV1GJ411x7h7", self.SHORT_HOSTS))
        self.assertFalse(is_short_link("https://b23.tv.evil.com/xYzAbC", self.SHORT_HOSTS))
        self.assertFalse(is_short_link("https://b23.tv:8080/xYzAbC", self.SHORT_HOSTS))
        self.assertFalse(is_short_link("ftp://b23.tv/xYzAbC", self.SHORT_HOSTS))
        self.assertFalse(is_short_link("", self.SHORT_HOSTS))


class ThumbnailTests(unittest.TestCase):
    def test_hdslb_hosts_allowed(self):
        for url in ("https://i0.hdslb.com/bfs/archive/a.jpg",
                    "https://i1.hdslb.com/bfs/archive/a.jpg",
                    "http://i2.hdslb.com/bfs/archive/a.jpg"):
            self.assertTrue(is_allowed_thumbnail(url, ".hdslb.com"))

    def test_lookalike_hosts_rejected(self):
        self.assertFalse(is_allowed_thumbnail("https://hdslb.com.evil.com/a.jpg", ".hdslb.com"))
        self.assertFalse(is_allowed_thumbnail("https://evilhdslb.com/a.jpg", ".hdslb.com"))
        self.assertFalse(is_allowed_thumbnail("https://i.ytimg.com/a.jpg", ".hdslb.com"))
        self.assertFalse(is_allowed_thumbnail("file:///etc/passwd", ".hdslb.com"))


if __name__ == "__main__":
    unittest.main()
