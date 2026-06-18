from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from summarize_metrics import build_report_from_results_json


class SummarizeMetricsTest(unittest.TestCase):
    def test_results_json_report_includes_ac_sr_and_settling_metrics(self):
        data = {
            "converged": True,
            "metrics": {
                "gain_db": 62.5,
                "gbw_hz": 1.2e8,
                "bandwidth_hz": 1.2e8,
                "unity_gain_freq_hz": 1.2e8,
                "phase_margin_deg": 68.0,
                "power_w": 8.5e-4,
                "slew_rate_v_per_s": 1.1e8,
                "slew_rate_positive_v_per_s": 1.3e8,
                "slew_rate_negative_v_per_s": 1.1e8,
                "settling_time_s": 18e-9,
            },
            "params": {"I_tail": 20e-6},
            "target_status": {"gain_db": True},
            "gap": {"gain_db": 2.5},
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.json"
            path.write_text(json.dumps(data), encoding="utf-8")

            report = build_report_from_results_json(data, source=path)

        self.assertIn("Gain", report)
        self.assertIn("GBW", report)
        self.assertIn("Phase Margin", report)
        self.assertIn("Power", report)
        self.assertIn("Slew Rate", report)
        self.assertIn("SR+", report)
        self.assertIn("SR-", report)
        self.assertIn("Settling Time 0.1%", report)
        self.assertIn("Parameters:", report)
        self.assertIn("Target Status:", report)


if __name__ == "__main__":
    unittest.main()
