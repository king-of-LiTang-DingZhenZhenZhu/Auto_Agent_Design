from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from config import Settings
from models import DesignTarget, IterationRecord, OptimizationState, ParamSpace, SimResult
from optimizer import HybridOptimizer


class OptimizerEarlyStopTest(unittest.TestCase):
    def _optimizer(self) -> HybridOptimizer:
        return HybridOptimizer(None, None, Settings())

    def _state_with_results(self, targets: DesignTarget, results: list[SimResult]):
        state = OptimizationState(targets=targets, param_space=ParamSpace())
        for i, result in enumerate(results):
            state.update(
                IterationRecord(
                    iteration=i,
                    params={},
                    result=result,
                    reward=-100.0,
                )
            )
        return state

    def test_detects_five_consecutive_severe_deviations(self):
        targets = DesignTarget(gain_db=60, bandwidth_hz=100e6)
        results = [
            SimResult(gain_db=-5, bandwidth_hz=100e3)
            for _ in range(5)
        ]
        state = self._state_with_results(targets, results)

        self.assertTrue(
            self._optimizer()._detect_repeated_severe_deviation(state, targets)
        )

    def test_non_severe_result_resets_recent_window(self):
        targets = DesignTarget(gain_db=60, bandwidth_hz=100e6)
        results = [
            SimResult(gain_db=-5, bandwidth_hz=100e3)
            for _ in range(4)
        ]
        results.append(SimResult(gain_db=45, bandwidth_hz=20e6))
        state = self._state_with_results(targets, results)

        self.assertFalse(
            self._optimizer()._detect_repeated_severe_deviation(state, targets)
        )

    def test_missing_key_ac_metric_is_severe(self):
        targets = DesignTarget(gain_db=60, bandwidth_hz=100e6)
        self.assertTrue(
            self._optimizer()._is_severe_deviation(SimResult(), targets)
        )

    def test_reward_penalizes_excessive_phase_margin(self):
        targets = DesignTarget(phase_margin_deg=60)
        optimizer = self._optimizer()

        moderate_pm = optimizer.compute_reward(
            SimResult(phase_margin_deg=70),
            targets,
        )
        excessive_pm = optimizer.compute_reward(
            SimResult(phase_margin_deg=90),
            targets,
        )

        self.assertLess(excessive_pm, moderate_pm)

    def test_writes_iteration_summary_and_metrics_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(workspace_dir=tmp)
            optimizer = HybridOptimizer(None, None, settings)
            run_dir = Path(tmp) / "run_000"
            run_dir.mkdir()
            result = SimResult(
                gain_db=50.0,
                bandwidth_hz=100e6,
                unity_gain_freq_hz=100e6,
                phase_margin_deg=65.0,
                power_w=100e-6,
                slew_rate_v_per_s=120e6,
                slew_rate_positive_v_per_s=130e6,
                slew_rate_negative_v_per_s=120e6,
                settling_time_s=20e-9,
            )

            optimizer._write_iteration_summary(
                run_dir=run_dir,
                iteration=0,
                result=result,
                reward=12.5,
                tb_paths=[run_dir / "tb.scs"],
            )
            summary = (run_dir / "metrics_summary.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn("Iteration: 1", summary)
            self.assertIn("Gain", summary)
            self.assertIn("Slew Rate", summary)
            self.assertIn("Settling Time 0.1%", summary)

            state = OptimizationState(
                targets=DesignTarget(), param_space=ParamSpace()
            )
            state.update(
                IterationRecord(
                    iteration=0,
                    params={},
                    result=result,
                    reward=12.5,
                )
            )
            optimizer._save_metrics_csv(state)

            csv_path = Path(tmp) / "optimization_metrics.csv"
            with csv_path.open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["iteration"], "1")
            self.assertNotIn("converged", rows[0])
            self.assertNotIn("bandwidth_hz", rows[0])
            self.assertNotIn("unity_gain_freq_hz", rows[0])
            self.assertNotIn("slew_rate_positive_v_per_s", rows[0])
            self.assertNotIn("slew_rate_negative_v_per_s", rows[0])
            self.assertEqual(rows[0]["gain_db(dB)"], "50.00")
            self.assertEqual(rows[0]["gbw_hz(MHz)"], "100.00")
            self.assertEqual(rows[0]["phase_margin_deg(deg)"], "65.00")
            self.assertEqual(rows[0]["power_w(mW)"], "0.100")
            self.assertEqual(rows[0]["slew_rate_v_per_s(V/us)"], "120.00")
            self.assertEqual(rows[0]["settling_time_s(ns)"], "20.00")


if __name__ == "__main__":
    unittest.main()
