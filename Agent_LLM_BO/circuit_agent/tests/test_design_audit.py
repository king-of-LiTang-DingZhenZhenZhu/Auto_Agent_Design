import json
import tempfile
import unittest
from pathlib import Path

from design_audit import run_design_audit


class DesignAuditTests(unittest.TestCase):
    def test_reports_geometry_and_power_optimization_opportunities(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            netlist = project / "circuit.cir"
            netlist.write_text(
                "parameters Wbig=600u Lbig=500n\n"
                "Mbig (out in vss vss) nch w=Wbig l=Lbig nf=2\n",
                encoding="utf-8",
            )
            results = project / "results.json"
            results.write_text(
                json.dumps(
                    {
                        "all_targets_met": True,
                        "metrics": {
                            "gain_db": 80,
                            "gbw_hz": 150e6,
                            "power_w": 0.9e-3,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (project / "optimization_log.json").write_text(
                json.dumps(
                    {
                        "targets": {
                            "gain_db": 60,
                            "bandwidth_hz": 100e6,
                            "power_w": 1e-3,
                        }
                    }
                ),
                encoding="utf-8",
            )

            report = run_design_audit(project, results, netlist)

            codes = {item["code"] for item in report["findings"]}
            self.assertEqual(report["status"], "warn")
            self.assertIn("very_large_mos_width", codes)
            self.assertIn("power_reduction_opportunity", codes)
            large = next(
                item for item in report["findings"] if item["code"] == "very_large_mos_width"
            )
            self.assertAlmostEqual(
                large["evidence"]["devices"][0]["effective_width_m"], 600e-6
            )
            self.assertTrue((project / "design_audit" / "design_audit.json").exists())
            self.assertTrue((project / "design_audit" / "design_audit.md").exists())

    def test_critical_linear_device_blocks_pvt_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            netlist = project / "circuit.cir"
            netlist.write_text(
                "parameters W=100u L=1u\n"
                "M1 (out in vss vss) nch w=W l=L nf=8 m=2\n",
                encoding="utf-8",
            )
            results = project / "results.json"
            results.write_text(
                json.dumps(
                    {
                        "all_targets_met": True,
                        "operating_point_status": {
                            "critical_linear": ["M1"],
                            "critical_near_edge": [],
                        },
                    }
                ),
                encoding="utf-8",
            )

            report = run_design_audit(project, results, netlist)

            self.assertEqual(report["status"], "block")
            self.assertEqual(report["blocker_count"], 1)

    def test_reasonable_design_passes_without_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            netlist = project / "circuit.cir"
            netlist.write_text(
                "parameters W=10u L=1u\n"
                "M1 (out in vss vss) nch w=W l=L nf=1\n",
                encoding="utf-8",
            )
            results = project / "results.json"
            results.write_text(
                json.dumps({"all_targets_met": True, "metrics": {"power_w": 1e-4}}),
                encoding="utf-8",
            )

            report = run_design_audit(project, results, netlist)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["findings"], [])
