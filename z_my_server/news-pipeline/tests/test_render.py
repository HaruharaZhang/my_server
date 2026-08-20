import json
import logging
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import render


class RecentPayloadsTests(unittest.TestCase):
    def test_loads_latest_thirty_days(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            end_day = date(2026, 7, 1)
            for offset in range(35):
                day = end_day - timedelta(days=offset)
                (out_dir / f"items-{day.isoformat()}.json").write_text(
                    json.dumps({"items": [{"title": day.isoformat()}]}),
                    encoding="utf-8",
                )

            payloads = render.load_recent_payloads(
                {"paths": {"out_dir": str(out_dir)}},
                None,
                end_day.isoformat(),
                logging.getLogger(__name__),
            )

        self.assertEqual(len(payloads), 30)
        self.assertEqual(payloads[0][0], "2026-07-01")
        self.assertEqual(payloads[-1][0], "2026-06-02")


class FrontendLoadingTests(unittest.TestCase):
    def test_template_contains_loading_and_dynamic_navigation_hooks(self):
        template = (Path(render.__file__).parent / "template.html").read_text(
            encoding="utf-8"
        )

        self.assertEqual(render.PAGE_VERSION, "v3.4.5")
        self.assertIn('id="loading-status"', template)
        self.assertIn('aria-live="polite"', template)
        self.assertIn('aria-busy="true"', template)
        self.assertIn("function navigateToDate(date)", template)
        self.assertIn("function updateStickyMetrics()", template)
        self.assertIn("new ResizeObserver(updateStickyMetrics)", template)
        self.assertIn("activeDateLock", template)
        self.assertIn("加载失败，请刷新重试", template)


if __name__ == "__main__":
    unittest.main()
