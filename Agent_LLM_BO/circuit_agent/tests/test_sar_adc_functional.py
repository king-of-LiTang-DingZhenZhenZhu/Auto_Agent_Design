from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from config import Settings
from models import NetlistTemplate
from psf_results import calculate_adc_functional_metrics, parse_psf_results
from simulator import Simulator
from system_decomposition import (
    SystemDecompositionError,
    SystemDesignRequest,
    decompose_system,
    write_system_project,
)
from topologies import get_topology
from topologies.converters.sar_adc_functional_4bit import (
    default_sar_adc_functional_targets,
)


def _adc_waveforms(
    *,
    code_overrides: dict[int, int] | None = None,
    missing_eoc: set[int] | None = None,
    eoc_index: int = 7,
):
    code_overrides = code_overrides or {}
    missing_eoc = missing_eoc or set()
    points_per_conversion = 12
    count = 16 * points_per_conversion
    time = np.arange(count, dtype=float) * 100e-9
    signals = {
        name: np.zeros(count)
        for name in ("start", "eoc", "d3", "d2", "d1", "d0", "vin_sampled")
    }
    for expected_code in range(16):
        base = expected_code * points_per_conversion
        signals["start"][base + 1:base + 3] = 0.9
        signals["vin_sampled"][base:base + points_per_conversion] = (
            (expected_code + 0.5) * 0.9 / 16.0
        )
        if expected_code in missing_eoc:
            continue
        output_code = code_overrides.get(expected_code, expected_code)
        signals["eoc"][base + eoc_index:base + eoc_index + 2] = 0.9
        for bit_index, name in zip((3, 2, 1, 0), ("d3", "d2", "d1", "d0")):
            if output_code & (1 << bit_index):
                signals[name][base + eoc_index:base + eoc_index + 2] = 0.9
    signals["vdd"] = np.full(count, 0.9)
    signals["vref_metric"] = np.full(count, 0.9)
    return time, signals


