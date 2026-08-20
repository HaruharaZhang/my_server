import json
import asyncio
import sys
import urllib.error
from datetime import date
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).parents[1]))

import cve


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def client_config():
    return {
        "proxy_url": "http://127.0.0.1:7890", "max_retries": 3,
        "connect_timeout": 1, "read_timeout": 1, "backoff_seconds": [1, 2, 4],
    }


class ProxyClientTests(TestCase):
    def test_proxy_is_explicit_and_environment_is_ignored(self):
        with mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://bad:9999"}):
            client = cve.ProxyClient(client_config(), mock.Mock())
        proxies = [handler.proxies for handler in client.opener.handlers if hasattr(handler, "proxies")]
        self.assertIn({"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}, proxies)

    def test_first_success_does_not_retry(self):
        client = cve.ProxyClient(client_config(), mock.Mock(), sleep=mock.Mock())
        client.opener.open = mock.Mock(return_value=Response({"ok": True}))
        self.assertEqual(client.json("NVD", "https://example.test"), {"ok": True})
        self.assertEqual(client.opener.open.call_count, 1)

    def test_retryable_error_attempts_four_times(self):
        client = cve.ProxyClient(client_config(), mock.Mock(), sleep=mock.Mock(), jitter=lambda *_: 0)
        error = urllib.error.HTTPError("https://example.test", 503, "bad", {}, None)
        client.opener.open = mock.Mock(side_effect=error)
        with self.assertRaises(urllib.error.HTTPError):
            client.json("NVD", "https://example.test")
        self.assertEqual(client.opener.open.call_count, 4)
        self.assertEqual([x.args[0] for x in client.sleep.call_args_list], [1, 2, 4])

    def test_permanent_400_does_not_retry(self):
        client = cve.ProxyClient(client_config(), mock.Mock(), sleep=mock.Mock())
        error = urllib.error.HTTPError("https://example.test", 400, "bad", {}, None)
        client.opener.open = mock.Mock(side_effect=error)
        with self.assertRaises(urllib.error.HTTPError):
            client.json("NVD", "https://example.test")
        self.assertEqual(client.opener.open.call_count, 1)

    def test_parse_failure_retries(self):
        class BadResponse(Response):
            def read(self): return b"{truncated"
        client = cve.ProxyClient(client_config(), mock.Mock(), sleep=mock.Mock())
        client.opener.open = mock.Mock(return_value=BadResponse({}))
        with self.assertRaises(cve.ParseError):
            client.json("NVD", "https://example.test")
        self.assertEqual(client.opener.open.call_count, 4)


class CveRulesTests(TestCase):
    def setUp(self):
        self.cfg = {"cvss_min": 7, "epss_min_percent": 10, "epss_percentile_min": 90,
                    "service_aliases": {"LiteLLM": ["litellm", "berriai/litellm"]}}

    def record(self, **values):
        record = cve.blank_record(values.pop("id", "CVE-2026-1234"), "test")
        record.update(values)
        return record

    def test_risk_boundaries(self):
        self.assertFalse(cve.high_risk(self.record(cvss=6.9, epss=9.99, epss_percentile=89.99), self.cfg))
        self.assertTrue(cve.high_risk(self.record(cvss=7.0), self.cfg))
        self.assertTrue(cve.high_risk(self.record(epss=10.0), self.cfg))
        self.assertTrue(cve.high_risk(self.record(epss_percentile=90.0), self.cfg))
        self.assertTrue(cve.high_risk(self.record(severity="High"), self.cfg))
        self.assertTrue(cve.high_risk(self.record(kev=True), self.cfg))

    def test_merge_by_cve_and_reject_invalid_status(self):
        first = self.record(products=["litellm"], cvss=8.1)
        second = self.record(fixed_versions=["1.2.3"], epss=12)
        rejected = self.record(id="CVE-2026-9999", status="REJECTED")
        merged = cve.merge_records([first, second, rejected])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["fixed_versions"], ["1.2.3"])

    def test_litellm_cves_are_one_service_card(self):
        one = self.record(id="CVE-2026-1234", products=["litellm"], cvss=8, published="2026-07-01")
        two = self.record(id="CVE-2026-5678", purls=["pkg:pypi/litellm"], cvss=9, published="2026-07-01")
        cards = cve.aggregate_services([one, two], self.cfg)
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]["title"].startswith("LiteLLM:"))
        self.assertEqual(len(cards[0]["cves"]), 2)

    def test_same_service_is_split_across_published_days(self):
        records = [
            self.record(id="CVE-2026-1001", products=["litellm"], published="2026-07-01T23:00:00Z"),
            self.record(id="CVE-2026-1002", products=["litellm"], published="2026-07-03T01:00:00Z"),
        ]
        cards = cve.aggregate_services(records, self.cfg)
        self.assertEqual({card["date"] for card in cards}, {"2026-07-02", "2026-07-03"})

    def test_window_uses_published_date_only(self):
        old_but_modified_yesterday = self.record(
            published="2026-06-01T00:00:00Z",
            modified="2026-07-14T00:00:00Z",
        )
        newly_published = self.record(
            published="2026-07-14T00:00:00Z",
            modified="2026-07-15T00:00:00Z",
        )
        missing_published = self.record(modified="2026-07-14T00:00:00Z")

        self.assertFalse(cve.in_window(old_but_modified_yesterday, date(2026, 7, 14), date(2026, 7, 14)))
        self.assertTrue(cve.in_window(newly_published, date(2026, 7, 14), date(2026, 7, 14)))
        self.assertFalse(cve.in_window(missing_published, date(2026, 7, 14), date(2026, 7, 14)))

    def test_discovery_urls_filter_by_publication_date(self):
        source = {"url": "https://example.test/cves"}
        nvd_url = cve._nvd_url(source, date(2026, 7, 14), date(2026, 7, 14))
        github_url = cve._github_url(source, date(2026, 7, 14))

        self.assertIn("pubStartDate=", nvd_url)
        self.assertIn("pubEndDate=", nvd_url)
        self.assertNotIn("lastMod", nvd_url)
        self.assertIn("2026-07-13T16%3A00%3A00%2B00%3A00", github_url)
        self.assertIn("2026-07-14T16%3A00%3A00%2B00%3A00", github_url)
        self.assertIn("sort=published", github_url)

    def test_osv_events_become_ranges_not_introduced_versions(self):
        payload = {"vulns": [{"id": "GHSA-test", "aliases": ["CVE-2026-1234"], "affected": [{
            "package": {"ecosystem": "PyPI", "name": "demo"},
            "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "1.0"}, {"fixed": "1.4"}]}],
        }]}]}
        record = cve.parse_osv(payload, "CVE-2026-1234")[0]
        self.assertEqual(record["affected_versions"], [])
        self.assertEqual(record["version_ranges"][0]["introduced"], "1.0")
        self.assertEqual(record["version_ranges"][0]["fixed"], "1.4")

    def test_vendor_version_range_wins_over_osv(self):
        osv = self.record(version_ranges=[{"introduced": "1", "fixed": "2", "source": "OSV"}])
        vendor = self.record(version_ranges=[{"range": "1.5 only", "source": "Red Hat"}])
        merged = cve.merge_records([osv, vendor])[0]
        self.assertEqual(merged["version_ranges"], [{"range": "1.5 only", "source": "Red Hat"}])

    def test_kernel_and_distributions_are_separate(self):
        records = [
            self.record(id="CVE-2026-1001", products=["Linux Kernel"]),
            self.record(id="CVE-2026-1002", products=["Ubuntu Linux Kernel"]),
            self.record(id="CVE-2026-1003", products=["Debian Linux"]),
            self.record(id="CVE-2026-1004", products=["Red Hat Enterprise Linux"]),
            self.record(id="CVE-2026-1005", products=["SUSE Linux Enterprise"]),
        ]
        names = {x["service_name"] for x in cve.aggregate_services(records, self.cfg)}
        self.assertEqual(names, {"Linux Kernel", "Ubuntu", "Debian", "RHEL", "SUSE"})

    def test_vendor_parser_keeps_source_attribution(self):
        payload = {"package": "openssl", "items": [{"id": "CVE-2026-1234", "status": "fixed", "fixed_version": "3.0.1"}]}
        records = cve.parse_vendor_json(payload, "Vendor VEX")
        self.assertEqual(records[0]["id"], "CVE-2026-1234")
        self.assertEqual(records[0]["fixed_versions"], ["3.0.1"])
        self.assertEqual(records[0]["field_sources"]["fixed_versions"], ["Vendor VEX"])

    def test_ai_grouping_rejects_fabricated_cve_ids(self):
        class Router:
            async def chat(self, _messages):
                return ('[{"service_name":"LiteLLM","service_description":"模型网关",'
                        '"cves":[{"cve_id":"CVE-2026-1234","impact_summary":"攻击者可读取敏感配置"},'
                        '{"cve_id":"CVE-2026-9999","impact_summary":"伪造内容"}]}]', "test-model")
        record = self.record(products=["litellm"])
        payload = {"records": [record], "services": [], "status": {}}
        result = asyncio.run(cve.ai_aggregate_services(payload, self.cfg, Router(), mock.Mock()))
        self.assertEqual(result["status"]["ai_grouped_cves"], 1)
        self.assertEqual(result["services"][0]["service_name"], "LiteLLM")
        self.assertNotIn("CVE-", result["services"][0]["title"])
        self.assertIn("模型网关", result["services"][0]["service_description"])
        self.assertIn("首次发布时间待确认", result["services"][0]["service_description"])

    def test_ai_uses_small_batches_and_keeps_protected_service_name(self):
        class Router:
            def __init__(self):
                self.calls = 0

            async def chat(self, messages):
                self.calls += 1
                candidates = json.loads(messages[0]["content"].split("\n记录：", 1)[1])
                groups = [{
                    "service_name": "Wrong Name",
                    "service_description": "操作系统内核",
                    "cves": [{"cve_id": item["cve_id"], "impact_summary": "攻击者可提升本地权限"}],
                } for item in candidates]
                return json.dumps(groups, ensure_ascii=False), "test-model"

        router = Router()
        records = [
            self.record(id=f"CVE-2026-{1000 + index}", products=["Linux Kernel"])
            for index in range(3)
        ]
        payload = {"records": records, "services": [], "status": {}}
        cfg = dict(self.cfg, ai_batch_size=2)
        result = asyncio.run(cve.ai_aggregate_services(payload, cfg, router, mock.Mock()))
        self.assertEqual(router.calls, 2)
        self.assertEqual(result["status"]["ai_grouped_cves"], 3)
        self.assertEqual({record["service_override"] for record in records}, {"Linux Kernel"})
        self.assertTrue(all(record["impact_summary"] == "攻击者可提升本地权限" for record in records))

    def test_increment_merges_yesterday_and_trims_to_30_days(self):
        old = self.record(id="CVE-2026-1001", products=["old"], published="2026-06-15", cvss=8,
                          impact_summary="保留的摘要", summary_fingerprint="old")
        expired = self.record(
            id="CVE-2026-1002",
            published="2026-06-14",
            modified="2026-07-14",
            cvss=9,
        )
        changed = self.record(id="CVE-2026-1001", fixed_versions=["2.0"], modified="2026-07-14")
        new = self.record(id="CVE-2026-1003", published="2026-07-14", cvss=7)
        merged = cve.merge_increment({"records": [old, expired]}, {"records": [changed, new], "status": {}},
                                     date(2026, 7, 14), 30)
        by_id = {record["id"]: record for record in merged["records"]}
        self.assertEqual(set(by_id), {"CVE-2026-1001", "CVE-2026-1003"})
        self.assertEqual(by_id["CVE-2026-1001"]["fixed_versions"], ["2.0"])
        self.assertEqual(by_id["CVE-2026-1001"]["impact_summary"], "保留的摘要")

    def test_unchanged_summary_and_profile_skip_ai(self):
        record = self.record(products=["litellm"], impact_summary="已有摘要")
        record["summary_fingerprint"] = cve.facts_fingerprint(record)
        profile = {"service_key": "litellm", "description": "用途。解决问题。首次发布时间待确认。"}

        class Router:
            async def chat(self, _messages):
                raise AssertionError("不应调用模型")

        payload = {"records": [record], "services": [], "status": {}}
        result = asyncio.run(cve.ai_aggregate_services(payload, self.cfg, Router(), mock.Mock(), {"litellm": profile}))
        self.assertEqual(result["services"][0]["service_profile"], profile)
