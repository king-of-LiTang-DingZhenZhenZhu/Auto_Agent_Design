from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from knowledge_review import build_knowledge_analysis
from psf_results import (
    calculate_comparator_decision_metrics,
    parse_psf_results,
)
from topologies import get_topology, get_topology_for_targets
from topologies.strongarm_latch import default_strongarm_targets


class StrongARMLatchTest(unittest.TestCase):
    def test_selector_and_paper_connections(self):
        topology = get_topology("strongarm_latch")
        circuit = topology.generate_circuit()

        self.assertEqual(
            get_topology_for_targets(default_strongarm_targets()),
            "strongarm_latch",
        )
        self.assertRegex(
            circuit,
            r"(?m)^subckt strongarm_latch "
            r"\(vip vin clk outp outn vdd vss\)",
        )
        self.assertIn("M1 (p vip ntail vss)", circuit)
        self.assertIn("M2 (q vin ntail vss)", circuit)
        self.assertIn("M7 (ntail clk vss vss)", circuit)
        self.assertIn("M3 (outn outp p vss)", circuit)
        self.assertIn("M4 (outp outn q vss)", circuit)
        self.assertIn("M5 (outn outp vdd vdd)", circuit)
        self.assertIn("M6 (outp outn vdd vdd)", circuit)
        self.assertIn("S1 (p clk vdd vdd)", circuit)
        self.assertIn("S2 (q clk vdd vdd)", circuit)
        self.assertIn("S3 (outn clk vdd vdd)", circuit)
        self.assertIn("S4 (outp clk vdd vdd)", circuit)
        self.assertIsNone(topology.get_gmid_spec())
        self.assertEqual(topology.critical_operating_point_instances(), set())

    def test_generates_positive_and_negative_decision_testbenches(self):
        topology = get_topology("strongarm_latch")
        files = topology.get_circuit_files()

        self.assertEqual(
            files.testbench_suffixes,
            ["decision_pos", "decision_neg"],
        )
        positive, negative = files.testbenches
        self.assertIn("decisionPosTran tran stop=9n maxstep=1p", positive)
        self.assertIn("decisionNegTran tran stop=9n maxstep=1p", negative)
        self.assertIn("parameters VDD=900m VCM=450m VDIFF=10m", positive)
        self.assertIn("parameters VDD=900m VCM=450m VDIFF=-10m", negative)
        self.assertIn(
            "Xdut (vip vin clk outp outn vdd vss) strongarm_latch",
            positive,
        )
        self.assertIn("save VDDsrc:p", positive)

    def test_project_records_comparator_metric_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = get_topology("strongarm_latch").write_project(
                Path(tmp) / "strongarm",
                targets=default_strongarm_targets(),
                original_requirement="Razavi modified StrongARM latch",
            )
            requirements = json.loads(
                (project / "requirements.json").read_text(encoding="utf-8")
            )
            names = {path.name for path in project.iterdir()}

        self.assertIn("tb_strongarm_latch_decision_pos.scs", names)
        self.assertIn("tb_strongarm_latch_decision_neg.scs", names)
        self.assertEqual(requirements["topology_name"], "strongarm_latch")
        self.assertEqual(
            requirements["metric_goals"]["energy_per_decision_j"]["constraint"],
            "max",
        )

    def test_decision_metric_math(self):
        time = np.linspace(0.0, 9e-9, 9001)
        clock = np.zeros_like(time)
        for start in (1e-9, 5e-9):
            clock[(time >= start) & (time < start + 2e-9)] = 0.9
        outp = np.full_like(time, 0.9)
        outn = np.full_like(time, 0.9)
        evaluate = (time >= 1e-9) & (time < 3e-9)
        outn[evaluate] = 0.9 * np.exp(-(time[evaluate] - 1e-9) / 0.2e-9)
        supply = np.full_like(time, 0.9)
        power = np.full_like(time, -100e-6)

        margin, delay, energy, average_power = (
            calculate_comparator_decision_metrics(
                time,
                clock,
                outp,
                outn,
                supply,
                power,
                expect_positive=True,
            )
        )

        self.assertGreater(margin, 0.8)
        self.assertAlmostEqual(delay, 0.2e-9 * np.log(2), delta=2e-12)
        self.assertAlmostEqual(energy, 400e-15, delta=1e-15)
        self.assertAlmostEqual(average_power, 100e-6, delta=1e-9)

    def test_psf_dispatch_and_knowledge_domain(self):
        time = np.linspace(0.0, 9e-9, 9001)
        clock = np.zeros_like(time)
        for start in (1e-9, 5e-9):
            clock[(time >= start) & (time < start + 2e-9)] = 0.9
        outp = np.full_like(time, 0.9)
        outn = np.full_like(time, 0.9)
        evaluate = (time >= 1e-9) & (time < 3e-9)
        outn[evaluate] = 0.9 * np.exp(-(time[evaluate] - 1e-9) / 0.2e-9)

        class FakeSignal:
            def __init__(self, name, ordinate):
                self.name = name
                self.ordinate = ordinate

        class FakePSF:
            def __init__(self, _path):
                self.signals = {
                    "clk": FakeSignal("clk", clock),
                    "outp": FakeSignal("outp", outp),
                    "outn": FakeSignal("outn", outn),
                    "vdd": FakeSignal("vdd", np.full_like(time, 0.9)),
                    "VDDsrc:p": FakeSignal(
                        "VDDsrc:p", np.full_like(time, -100e-6)
                    ),
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
            (raw / "decisionPosTran.tran").touch()
            result = parse_psf_results(
                raw,
                "decisionPosTran tran stop=9n maxstep=1p",
            )

        self.assertIsNotNone(result)
        self.assertGreater(result.raw_metrics["decision_positive_margin_v"], 0.8)
        self.assertAlmostEqual(result.power_w, 100e-6, delta=1e-9)

        analysis = build_knowledge_analysis(
            "strongarm_latch",
            history={"targets": {}},
            records=[{
                "iteration": 1,
                "params": {
                    "Winput_n": 2e-6,
                    "Wlatch_n": 1e-6,
                    "Wlatch_p": 2e-6,
                },
                "result": {
                    "raw_metrics": result.raw_metrics,
                    "power_w": result.power_w,
                },
            }],
            workspace="unused",
        )
        self.assertEqual(analysis["domain"], "comparator")
        self.assertEqual(
            analysis["run_analyses"][0]["derived"][
                "input_to_n_latch_width_ratio"
            ],
            2.0,
        )


if __name__ == "__main__":
    unittest.main()
