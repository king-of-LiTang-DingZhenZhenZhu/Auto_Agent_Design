from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
CIRCUIT_AGENT = ROOT / "Agent_LLM_BO" / "circuit_agent"
sys.path[:0] = [str(ROOT), str(CIRCUIT_AGENT)]

from design_flow_graph import run_design_flow
from physical_bridge import execute_physical_from_state
from topologies import get_topology


class PhysicalGateTest(unittest.TestCase):
    def test_signoff_error_is_promoted_to_top_level_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "two_stage"
            project.mkdir(parents=True)
            netlist = project / "circuit.cir"
            netlist.write_text(get_topology("two_stage_ota").generate_circuit(), encoding="utf-8")
            result = SimpleNamespace(
                status="physical_blocked",
                physical_root=str(project / "physical"),
                handoff_path="",
                layout_plan_path="",
                gds_path="",
                drc_report_path="",
                lvs_report_path="",
                drc_violations=None,
                lvs_issues=None,
                eco_iterations=0,
                errors=("schematic_oa failed",),
                passed=False,
            )
            state = {
                "project_dir": str(project),
                "topology": "two_stage_ota",
                "final_netlist": str(netlist),
                "final_source": "bo_best",
                "nominal_pass": True,
                "review_pass": False,
                "audit_status": "pass",
                "pvt_pass": True,
                "errors": [],
            }

            with patch("physical_bridge.build_imported_design_handoff"), \
                 patch("physical_bridge.prepare_imported_physical_run", return_value=result), \
                 patch("physical_bridge.run_imported_design_signoff", return_value=result):
                updated = execute_physical_from_state(
                    state,
                    prepare_physical=True,
                    run_signoff=True,
                    max_eco_iterations=5,
                )

            self.assertEqual(updated["physical_blocker"], "schematic_oa failed")
            self.assertEqual(updated["next_action"], "fix_physical_blocker")

    def test_prepare_physical_preserves_review_action_when_nominal_is_unmet(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "two_stage"
            project.mkdir(parents=True)
            netlist = project / "circuit.cir"
            netlist.write_text(get_topology("two_stage_ota").generate_circuit(), encoding="utf-8")
            (project / "results.json").write_text(json.dumps({
                "project_name": "two_stage",
                "topology_name": "two_stage_ota",
                "all_targets_met": False,
                "netlist_file": str(netlist),
            }), encoding="utf-8")

            state = run_design_flow(project, prepare_physical=True)

            self.assertIn("prepare_agent_review", state["next_action"])
            self.assertTrue(state["physical_requested"])
            self.assertFalse((project / "physical").exists())

    def test_prepare_physical_stops_without_pvt_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "two_stage"
            project.mkdir(parents=True)
            netlist = project / "circuit.cir"
            netlist.write_text(get_topology("two_stage_ota").generate_circuit(), encoding="utf-8")
            (project / "results.json").write_text(json.dumps({
                "project_name": "two_stage",
                "topology_name": "two_stage_ota",
                "all_targets_met": True,
                "netlist_file": str(netlist),
            }), encoding="utf-8")

            state = run_design_flow(project, prepare_physical=True)

            self.assertEqual(state["next_action"], "run_pvt")
            self.assertTrue(state["physical_requested"])
            self.assertFalse((project / "physical").exists())


if __name__ == "__main__":
    unittest.main()
