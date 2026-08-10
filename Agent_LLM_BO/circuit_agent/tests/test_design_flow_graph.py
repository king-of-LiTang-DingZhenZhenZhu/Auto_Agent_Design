from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from design_flow_graph import run_design_flow
from models import DesignTarget
from pdk_integration.profiles import get_pdk_profile
from topologies import get_topology


class DesignFlowGraphTests(unittest.TestCase):
    def test_explicit_pvt_contract_is_forwarded_and_serialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            netlist = project / "netlist" / "circuit.cir"
            netlist.parent.mkdir()
            netlist.write_text(
                get_topology("5t_ota").generate_circuit(),
                encoding="utf-8",
            )
            (project / "results.json").write_text(
                json.dumps(
                    {
                        "project_name": "proj",
                        "all_targets_met": True,
                        "netlist_file": str(netlist),
                    }
                ),
                encoding="utf-8",
            )
            targets = DesignTarget(gain_db=55)
            profile = get_pdk_profile()

            with patch(
                "design_flow_graph.run_pvt_verification",
                return_value={"pvt_pass": True},
            ) as run_pvt:
                state = run_design_flow(
                    project,
                    run_pvt=True,
                    pvt_targets=targets,
                    pvt_profile=profile,
                )

            self.assertTrue(state["pvt_pass"])
            self.assertIs(run_pvt.call_args.kwargs["targets"], targets)
            self.assertIs(run_pvt.call_args.kwargs["profile"], profile)
            persisted = json.loads(
                (project / "flow" / "flow_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["pvt_targets"]["gain_db"], 55)
            self.assertEqual(persisted["pvt_profile"]["name"], profile.name)

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
            self.assertIn(state["audit_status"], {"pass", "warn"})
            self.assertTrue((project / "design_audit" / "design_audit.md").exists())
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
            self.assertEqual(state["review_mode"], "failure_repair")
            self.assertIs(state["nominal_pass"], False)
            self.assertTrue((project / "flow" / "flow_report.md").exists())

    def test_nominal_pass_with_audit_blocker_stops_before_pvt(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            project.mkdir(parents=True)
            netlist = project / "netlist" / "circuit.cir"
            netlist.parent.mkdir()
            netlist.write_text(get_topology("5t_ota").generate_circuit(), encoding="utf-8")
            (project / "results.json").write_text(
                json.dumps(
                    {
                        "project_name": "proj",
                        "all_targets_met": True,
                        "netlist_file": str(netlist),
                        "operating_point_status": {
                            "critical_linear": ["Mdp1"],
                            "critical_near_edge": [],
                        },
                    }
                ),
                encoding="utf-8",
            )

            state = run_design_flow(project, run_pvt=True, simulate=False)

            self.assertEqual(state["audit_status"], "block")
            self.assertIn("prepare_agent_review", state["next_action"])
            self.assertEqual(state["review_mode"], "audit_repair")
            self.assertFalse((project / "pvt").exists())

    def test_on_chip_passives_with_incomplete_pdk_mapping_stop_before_audit_and_pvt(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "outputs" / "proj"
            netlist = project / "netlist" / "circuit.cir"
            netlist.parent.mkdir(parents=True)
            netlist.write_text(
                get_topology("two_stage_ota").generate_circuit(),
                encoding="utf-8",
            )
            (project / "results.json").write_text(
                json.dumps({
                    "project_name": "proj",
                    "topology_name": "two_stage_ota",
                    "all_targets_met": True,
                    "netlist_file": str(netlist),
                }),
                encoding="utf-8",
            )

            incomplete_profile = replace(
                get_pdk_profile("tsmc28"),
                passive_role_map={
                    role: device
                    for role, device in get_pdk_profile("tsmc28").passive_role_map.items()
                    if role != "compensation_capacitor"
                },
            )
            with patch(
                "pdk_integration.profiles.get_pdk_profile", return_value=incomplete_profile
            ), patch("design_flow_graph.run_pvt_verification") as run_pvt:
                state = run_design_flow(project, run_pvt=True, simulate=False)

            run_pvt.assert_not_called()
            self.assertEqual(state["passive_status"], "blocked")
            self.assertEqual(state["next_action"], "configure_pdk_passives")
            self.assertFalse((project / "design_audit").exists())
            report = json.loads(
                (project / "passive_realization" / "passive_realization.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("compensation_capacitor", report["error"])
            self.assertIn("Cc", report["error"])


if __name__ == "__main__":
    unittest.main()
