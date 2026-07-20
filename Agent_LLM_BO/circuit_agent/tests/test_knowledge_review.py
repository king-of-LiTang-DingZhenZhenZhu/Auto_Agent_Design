from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from knowledge_review import build_knowledge_analysis, write_knowledge_analysis


class KnowledgeReviewTests(unittest.TestCase):
    def test_two_stage_analysis_derives_gm_gbw_and_pole_ratio(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "run_003"
            diagnostics = run_dir / "diagnostics"
            diagnostics.mkdir(parents=True)
            (run_dir / "circuit.cir").write_text(
                "parameters Cc=1p Rz=1k\n"
                "subckt two_stage_ota vip vin vout ibias vdd vss\n"
                "ends two_stage_ota\n",
                encoding="utf-8",
            )
            (diagnostics / "dc_operating_points.csv").write_text(
                "instance,model,gm,vds,vdsat\n"
                "Xdut.Mdiff1,nch,1e-3,0.3,0.1\n"
                "Xdut.Mdiff2,nch,1e-3,0.3,0.1\n",
                encoding="utf-8",
            )
            record = {
                "iteration": 3,
                "reward": 2.0,
                "params": {"Cc": 1e-12},
                "result": {
                    "converged": True,
                    "bandwidth_hz": 100e6,
                    "phase_margin_deg": 45.0,
                },
            }

            analysis = build_knowledge_analysis(
                topology_name="two_stage_ota",
                history={
                    "targets": {
                        "bandwidth_hz": 100e6,
                        "phase_margin_deg": 60.0,
                    }
                },
                records=[record],
                workspace=workspace,
            )

        derived = analysis["run_analyses"][0]["derived"]
        self.assertAlmostEqual(derived["measured_input_gm_S"], 1e-3)
        self.assertAlmostEqual(
            derived["first_order_predicted_gbw_hz"],
            1e-3 / (2 * math.pi * 1e-12),
        )
        self.assertAlmostEqual(
            derived["input_gm_required_for_target_S"],
            2 * math.pi * 100e6 * 1e-12,
        )
        self.assertAlmostEqual(derived["two_pole_estimated_p2_over_ugf"], 1.0)
        self.assertAlmostEqual(
            derived["p2_over_ugf_required_for_target_pm"], math.tan(math.radians(60))
        )
        self.assertTrue(analysis["run_analyses"][0]["diagnoses"])

    def test_bandgap_analysis_reports_delta_vbe_and_missing_signoff_data(self):
        record = {
            "iteration": 1,
            "reward": 1.0,
            "params": {
                "BJT_AREA_RATIO": 8,
                "R0_SEG_L": 10e-6,
                "R0_SEG_W": 2e-6,
                "R1_SEG_L": 10e-6,
                "R1_SEG_W": 2e-6,
            },
            "result": {"converged": True},
        }

        analysis = build_knowledge_analysis(
            topology_name="bandgap_ptat",
            history={"targets": {}},
            records=[record],
            workspace=Path("unused"),
        )

        run = analysis["run_analyses"][0]
        self.assertAlmostEqual(
            run["derived"]["delta_vbe_27c_first_order_V"],
            25.852e-3 * math.log(8),
        )
        self.assertEqual(run["derived"]["r1_over_r0_first_order"], 2.0)
        self.assertTrue(any("temperature sweep" in item for item in run["unavailable"]))
        self.assertTrue(analysis["limitations"])

    def test_unknown_topology_and_report_output_are_safe(self):
        analysis = build_knowledge_analysis(
            topology_name="unknown",
            history={},
            records=[],
            workspace=Path("unused"),
        )
        self.assertEqual(analysis["status"], "no_structured_knowledge")

        with tempfile.TemporaryDirectory() as tmp:
            json_path, markdown_path = write_knowledge_analysis(analysis, tmp)
            self.assertTrue(json_path.exists())
            self.assertIn(
                "Knowledge-Driven Circuit Analysis",
                markdown_path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
