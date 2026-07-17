from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from parameter_effects import analyze_history, analyze_optimization_history


class ParameterEffectsTests(unittest.TestCase):
    def test_infers_search_and_physical_parameter_directions(self):
        history = self._history(20)

        analysis = analyze_history(history, min_samples=15)

        gain = self._effect(analysis, "search", "Cc", "gain_db")
        gbw = self._effect(analysis, "search", "Cc", "bandwidth_hz")
        power = self._effect(analysis, "search", "Cc", "power_w")
        physical_gain = self._effect(
            analysis, "physical", "Wdiff", "gain_db"
        )
        self.assertEqual(gain["direction"], "positive")
        self.assertEqual(gain["helpful_direction"], "increase")
        self.assertAlmostEqual(gain["spearman_rho"], 1.0)
        self.assertEqual(gbw["direction"], "negative")
        self.assertEqual(gbw["helpful_direction"], "decrease")
        self.assertEqual(power["helpful_direction"], "decrease")
        self.assertEqual(physical_gain["direction"], "positive")
        self.assertTrue(any(
            item["parameter"] == "Cc" and item["action"] == "inspect_upper_bound"
            for item in analysis["recommendations"]
        ))

    def test_marks_small_histories_as_insufficient(self):
        analysis = analyze_history(self._history(5), min_samples=15)

        effect = self._effect(analysis, "search", "Cc", "gain_db")

        self.assertEqual(effect["status"], "insufficient_data")
        self.assertIsNone(effect["spearman_rho"])

    def test_flags_parameter_region_with_more_failures(self):
        history = self._history(20)
        for record in history["history"][-5:]:
            record["result"]["converged"] = False

        analysis = analyze_history(history, min_samples=15)

        self.assertTrue(any(
            item["parameter"] == "Cc"
            and item["action"] == "inspect_or_reduce_upper_range"
            for item in analysis["recommendations"]
        ))

    def test_writes_json_csv_and_markdown_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history_path = root / "optimization_log.json"
            history_path.write_text(
                json.dumps(self._history(20)), encoding="utf-8"
            )

            analyze_optimization_history(
                history_path, root / "parameter_analysis", min_samples=15
            )

            output = root / "parameter_analysis"
            self.assertTrue((output / "parameter_effects.json").exists())
            self.assertIn(
                "empirical association",
                (output / "parameter_effects.md").read_text(encoding="utf-8"),
            )
            with (output / "parameter_effects.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows)
            self.assertIn("helpful_direction", rows[0])

    @staticmethod
    def _effect(analysis, domain, parameter, metric):
        return next(
            effect
            for effect in analysis["effects"]
            if effect["domain"] == domain
            and effect["parameter"] == parameter
            and effect["metric"] == metric
        )

    @staticmethod
    def _history(count):
        records = []
        for index in range(1, count + 1):
            records.append({
                "iteration": index - 1,
                "params": {"Cc": float(index)},
                "physical_params": {"Wdiff": float(index * 2)},
                "reward": float(index),
                "result": {
                    "converged": True,
                    "gain_db": float(index),
                    "bandwidth_hz": float(1000 - index),
                    "phase_margin_deg": float(index),
                    "power_w": float(index),
                    "slew_rate_v_per_s": float(index),
                    "settling_time_s": float(1000 - index),
                    "operating_point_status": {
                        "linear_count": 20 - index,
                        "near_edge_count": 20 - index,
                        "min_margin_v": float(index),
                    },
                },
            })
        return {
            "search_space": [{
                "name": "Cc",
                "low": 1.0,
                "high": float(count),
                "log_scale": False,
                "value_type": "float",
            }],
            "history": records,
        }


if __name__ == "__main__":
    unittest.main()
