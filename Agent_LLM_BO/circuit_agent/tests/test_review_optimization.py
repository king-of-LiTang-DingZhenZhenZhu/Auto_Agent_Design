import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Settings
from models import ParamDef, SimResult
from review_optimization import (
    Candidate,
    apply_patch_plan,
    apply_review_rules,
    generate_candidate,
    inflate_width_params_from_instances,
    parse_parameter_values,
    select_top_records,
    simulate_candidate,
    write_candidate_metrics,
    write_local_agent_review_package,
    main as review_main,
)
from topologies import get_topology


class ReviewOptimizationTests(unittest.TestCase):
    def test_select_top_records_uses_top_decile_with_limits(self):
        short = [{"iteration": i, "reward": i} for i in range(2)]
        self.assertEqual([r["iteration"] for r in select_top_records(short)], [1, 0])

        records = [{"iteration": i, "reward": i} for i in range(25)]
        selected = select_top_records(records)
        self.assertEqual(len(selected), 3)
        self.assertEqual([r["iteration"] for r in selected], [24, 23, 22])

        many = [{"iteration": i, "reward": i} for i in range(120)]
        selected_many = select_top_records(many)
        self.assertEqual(len(selected_many), 10)
        self.assertEqual(selected_many[0]["iteration"], 119)
        self.assertEqual(selected_many[-1]["iteration"], 110)

    def test_apply_review_rules_changes_expected_params_and_clamps(self):
        params = {
            "Ldiff": 400e-9,
            "Wcs": 10e-6,
            "Wtail": 2e-6,
            "Wload": 2e-6,
            "Cc": 1e-12,
            "Rz": 1e3,
        }
        bounds = {
            "Ldiff": ParamDef("Ldiff", 120e-9, 450e-9, unit="m"),
            "Wcs": ParamDef("Wcs", 1e-6, 11e-6, unit="m"),
            "Wtail": ParamDef("Wtail", 1e-6, 20e-6, unit="m"),
            "Wload": ParamDef("Wload", 1e-6, 20e-6, unit="m"),
            "Cc": ParamDef("Cc", 0.1e-12, 2e-12, unit="F"),
            "Rz": ParamDef("Rz", 100.0, 10e3, unit="ohm"),
        }
        result = {
            "gain_db": 50,
            "bandwidth_hz": 50e6,
            "phase_margin_deg": 45,
            "power_w": 2e-3,
            "slew_rate_v_per_s": 50e6,
            "settling_time_s": 50e-9,
        }
        targets = {
            "gain_db": 60,
            "bandwidth_hz": 100e6,
            "phase_margin_deg": 60,
            "power_w": 1e-3,
            "slew_rate_v_per_s": 100e6,
            "settling_time_s": 20e-9,
        }

        adjusted, changes = apply_review_rules(params, result, targets, bounds)

        self.assertAlmostEqual(adjusted["Ldiff"], 450e-9)
        self.assertIn("Wcs", changes)
        self.assertLessEqual(adjusted["Wcs"], 11e-6)
        self.assertLess(adjusted["Wtail"], params["Wtail"])
        self.assertIn("Wload", changes)
        self.assertIn("Cc", changes)
        self.assertIn("Rz", changes)

    def test_apply_patch_plan_scales_sets_clamps_and_ignores_unknowns(self):
        params = {"Wcs": 10e-6, "Cc": 1e-12}
        bounds = {
            "Wcs": ParamDef("Wcs", 1e-6, 11e-6, unit="m"),
            "Cc": ParamDef("Cc", 0.1e-12, 2e-12, unit="F"),
        }
        plan_entry = {
            "iteration": 7,
            "reason": "Agent review",
            "actions": [
                {"param": "Wcs", "operation": "scale", "factor": 1.5},
                {"param": "Cc", "operation": "set", "value": 0.5e-12},
                {"param": "NotAParam", "operation": "scale", "factor": 10},
            ],
        }

        adjusted, changes = apply_patch_plan(params, plan_entry, bounds)

        self.assertAlmostEqual(adjusted["Wcs"], 11e-6)
        self.assertAlmostEqual(adjusted["Cc"], 0.5e-12)
        self.assertEqual(set(changes), {"Wcs", "Cc"})

    def test_parse_parameters_and_inflate_rendered_widths(self):
        netlist = """
parameters Wtail=1u Ltail=120n Wload=2.6u Cc=1p Rz=1k
Mtail ntail vbias vss vss nch_lvt_mac w=1u l=120n nf=2
Mload vout vbias vss vss nch_lvt_mac w=2.6u l=120n nf=4
"""
        params = parse_parameter_values(netlist)
        inflated = inflate_width_params_from_instances(netlist, params)

        self.assertAlmostEqual(params["Wload"], 2.6e-6)
        self.assertAlmostEqual(inflated["Wtail"], 2e-6)
        self.assertAlmostEqual(inflated["Wload"], 10.4e-6)
        self.assertAlmostEqual(params["Cc"], 1e-12)
        self.assertAlmostEqual(params["Rz"], 1e3)

    def test_generate_candidate_and_dry_run_outputs(self):
        template = """
simulator lang=spectre
parameters W1=5u L1=120n W3=10u L3=240n Wtail=3u Ltail=120n Cc=1p
subckt dut vip vin vout vdd vss
M1 n1 vip ntail vdd pch_lvt_mac w=W1 l=L1 nf=1
M2 vout vin ntail vdd pch_lvt_mac w=W1 l=L1 nf=1
M3 n1 n1 vss vss nch_lvt_mac w=W3 l=L3 nf=1
M4 vout n1 vss vss nch_lvt_mac w=W3 l=L3 nf=1
M5 ntail vbias vdd vdd pch_lvt_mac w=Wtail l=Ltail nf=2
Cc1 vout n1 C=Cc
ends dut
"""
        rendered = """
simulator lang=spectre
parameters W1=5u L1=120n W3=10u L3=240n Wtail=1.5u Ltail=120n Cc=1p
subckt dut vip vin vout vdd vss
M1 n1 vip ntail vdd pch_lvt_mac w=5u l=120n nf=1
M2 vout vin ntail vdd pch_lvt_mac w=5u l=120n nf=1
M3 n1 n1 vss vss nch_lvt_mac w=10u l=240n nf=4
M4 vout n1 vss vss nch_lvt_mac w=10u l=240n nf=4
M5 ntail vbias vdd vdd pch_lvt_mac w=1.5u l=120n nf=2
Cc1 vout n1 C=1p
ends dut
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            project = root / "outputs" / "proj"
            run_dir = workspace / "run_003"
            candidates_root = project / "agent_review" / "candidates"
            run_dir.mkdir(parents=True)
            candidates_root.mkdir(parents=True)
            (workspace / "circuit_template.cir").write_text(template, encoding="utf-8")
            (run_dir / "circuit.cir").write_text(rendered, encoding="utf-8")
            (run_dir / "tb.scs").write_text("include \"circuit.cir\"\n", encoding="utf-8")

            history = {
                "targets": {"gain_db": 60, "bandwidth_hz": 100e6},
            }
            record = {
                "iteration": 3,
                "reward": 1.25,
                "result": {"gain_db": 50, "bandwidth_hz": 80e6},
            }
            topology = get_topology("5t_ota")
            bounds = {p.name: p for p in topology.get_param_space().params}
            settings = Settings(dry_run=True, workspace_dir=str(workspace))
            patch_plan = {
                "summary": "Increase compensation only.",
                "candidates": [
                    {
                        "iteration": 3,
                        "reason": "Agent chose a PM-oriented candidate.",
                        "actions": [
                            {"param": "Cc", "operation": "scale", "factor": 1.25}
                        ],
                    }
                ],
            }

            candidate = generate_candidate(
                record=record,
                history=history,
                workspace=workspace,
                candidates_root=candidates_root,
                param_bounds=bounds,
                settings=settings,
                patch_plan=patch_plan,
            )
            simulate_candidate(candidate, settings)
            write_candidate_metrics(project / "candidate_metrics.csv", [candidate])

            candidate_netlist = (candidate.candidate_dir / "circuit.cir").read_text(
                encoding="utf-8"
            )
            self.assertIn("parameters", candidate_netlist)
            self.assertIn("Cc=1.25p", candidate_netlist)
            self.assertTrue((candidate.candidate_dir / "tb.scs").exists())
            self.assertTrue((candidate.candidate_dir / "metrics_summary.txt").exists())
            self.assertIsInstance(candidate.result, SimResult)
            self.assertEqual(candidate.review_reason, "Agent chose a PM-oriented candidate.")

            with (project / "candidate_metrics.csv").open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["original_iteration"], "3")
            self.assertEqual(rows[0]["original_reward"], "1.25")
            self.assertEqual(rows[0]["changed_params"], "Cc")

    def test_write_local_agent_review_package_outputs_context_and_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            project = root / "outputs" / "proj"
            review_root = project / "agent_review"
            run_dir = workspace / "run_001"
            run_dir.mkdir(parents=True)
            project.mkdir(parents=True)
            (project / "optimization_metrics.csv").write_text(
                "iteration,reward,gain_db(dB)\n1,0.5,40.0\n",
                encoding="utf-8",
            )
            (run_dir / "circuit.cir").write_text(
                "parameters W1=5u L1=120n Cc=1p\n"
                "M1 out in vss vss nch_lvt_mac w=5u l=120n nf=1\n",
                encoding="utf-8",
            )
            history = {
                "total_iterations": 2,
                "best_iteration": 1,
                "best_reward": 0.5,
                "targets": {"gain_db": 60},
            }
            records = [
                {"iteration": 1, "reward": 0.5, "result": {"gain_db": 40}},
            ]
            bounds = {"W1": ParamDef("W1", 1e-6, 20e-6)}

            write_local_agent_review_package(
                project=project,
                workspace=workspace,
                topology_name="5t_ota",
                history=history,
                history_path=workspace / "history.json",
                records=records,
                param_bounds=bounds,
                review_root=review_root,
            )

            context = (review_root / "agent_context.md").read_text(encoding="utf-8")
            plan = (review_root / "patch_plan.json").read_text(encoding="utf-8")
            self.assertIn("Local Agent BO Review Context", context)
            self.assertIn("Optimization Metrics CSV", context)
            self.assertIn("W1", context)
            self.assertIn('"iteration": 1', plan)

    def test_patch_plan_run_preserves_agent_context_and_plan(self):
        template = """
