from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from design_flow_graph import run_design_flow
from topologies import get_topology


class DesignFlowGraphTests(unittest.TestCase):
    def test_nominal_pass_runs_pvt_dry_run_and_writes_flow_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            netlist = project / "netlist" / "circuit.cir"
            netlist.parent.mkdir()
            netlist.write_text(get_topology("5t_ota").generate_circuit(), encoding="utf-8")
            sim = project / "simulation"
            sim.mkdir()
            (sim / "tb_circuit.scs").write_text(
                get_topology("5t_ota").generate_testbench(analysis_type="ac"),
                encoding="utf-8",
            )
            (project / "optimization_log.json").write_text(
                json.dumps({"targets": {"gain_db": 40, "bandwidth_hz": 1e6}}),
                encoding="utf-8",
            )
            (project / "results.json").write_text(
                json.dumps({
                    "project_name": "proj",
                    "all_targets_met": True,
                    "netlist_file": str(netlist),
                }),
                encoding="utf-8",
            )

            state = run_design_flow(project, run_pvt=True, simulate=False)

            self.assertEqual(state["next_action"], "inspect_pvt_report")
            self.assertEqual(state["final_source"], "bo_best")
            self.assertTrue((project / "pvt" / "pvt_results.csv").exists())
            self.assertTrue((project / "flow" / "flow_state.json").exists())
            self.assertTrue((project / "flow" / "flow_report.md").exists())

    def test_unmet_nominal_stops_at_agent_review_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            netlist = project / "netlist" / "circuit.cir"
            netlist.parent.mkdir()
            netlist.write_text(get_topology("5t_ota").generate_circuit(), encoding="utf-8")
            (project / "results.json").write_text(
                json.dumps({
                    "project_name": "proj",
                    "all_targets_met": False,
                    "netlist_file": str(netlist),
                }),
                encoding="utf-8",
            )

            state = run_design_flow(project)

            self.assertIn("prepare_agent_review", state["next_action"])
            self.assertIs(state["nominal_pass"], False)
            self.assertTrue((project / "flow" / "flow_report.md").exists())


if __name__ == "__main__":
    unittest.main()
