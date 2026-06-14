from __future__ import annotations

import math
import unittest

import numpy as np

from psf_results import calculate_ac_metrics, calculate_slew_rate


class PsfResultMathTest(unittest.TestCase):
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

    def test_slew_rate(self):
        time = np.linspace(0.0, 1e-6, 1001)
        voltage = 2e6 * time
        self.assertAlmostEqual(calculate_slew_rate(time, voltage), 2e6, places=3)


if __name__ == "__main__":
    unittest.main()