simulator lang=spectre
parameters W1=5u L1=120n Cc=1p
subckt dut vip vin vout vdd vss
M1 vout vip vss vss nch_lvt_mac w=W1 l=L1 nf=1
Cc1 vout vss capacitor c=Cc
ends dut
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "outputs" / "proj"
            workspace = root / "workspace"
            review_root = project / "agent_review"
            run_dir = workspace / "run_000"
            run_dir.mkdir(parents=True)
            review_root.mkdir(parents=True)
            (workspace / "circuit_template.cir").write_text(template, encoding="utf-8")
            (run_dir / "circuit.cir").write_text(template, encoding="utf-8")
            (run_dir / "tb.scs").write_text("include \"circuit.cir\"\n", encoding="utf-8")
            (project / "optimization_log.json").write_text(
                json.dumps(
                    {
                        "targets": {"gain_db": 60},
                        "history": [
                            {
                                "iteration": 0,
                                "reward": 1.0,
                                "result": {"gain_db": 40},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            context_path = review_root / "agent_context.md"
            plan_path = review_root / "patch_plan.json"
            context_path.write_text("keep this context", encoding="utf-8")
            plan_path.write_text(
                json.dumps(
                    {
                        "summary": "increase cap",
                        "candidates": [
                            {
                                "iteration": 0,
                                "reason": "test",
                                "actions": [
                                    {
                                        "param": "Cc",
                                        "operation": "scale",
                                        "factor": 1.2,
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            argv = [
                "review_optimization.py",
                "--project",
                str(project),
                "--workspace",
                str(workspace),
                "--topology",
                "5t_ota",
                "--patch-plan",
                str(plan_path),
                "--dry-run",
            ]
            with patch.object(sys, "argv", argv):
                review_main()

            self.assertEqual(context_path.read_text(encoding="utf-8"), "keep this context")
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["summary"], "increase cap")
            self.assertTrue((review_root / "candidate_metrics.csv").exists())
            self.assertTrue(
                (review_root / "candidates" / "iter_000_candidate_01" / "circuit.cir").exists()
            )


if __name__ == "__main__":
    unittest.main()