class SARADCFunctionalTests(unittest.TestCase):
    def test_topology_generates_verilog_a_project_and_render_copy(self):
        topology = get_topology("sar_adc_functional_4bit")
        self.assertFalse(topology.supports_schematic_generation())
        files = topology.get_circuit_files()
        self.assertIn('ahdl_include "sar_adc_functional_4bit.va"', files.circuit_netlist)
        self.assertEqual(files.testbench_suffixes, ["adc_functional"])
        self.assertIn("sar_adc_functional_4bit.va", files.auxiliary_files)
        self.assertIn("adcFunctionalTran tran", files.testbenches[0])

        with tempfile.TemporaryDirectory() as tmp:
            project = topology.write_project(
                Path(tmp) / "adc",
                targets=default_sar_adc_functional_targets(),
            )
            self.assertTrue((project / "sar_adc_functional_4bit.va").exists())
            run_dir = Path(tmp) / "run"
            Simulator(Settings(dry_run=True)).render_circuit_and_testbench(
                NetlistTemplate.from_netlist(files.circuit_netlist),
                files.testbenches,
                topology.get_default_params(),
                run_dir,
                auxiliary_files=files.auxiliary_files,
            )
            self.assertTrue((run_dir / "sar_adc_functional_4bit.va").exists())

            simulator = Simulator(Settings(dry_run=True))
            ok, log, error = simulator.run_spectre(
                run_dir / "tb.scs", run_dir
            )
            self.assertTrue(ok, error)
            dry_result = simulator.parse_simulation_log(log)
            all_met, _ = default_sar_adc_functional_targets().is_satisfied(
                dry_result
            )
            self.assertTrue(all_met)

            with self.assertRaisesRegex(ValueError, "Unsafe"):
                simulator.render_circuit_and_testbench(
                    NetlistTemplate.from_netlist(files.circuit_netlist),
                    files.testbenches,
                    topology.get_default_params(),
                    Path(tmp) / "unsafe",
                    auxiliary_files={"../model.va": "module model; endmodule"},
                )

    def test_behavioral_system_request_selects_functional_parent(self):
        request = SystemDesignRequest(
            system_type="SAR ADC",
            voltage_domain="core_0p9",
            targets=default_sar_adc_functional_targets(),
        )
        spec = decompose_system(request)
        self.assertEqual(spec.parent_topology, "sar_adc_functional_4bit")
        self.assertEqual(
            next(block for block in spec.blocks if block.block_id == "cdac")
            .operating_conditions["segmentation_bits"],
            [2, 2],
        )
        cdac = next(block for block in spec.blocks if block.block_id == "cdac")
        comparator = next(
            block for block in spec.blocks if block.block_id == "comparator"
        )
        sar_logic = next(
            block for block in spec.blocks if block.block_id == "sar_logic"
        )
        self.assertEqual(cdac.operating_conditions["thermometer_coded_msb_bits"], 0)
        self.assertEqual(comparator.targets.metric_goals, {})
        self.assertEqual(sar_logic.operating_conditions["clocks_per_conversion"], 5)
        self.assertTrue(all("12 bit" not in item for item in spec.assumptions))

        with tempfile.TemporaryDirectory() as tmp:
            project, written_spec = write_system_project(request, Path(tmp) / "adc")
            self.assertEqual(written_spec.parent_topology, "sar_adc_functional_4bit")
            self.assertTrue((project / "system_design.json").exists())
            self.assertTrue((project / "sar_adc_functional_4bit.va").exists())

    def test_behavioral_system_request_rejects_non_four_bit_resolution(self):
        targets = default_sar_adc_functional_targets()
        targets.custom_specs["resolution_bits"] = 5
        with self.assertRaisesRegex(SystemDecompositionError, "exactly 4 bits"):
            decompose_system(SystemDesignRequest(system_type="SAR ADC", targets=targets))

    def test_functional_metrics_pass_all_sixteen_codes(self):
        time, signals = _adc_waveforms()
        metrics = calculate_adc_functional_metrics(
            time,
            signals["start"],
            signals["eoc"],
            [signals["d3"], signals["d2"], signals["d1"], signals["d0"]],
            signals["vin_sampled"],
            signals["vdd"],
        )
        self.assertEqual(metrics["conversion_count"], 16.0)
        self.assertEqual(metrics["conversion_success_rate"], 1.0)
        self.assertEqual(metrics["max_code_error_lsb"], 0.0)
        self.assertEqual(metrics["missing_code_count"], 0.0)
        self.assertEqual(metrics["monotonicity_violation_count"], 0.0)

    def test_functional_metrics_report_error_missing_code_and_nonmonotonicity(self):
        time, signals = _adc_waveforms(
            code_overrides={8: 0},
            missing_eoc={5},
        )
        metrics = calculate_adc_functional_metrics(
            time,
            signals["start"],
            signals["eoc"],
            [signals["d3"], signals["d2"], signals["d1"], signals["d0"]],
            signals["vin_sampled"],
            signals["vdd"],
        )
        self.assertLess(metrics["conversion_success_rate"], 1.0)
        self.assertGreater(metrics["max_code_error_lsb"], 0.0)
        self.assertGreater(metrics["missing_code_count"], 0.0)
        self.assertGreater(metrics["monotonicity_violation_count"], 0.0)

    def test_functional_metrics_measure_conversion_timeout(self):
        time, signals = _adc_waveforms(eoc_index=10)
        metrics = calculate_adc_functional_metrics(
            time,
            signals["start"],
            signals["eoc"],
            [signals["d3"], signals["d2"], signals["d1"], signals["d0"]],
            signals["vin_sampled"],
            signals["vdd"],
        )
        self.assertAlmostEqual(metrics["conversion_time_max_s"], 900e-9)

    def test_psf_dispatches_adc_functional_analysis(self):
        time, signals = _adc_waveforms()

        class FakeSignal:
            def __init__(self, name, ordinate):
                self.name = name
                self.ordinate = ordinate

        class FakePSF:
            def __init__(self, _path):
                self.signals = {
                    name: FakeSignal(name, values)
                    for name, values in signals.items()
                }

            def get_sweep(self):
                return SimpleNamespace(abscissa=time)

            def get_signal(self, name):
                return self.signals[name]

            def all_signals(self):
                return list(self.signals.values())

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, {"psf_utils": SimpleNamespace(PSF=FakePSF)}
        ):
            raw = Path(tmp)
            (raw / "adcFunctionalTran.tran").touch()
            result = parse_psf_results(
                raw,
                "adcFunctionalTran tran stop=32u maxstep=8n",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.raw_metrics["conversion_success_rate"], 1.0)
        all_met, _ = default_sar_adc_functional_targets().is_satisfied(result)
        self.assertTrue(all_met)


if __name__ == "__main__":
    unittest.main()
