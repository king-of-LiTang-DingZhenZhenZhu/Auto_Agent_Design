from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CIRCUIT_AGENT = ROOT / "Agent_LLM_BO" / "circuit_agent"
sys.path[:0] = [str(ROOT), str(CIRCUIT_AGENT)]

from design_flow_graph import run_design_flow
from topologies import get_topology


class PhysicalGateTest(unittest.TestCase):
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
