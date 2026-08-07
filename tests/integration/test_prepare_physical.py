from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CIRCUIT_AGENT = ROOT / "Agent_LLM_BO" / "circuit_agent"
sys.path[:0] = [str(ROOT), str(CIRCUIT_AGENT)]

from analogskills.imported_design import build_imported_design_handoff, prepare_imported_physical_run
from topologies import get_topology
from virtuoso_export.parser import parse_netlist


class PreparePhysicalIntegrationTest(unittest.TestCase):
    def test_both_supported_topologies_prepare_without_upstream_repo(self):
        for topology in ("two_stage_ota", "strongarm_latch"):
            with self.subTest(topology=topology), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp) / topology
                project.mkdir()
                netlist = project / "circuit.cir"
                netlist.write_text(get_topology(topology).generate_circuit(), encoding="utf-8")
                pvt = project / "pvt" / "pvt_results.json"
                pvt.parent.mkdir()
                pvt.write_text(json.dumps({"pvt_pass": True, "summary": {"total_corners": 27}}), encoding="utf-8")
                handoff = build_imported_design_handoff(
                    project_dir=project, topology=topology, final_netlist=netlist,
                    final_source="bo_best", pvt_results=pvt, schematic_ir=parse_netlist(netlist),
                )
                result = prepare_imported_physical_run(handoff)
                self.assertEqual(result.status, "prepared")
                self.assertTrue(Path(result.layout_plan_path).is_file())
                self.assertTrue(Path(result.layout_skill_path).is_file())
                self.assertTrue(Path(result.lvs_source_path).is_file())
                text = Path(result.layout_skill_path).read_text(encoding="utf-8")
                self.assertNotIn("drawn_primitive", text)
                self.assertIn('ddCreateLib("BO_Designs")', text)
                self.assertIn('techBindTechFile(libObj "tsmcN28")', text)
                self.assertLess(text.index("techBindTechFile"), text.index("dbOpenCellViewByType"))
                self.assertNotIn("exit()", text)
                oa_batch = Path(result.physical_root) / "oa" / "write_all.il"
                oa_batch_text = oa_batch.read_text(encoding="utf-8")
                self.assertEqual(oa_batch_text.count("load("), 2)
                self.assertIn("hiFormCancel(techSaveDrmForm)", oa_batch_text)
                self.assertTrue(oa_batch_text.rstrip().endswith("exit()"))
                schematic_text = Path(result.schematic_skill_path).read_text(encoding="utf-8")
                self.assertNotIn("exit()", schematic_text)
                self.assertIn("schCreateWire", schematic_text)
                self.assertIn("schCreateWireLabel", schematic_text)
                self.assertIn("schCreatePin", schematic_text)
                self.assertIn("dbTransformPoint", schematic_text)
                self.assertIn("errset(schCheck(cv) t)", schematic_text)
                self.assertNotIn("boundp('schCreateWire)", schematic_text)
                streamout = Path(result.physical_root) / "oa" / "streamout.il"
                self.assertNotIn("exit()", streamout.read_text(encoding="utf-8"))
                mapping = json.loads((Path(result.physical_root) / "instance_mapping.json").read_text(encoding="utf-8"))
                self.assertEqual(set(mapping), {row.name for row in handoff.devices})
                if topology == "two_stage_ota":
                    manifest = json.loads(
                        (Path(result.physical_root) / "run_manifest.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(manifest["physical_planning"]["placement_mode"], "smt")
                    self.assertTrue(manifest["physical_planning"]["signoff_eligible"])
                    self.assertEqual(manifest["physical_planning"]["solver"], "z3")
                    for artifact in ("design_intent", "smt_solution", "routing_resources"):
                        self.assertTrue(Path(manifest["artifacts"][artifact]).is_file())
                    smt_solution = json.loads(
                        (Path(result.physical_root) / "layout" / "smt_solution.json").read_text(encoding="utf-8")
                    )
                    self.assertTrue(smt_solution["passed"])
                    assignments = smt_solution["route_resource_assignments"]
                    self.assertEqual(assignments["vip"]["layer"], assignments["vin"]["layer"])
                    self.assertEqual(abs(assignments["vip"]["lane"] - assignments["vin"]["lane"]), 1)
                    self.assertEqual(len({row["lane"] for row in assignments.values()}), len(assignments))
                    self.assertEqual(
                        smt_solution["matching_realization"]["input_pair"]["status"],
                        "degraded_explicit",
                    )
                    self.assertEqual(len(mapping["Mtail"]["lvs_instances"]), 2)
                    self.assertEqual(len(mapping["Mload"]["lvs_instances"]), 4)
                    layout_plan = json.loads(Path(result.layout_plan_path).read_text(encoding="utf-8"))
                    mtail = next(instance for instance in layout_plan["instances"] if instance["name"] == "Mtail")
                    mload = next(instance for instance in layout_plan["instances"] if instance["name"] == "Mload")
                    self.assertEqual(mtail["params"]["fingers"], 2)
                    self.assertEqual(mtail["params"]["simM"], 1)
                    self.assertEqual(mload["params"]["fingers"], 4)
                    self.assertEqual(mload["params"]["simM"], 1)
                    self.assertEqual(
                        mtail["metadata"]["terminal_access"]["S"]["source"],
                        "crn28_calibre_access_plan",
                    )
                    self.assertTrue(mtail["metadata"]["routing_owned_shapes"])
                    rz = next(instance for instance in layout_plan["instances"] if instance["name"] == "Rz")
                    self.assertEqual(rz["metadata"]["logical_name"], "resistor")
                    self.assertGreater(rz["metadata"]["width_um"], 0.0)
                    self.assertGreater(rz["metadata"]["height_um"], 0.0)
                    stages = json.loads(
                        (Path(result.physical_root) / "layout" / "physical_precheck_stages.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(
                        set(stages),
                        {"routed_core", "supply_taps", "wells", "guard_ring", "final_with_pins"},
                    )
                    self.assertTrue(all(report["passed"] for report in stages.values()))
                    self.assertTrue(all(not report["shorts"] for report in stages.values()))
                    self.assertTrue(all(not report["opens"] for report in stages.values()))
                    self.assertEqual(
                        stages["final_with_pins"]["constraint_realization"]["route_resource_capacity_overflow"],
                        0,
                    )


if __name__ == "__main__":
    unittest.main()
