from __future__ import annotations

import math
import unittest

import numpy as np

from models import DesignTarget, SimResult
from psf_results import (
    calculate_ac_metrics,
    calculate_settling_times,
    calculate_slew_rates,
)


class PsfResultMathTest(unittest.TestCase):
    def test_missing_transient_metrics_fail_requested_targets(self):
        targets = DesignTarget(
            slew_rate_v_per_s=1e6,
            settling_time_s=10e-9,
        )
        all_met, status = targets.is_satisfied(SimResult())
        self.assertFalse(all_met)
        self.assertFalse(status["slew_rate_v_per_s"])
        self.assertFalse(status["settling_time_s"])

    def test_ac_metrics_use_first_zero_db_crossing(self):
        frequency = np.logspace(1, 9, 801)
        pole_hz = 1e4
        dc_gain = 100.0
        response = dc_gain / (1.0 + 1j * frequency / pole_hz)

        gain_db, ugf_hz, phase_margin_deg = calculate_ac_metrics(
            (frequency, response)
        )

        expected_ugf = pole_hz * math.sqrt(dc_gain**2 - 1.0)
        self.assertAlmostEqual(gain_db, 40.0, places=3)
        self.assertIsNotNone(ugf_hz)
        self.assertAlmostEqual(ugf_hz / expected_ugf, 1.0, places=3)
        self.assertIsNotNone(phase_margin_deg)
        self.assertAlmostEqual(phase_margin_deg, 90.57, places=1)

    def test_ac_metrics_report_missing_crossing(self):
        frequency = np.logspace(1, 5, 101)
        response = np.full(frequency.shape, 10.0 + 0.0j)

        gain_db, ugf_hz, phase_margin_deg = calculate_ac_metrics(
            (frequency, response)
        )

        self.assertAlmostEqual(gain_db, 20.0)
        self.assertIsNone(ugf_hz)
        self.assertIsNone(phase_margin_deg)

    def test_slew_rates_use_separate_10_to_90_percent_transitions(self):
        time = np.linspace(0.0, 10e-6, 10001)
        vinp = np.zeros_like(time)
        vinp[(time >= 1e-6) & (time < 6e-6)] = 1.0

        vout = np.zeros_like(time)
        rising = (time >= 1e-6) & (time < 3e-6)
        vout[rising] = (time[rising] - 1e-6) * 0.5e6
        vout[(time >= 3e-6) & (time < 6e-6)] = 1.0
        falling = (time >= 6e-6) & (time < 7e-6)
        vout[falling] = 1.0 - (time[falling] - 6e-6) * 1.0e6

        sr_positive, sr_negative, slew_rate = calculate_slew_rates(
            time, vinp, vout
        )

        self.assertAlmostEqual(sr_positive, 0.5e6, places=2)
        self.assertAlmostEqual(sr_negative, 1.0e6, places=2)
        self.assertAlmostEqual(slew_rate, 0.5e6, places=2)

    def test_slew_rates_ignore_spikes_outside_output_range(self):
        time = np.linspace(0.0, 10e-6, 10001)
        vinp = np.zeros_like(time)
        vinp[(time >= 1e-6) & (time < 6e-6)] = 1.0

        vout = np.zeros_like(time)
        rising = (time >= 1e-6) & (time < 2e-6)
        vout[rising] = (time[rising] - 1e-6) * 1.0e6
        vout[(time >= 2e-6) & (time < 6e-6)] = 1.0
        falling = (time >= 6e-6) & (time < 7e-6)
        vout[falling] = 1.0 - (time[falling] - 6e-6) * 1.0e6
        vout[100] = 5.0

        sr_positive, sr_negative, slew_rate = calculate_slew_rates(
            time, vinp, vout
        )

        self.assertAlmostEqual(sr_positive, 1.0e6, places=2)
        self.assertAlmostEqual(sr_negative, 1.0e6, places=2)
        self.assertAlmostEqual(slew_rate, 1.0e6, places=2)

    def test_settling_time_uses_last_0_1_percent_error_crossing(self):
        time = np.linspace(0.0, 10e-6, 10001)
        vinp = np.zeros_like(time)
        vinp[(time >= 1e-6) & (time < 6e-6)] = 1.0

        vout = np.zeros_like(time)
        rise = time >= 1e-6
        vout[rise] = 1.0 - np.exp(-(time[rise] - 1e-6) / 0.2e-6)
        fall = time >= 6e-6
        vout[fall] = np.exp(-(time[fall] - 6e-6) / 0.4e-6)

        rise_st, fall_st, worst_st = calculate_settling_times(
            time, vinp, vout, tolerance=0.001
        )

        self.assertAlmostEqual(rise_st / (0.2e-6 * np.log(1000)), 1.0, delta=0.02)
        self.assertAlmostEqual(fall_st / (0.4e-6 * np.log(1000)), 1.0, delta=0.02)
        self.assertEqual(worst_st, fall_st)


if __name__ == "__main__":
    unittest.main()
