from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
