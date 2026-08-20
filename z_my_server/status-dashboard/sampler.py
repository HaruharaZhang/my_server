#!/usr/bin/env python3
"""Sample default-interface traffic every five seconds and publish a 300s peak."""

import argparse
import json
import os
import time
from collections import deque
from pathlib import Path


def default_interface(route_path="/proc/net/route"):
    for line in Path(route_path).read_text(encoding="utf-8").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & 2:
            return fields[0]
    return None


def interface_bytes(interface, dev_path="/proc/net/dev"):
    for line in Path(dev_path).read_text(encoding="utf-8").splitlines()[2:]:
        name, values = line.split(":", 1)
        if name.strip() == interface:
            fields = values.split()
            return int(fields[0]), int(fields[8])
    raise ValueError("default interface missing from counters")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, separators=(",", ":"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


class PeakSampler:
    def __init__(self, window_seconds=300, max_interval=15):
        self.window_seconds = window_seconds
        self.max_interval = max_interval
        self.baseline = None
        self.samples = deque()

    def observe(self, interface, rx, tx, monotonic_now):
        old = self.baseline
        self.baseline = (interface, rx, tx, monotonic_now)
        if old is not None:
            old_interface, old_rx, old_tx, old_at = old
            elapsed = monotonic_now - old_at
            if (interface == old_interface and 0 < elapsed <= self.max_interval
                    and rx >= old_rx and tx >= old_tx):
                self.samples.append((monotonic_now, round((rx - old_rx) / elapsed),
                                     round((tx - old_tx) / elapsed)))
        cutoff = monotonic_now - self.window_seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        return {
            "sampled_epoch": time.time(),
            "sample_count": len(self.samples),
            "rx_peak_bps": max((row[1] for row in self.samples), default=None),
            "tx_peak_bps": max((row[2] for row in self.samples), default=None),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    sampler = PeakSampler()
    deadline = time.monotonic()
    while True:
        deadline += args.interval
        try:
            interface = default_interface()
            if not interface:
                raise ValueError("default route not found")
            rx, tx = interface_bytes(interface)
            atomic_json(args.output, sampler.observe(interface, rx, tx, time.monotonic()))
        except (OSError, ValueError):
            sampler.baseline = None
        time.sleep(max(0, deadline - time.monotonic()))


if __name__ == "__main__":
    main()
