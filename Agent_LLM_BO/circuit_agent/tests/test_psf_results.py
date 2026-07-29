from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from models import DesignTarget, MetricGoal, SimResult, parse_metric_goals
from psf_results import (
    calculate_ac_metrics,
    calculate_dc_psr_db,
    calculate_line_regulation,
    calculate_load_regulation,
    calculate_load_transient_metrics,
    calculate_psrr_db,
    calculate_settling_times,
    calculate_slew_rates,
    calculate_startup_metrics,
    calculate_temperature_metrics,
    parse_psf_results,
)


class PsfResultMathTest(unittest.TestCase):
    def test_metric_goals_round_trip_through_requirements(self):
        targets = DesignTarget(
            gain_db=60,
            power_w=1e-3,
            metric_goals={
                "psrr_db": MetricGoal(
                    constraint="min",
                    target=50,
                    objective="maximize",
                    priority=0.5,
                )
            },
        )

        requirements = targets.to_requirements_dict()
        restored = parse_metric_goals(requirements["metric_goals"])

        self.assertEqual(restored["gain_db"].constraint, "min")
        self.assertEqual(restored["power_w"].objective, "minimize")
        self.assertEqual(restored["psrr_db"].objective, "maximize")
        self.assertEqual(restored["psrr_db"].priority, 0.5)

    def test_bandgap_analysis_names_dispatch_to_specialized_psf_parsers(self):
        time = np.linspace(0.0, 10e-6, 1001)
        vdd = np.minimum(time / 1e-6, 1.0) * 1.8
        startup_vref = np.where(
            time >= 1e-6,
            1.2 * (1.0 - np.exp(-(time - 1e-6) / 0.5e-6)),
            0.0,
        )
        temperature = np.linspace(-40.0, 125.0, 166)
        line_vdd = np.array([1.6, 1.8, 2.0])
        datasets = {
            "startupTran.tran": (time, {"vdd": vdd, "vout": startup_vref}),
            "psrrAC.ac": (
                np.array([1.0, 1e3, 1e6]),
                {"vout": np.array([1e-3, 2e-3, 1e-2])},
            ),
            "tempSweep.dc": (
                temperature,
                {"vout": 1.2 + 1e-6 * (temperature - 27.0) ** 2},
            ),
            "lineSweep.dc": (
                line_vdd,
                {"vout": np.array([1.1998, 1.2, 1.2002])},
            ),
        }

        class FakeSignal:
            def __init__(self, name, ordinate):
                self.name = name
                self.ordinate = ordinate

        class FakePSF:
            def __init__(self, path):
                self.axis, values = datasets[Path(path).name]
                self.signals = {
                    name: FakeSignal(name, ordinate)
                    for name, ordinate in values.items()
                }

            def get_sweep(self):
                return SimpleNamespace(abscissa=self.axis)

            def all_signals(self):
                return list(self.signals.values())

            def get_signal(self, name):
                return self.signals[name]

        fake_module = SimpleNamespace(PSF=FakePSF)
        testbenches = {
            "startupTran.tran": "startupTran tran stop=10u",
            "psrrAC.ac": "psrrAC ac start=1 stop=100M dec=20",
            "tempSweep.dc": "tempSweep dc param=temp start=-40 stop=125 step=1",
            "lineSweep.dc": "lineSweep dc param=VDD start=1.6 stop=2 step=0.02",
        }

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, {"psf_utils": fake_module}
        ):
            raw_dir = Path(tmp)
            for filename in datasets:
                (raw_dir / filename).touch()
            results = {
                filename: parse_psf_results(raw_dir, testbench)
                for filename, testbench in testbenches.items()
            }

        self.assertTrue(results["startupTran.tran"].startup_success)
        self.assertAlmostEqual(results["psrrAC.ac"].psrr_db, 40.0)
        self.assertAlmostEqual(results["tempSweep.dc"].vref_v, 1.2)
        self.assertAlmostEqual(
            results["lineSweep.dc"].line_regulation_v_per_v, 1e-3
        )

    def test_bandgap_targets_require_all_requested_metrics(self):
        targets = DesignTarget(
            vref_v=1.2,
            vref_tolerance_v=5e-3,
            tempco_ppm_per_c=20,
            vref_temp_nonlinearity_v=1e-3,
            psrr_db=50,
            line_regulation_v_per_v=1e-3,
            startup_time_s=5e-6,
        )
        result = SimResult(
            vref_v=1.202,
            tempco_ppm_per_c=15,
            vref_temp_nonlinearity_v=0.5e-3,
            psrr_db=55,
            line_regulation_v_per_v=0.8e-3,
            startup_time_s=4e-6,
            startup_success=True,
        )

        all_met, status = targets.is_satisfied(result)

        self.assertTrue(all_met)
        self.assertTrue(all(status.values()))
        self.assertTrue(result.to_result_dict(targets)["all_targets_met"])

    def test_bandgap_extra_testbench_power_survives_result_merge(self):
        merged = SimResult.merge(
            SimResult(startup_success=True, startup_time_s=2e-6),
            SimResult(psrr_db=60, power_w=100e-6),
        )

        self.assertEqual(merged.power_w, 100e-6)
        self.assertEqual(merged.psrr_db, 60)

    def test_startup_metrics_reject_zero_state_and_measure_settling(self):
        time = np.linspace(0.0, 10e-6, 1001)
        vdd = np.minimum(time / 1e-6, 1.0) * 1.8
        vref = np.where(
            time >= 1e-6,
            1.2 * (1.0 - np.exp(-(time - 1e-6) / 0.5e-6)),
            0.0,
        )

        success, startup_time, final_vref = calculate_startup_metrics(
            time, vdd, vref
        )
        zero_success, _, _ = calculate_startup_metrics(
            time, vdd, np.zeros_like(time)
        )

        self.assertTrue(success)
        self.assertLess(startup_time, 5e-6)
        self.assertAlmostEqual(final_vref, 1.2, places=3)
        self.assertFalse(zero_success)

    def test_bandgap_psrr_temperature_and_line_metrics(self):
        frequency = np.array([1.0, 1e3, 1e6])
        supply_to_output = np.array([1e-3, 2e-3, 1e-2])
        self.assertAlmostEqual(
            calculate_psrr_db((frequency, supply_to_output)), 40.0
        )

        temperature = np.linspace(-40.0, 125.0, 166)
        vref = 1.2 + 1e-6 * (temperature - 27.0) ** 2
        nominal, tempco, nonlinearity = calculate_temperature_metrics(
            (temperature, vref)
        )
        self.assertAlmostEqual(nominal, 1.2, places=8)
        self.assertGreater(tempco, 0.0)
        self.assertGreater(nonlinearity, 0.0)

        vdd = np.array([1.6, 1.8, 2.0])
        line_vref = np.array([1.1998, 1.2, 1.2002])
        self.assertAlmostEqual(
            calculate_line_regulation((vdd, line_vref)), 1e-3
        )

    def test_ldo_dc_and_transient_metrics(self):
        load = np.array([0.0, 5e-3, 10e-3])
        output = np.array([0.9000, 0.8999, 0.8997])
        nominal, load_regulation = calculate_load_regulation((load, output))
        self.assertAlmostEqual(nominal, 0.9)
        self.assertAlmostEqual(load_regulation, 0.03)

        frequency = np.array([1e-3, 1e-2, 1e-1])
        transfer = np.array([10 ** (-64 / 20), 1e-3, 2e-3])
        self.assertAlmostEqual(
            calculate_dc_psr_db((frequency, transfer)),
            -64.0,
        )

        time = np.linspace(0.0, 12e-6, 12001)
        current = np.zeros_like(time)
        current[(time >= 2e-6) & (time < 7e-6)] = 10e-3
        vout = np.full_like(time, 0.9)
        vout[(time >= 2e-6) & (time < 7e-6)] = 0.88
        vout[(time >= 7e-6) & (time < 8e-6)] = 0.93
        overshoot, undershoot = calculate_load_transient_metrics(
            time,
            current,
            vout,
        )
        self.assertAlmostEqual(overshoot, 0.03)
        self.assertAlmostEqual(undershoot, 0.02)

    def test_custom_ldo_metrics_are_written_to_results(self):
        targets = DesignTarget(
            metric_goals={
                "dc_psr_db": MetricGoal(constraint="max", target=-62),
                "overshoot_v": MetricGoal(constraint="max", target=0.25),
            }
        )
        result = SimResult(
            raw_metrics={"dc_psr_db": -64.0, "overshoot_v": 0.2}
        )

        payload = result.to_result_dict(targets)

        self.assertTrue(payload["all_targets_met"])
        self.assertEqual(payload["metrics"]["dc_psr_db"], -64.0)

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
