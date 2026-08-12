from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CIRCUIT_AGENT = ROOT / "Agent_LLM_BO" / "circuit_agent"
sys.path[:0] = [str(ROOT), str(CIRCUIT_AGENT)]

from analogskills.imported_design.flow import (
    ImportedPhysicalResult,
    compile_imported_design,
    _physical_pcell_sizing,
    run_imported_design_signoff,
)
from analogskills.imported_design.handoff import build_imported_design_handoff
from analogskills.imported_design.physical_intent import (
    PHYSICAL_INTENT_SCHEMA,
    PhysicalIntentError,
    compile_physical_intent,
    solve_imported_physical_smt,
)
from analogskills.pdk import resolve_pdk_config
from topologies import get_topology
from virtuoso_export.parser import parse_netlist


class PhysicalIntentTest(unittest.TestCase):
    def _handoff(self, root: Path):
        project = root / "two_stage_ota"
        project.mkdir()
        netlist = project / "circuit.cir"
        netlist.write_text(get_topology("two_stage_ota").generate_circuit(), encoding="utf-8")
        pvt = project / "pvt" / "pvt_results.json"
        pvt.parent.mkdir()
        pvt.write_text(json.dumps({"pvt_pass": True, "summary": {"total_corners": 27}}), encoding="utf-8")
        return build_imported_design_handoff(
            project_dir=project,
            topology="two_stage_ota",
            final_netlist=netlist,
            final_source="bo_best",
            pvt_results=pvt,
            schematic_ir=parse_netlist(netlist),
        )

    def test_graph_and_policy_compile_to_auditable_dsl(self):
        with tempfile.TemporaryDirectory() as tmp:
            handoff = self._handoff(Path(tmp))
            graph, _ = compile_imported_design(handoff)
            intent = compile_physical_intent(
                graph,
                topology=handoff.topology,
                pdk=resolve_pdk_config("crn28hpcp"),
            )
            payload = intent.to_dict()
            self.assertEqual(payload["schema"], PHYSICAL_INTENT_SCHEMA)
            self.assertEqual({row["name"] for row in payload["spec"]["patterns"]}, {
                "bias_reference", "tail_device", "mirror_pair", "input_pair",
                "second_stage", "compensation",
            })
            self.assertTrue(any(row["kind"] == "match:common_centroid" for row in payload["constraints"]))
            self.assertEqual(payload["metadata"]["constraint_precedence"][0], "pdk_hard")
            self.assertEqual(payload["metadata"]["route_resource_solver"], "analogskills_local_smt")
            hard_relations = {
                (row["source"], row["target"], row["kind"])
                for row in payload["spec"]["relations"]
                if row["hard"]
            }
            self.assertIn(("tail_device", "input_pair", "overlap_x"), hard_relations)
            self.assertIn(("compensation", "second_stage", "overlap_y"), hard_relations)

    def test_smt_solves_placement_and_nonconflicting_route_lanes(self):
        with tempfile.TemporaryDirectory() as tmp:
            handoff = self._handoff(Path(tmp))
            graph, sizing = compile_imported_design(handoff)
            result = solve_imported_physical_smt(
                graph,
                _physical_pcell_sizing(handoff, sizing),
                topology=handoff.topology,
                pdk=resolve_pdk_config("crn28hpcp"),
            )
            self.assertTrue(result.passed)
            self.assertEqual({item.name for item in result.placements}, set(graph.devices))
            placement_by_name = {item.name: item for item in result.placements}
            self.assertEqual(placement_by_name["Mdiff1"].orient, "R0")
            self.assertEqual(placement_by_name["Mdiff2"].orient, "MY")
            self.assertEqual(placement_by_name["Mmirr1"].orient, "R0")
            self.assertEqual(placement_by_name["Mmirr2"].orient, "MY")
            lanes = [int(row["lane"]) for row in result.route_resource_assignments.values()]
            self.assertEqual(len(lanes), len(set(lanes)))
            self.assertEqual(
                abs(
                    int(result.route_resource_assignments["vip"]["lane"])
                    - int(result.route_resource_assignments["vin"]["lane"])
                ),
                1,
            )
            self.assertEqual(result.matching_realization["input_pair"]["status"], "degraded_explicit")
            self.assertEqual(result.routing_evidence["planner"], "analogskills.layout.analog_routing")
            self.assertTrue(result.routing_evidence["local_smt_patches"])
            self.assertTrue(all(
                row["solver"] in {"analogskills_local_smt", "template"}
                for row in result.route_resource_assignments.values()
            ))
            self.assertFalse(result.compiled.checks["constraint_realization_complete"])

    def test_physical_sizing_preserves_m_and_nf_as_explicit_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            handoff = self._handoff(Path(tmp))
            _graph, sizing = compile_imported_design(handoff)
            physical = _physical_pcell_sizing(handoff, sizing)
            self.assertEqual(physical["Mtail"]["m"], 2)
            self.assertEqual(physical["Mtail"]["nf"], 1)
            self.assertEqual(physical["Mtail"]["W"], sizing["Mtail"]["W"])
            self.assertEqual(physical["Mtail"]["mos_unit_array"]["unit_count"], 2)
            self.assertEqual(physical["Mtail"]["mos_unit_array"]["unit_nf"], 1)
            self.assertEqual(physical["Mtail"]["mos_unit_array"]["unit_m"], 1)

    def test_unknown_topology_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            handoff = self._handoff(Path(tmp))
            graph, _ = compile_imported_design(handoff)
            with self.assertRaises(PhysicalIntentError) as caught:
                compile_physical_intent(graph, topology="unknown", pdk=resolve_pdk_config("crn28hpcp"))
            self.assertEqual(caught.exception.reason, "physical_adapter_required")

    def test_legacy_debug_manifest_is_not_signoff_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run_manifest.json").write_text(
                json.dumps({
                    "cellview": {"lib": "BO_Designs", "cell": "ota"},
                    "physical_planning": {
                        "placement_mode": "legacy_seed_debug",
                        "signoff_eligible": False,
                    },
                }),
                encoding="utf-8",
            )
            base = ImportedPhysicalResult(
                "prepared", str(root), "", "", "", "", "", str(root / "ota.gds")
            )
            result = run_imported_design_signoff(base)
            self.assertEqual(result.status, "physical_blocked")
            self.assertEqual(result.errors, ("legacy_seed_debug is not eligible for sign-off",))


if __name__ == "__main__":
    unittest.main()
