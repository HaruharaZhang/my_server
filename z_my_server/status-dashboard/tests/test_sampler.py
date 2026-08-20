import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))

from sampler import PeakSampler, default_interface, interface_bytes
from collector import sampler_info


class SamplerTests(unittest.TestCase):
    def test_default_route_and_interface_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            route = Path(directory) / "route"
            dev = Path(directory) / "dev"
            route.write_text("Iface Destination Gateway Flags\neth0 00000000 01020304 0003\n")
            dev.write_text("header\nheader\n eth0: 100 0 0 0 0 0 0 0 250 0 0 0 0 0 0 0\n")
            self.assertEqual(default_interface(route), "eth0")
            self.assertEqual(interface_bytes("eth0", dev), (100, 250))

    def test_peaks_reset_and_window(self):
        sampler = PeakSampler(window_seconds=300)
        self.assertEqual(sampler.observe("eth0", 100, 100, 0)["sample_count"], 0)
        first = sampler.observe("eth0", 600, 1100, 5)
        self.assertEqual((first["rx_peak_bps"], first["tx_peak_bps"]), (100, 200))
        second = sampler.observe("eth0", 1600, 1350, 10)
        self.assertEqual((second["rx_peak_bps"], second["tx_peak_bps"]), (200, 200))
        self.assertEqual(sampler.observe("eth0", 10, 10, 15)["sample_count"], 2)
        self.assertEqual(sampler.observe("eth1", 20, 20, 20)["sample_count"], 2)
        expired = sampler.observe("eth1", 30, 30, 321)
        self.assertEqual(expired["sample_count"], 0)

    def test_collector_rejects_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps({"sampled_epoch": 1, "sample_count": 60,
                                        "rx_peak_bps": 5, "tx_peak_bps": 6}))
            self.assertEqual(sampler_info(path)["status"], "warning")


if __name__ == "__main__":
    unittest.main()
