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
                self.assertTrue(Path(result.physical_root).is_absolute())
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
                    self.assertTrue(manifest["physical_planning"]["constraint_realization_complete"])
                    self.assertEqual(manifest["physical_planning"]["signoff_blockers"], [])
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
                    self.assertEqual(assignments["n_tail"]["implementation"], "local_m3_template")
                    self.assertFalse(assignments["n_tail"]["solver_assignment_consumed"])
                    self.assertEqual(assignments["n_mirr"]["implementation"], "local_m3_template")
                    self.assertFalse(assignments["n_mirr"]["solver_assignment_consumed"])
                    self.assertEqual(
                        smt_solution["matching_realization"]["input_pair"]["status"],
                        "realized",
                    )
                    self.assertEqual(
                        smt_solution["matching_realization"]["input_pair"]["calibre_qualification"],
                        "pending",
                    )
                    self.assertEqual(
                        smt_solution["matching_realization"]["input_pair"]["unit_pattern"],
                        ["DUMMY_L", "A", "B", "B", "A", "DUMMY_R"],
                    )
                    self.assertEqual(len(mapping["Mtail"]["lvs_instances"]), 2)
                    self.assertEqual(len(mapping["Mload"]["lvs_instances"]), 4)
                    self.assertEqual(mapping["Mtail"]["physical_realization"]["strategy"], "explicit_m_unit_array")
                    self.assertEqual(mapping["Mtail"]["physical_realization"]["requested_m"], 2)
                    self.assertEqual(mapping["Mtail"]["physical_realization"]["requested_nf"], 1)
                    self.assertEqual(mapping["Mtail"]["physical_realization"]["oa_instance_count"], 2)
                    layout_plan = json.loads(Path(result.layout_plan_path).read_text(encoding="utf-8"))
                    landing_pads = [
                        rect
                        for rect in layout_plan["rects"]
                        if rect.get("metadata", {}).get("kind") == "required_via_landing_pad"
                    ]
                    self.assertTrue(landing_pads)
                    n_tail_m3 = [
                        path
                        for path in layout_plan["paths"]
                        if path["net"] == "n_tail" and path["layer"] == "M3"
                    ]
                    self.assertTrue(n_tail_m3)
                    self.assertTrue(
                        any(
                            path["net"] == "n_mirr" and path["layer"] == "M3"
                            for path in layout_plan["paths"]
                        )
                    )
                    mdiff_units = sorted(
                        [instance for instance in layout_plan["instances"] if instance["name"].startswith("Mdiff")],
                        key=lambda instance: instance["xy"][0],
                    )
                    self.assertEqual(
                        [instance["name"].split("_", 1)[0] for instance in mdiff_units],
                        ["Mdiff1", "Mdiff2", "Mdiff2", "Mdiff1"],
                    )
                    self.assertEqual([instance["orient"] for instance in mdiff_units], ["R0", "R0", "MY", "MY"])
                    self.assertEqual(mdiff_units[0]["params"]["leftDummyPoly"], "ON")
                    self.assertEqual(mdiff_units[0]["params"]["rightDummyPoly"], "OFF")
                    self.assertEqual(mdiff_units[-1]["params"]["leftDummyPoly"], "OFF")
                    self.assertEqual(mdiff_units[-1]["params"]["rightDummyPoly"], "ON")
                    self.assertTrue(
                        all(
                            instance["params"]["leftDummyPoly"] == "OFF"
                            and instance["params"]["rightDummyPoly"] == "OFF"
                            for instance in mdiff_units[1:3]
                        )
                    )
                    self.assertEqual(
                        mapping["Mdiff1"]["physical_realization"]["strategy"],
                        "common_centroid_abba_segmented",
                    )
                    self.assertEqual(mapping["Mdiff1"]["physical_realization"]["requested_nf"], 1)
                    self.assertEqual(mapping["Mdiff1"]["physical_realization"]["oa_instance_count"], 2)
                    mtail_units = [instance for instance in layout_plan["instances"] if instance["name"].startswith("Mtail_u")]
                    mload_units = [instance for instance in layout_plan["instances"] if instance["name"].startswith("Mload_u")]
                    self.assertEqual(len(mtail_units), 2)
                    self.assertEqual(len(mload_units), 4)
                    self.assertTrue(all(instance["params"]["fingers"] == 1 for instance in mtail_units))
                    self.assertTrue(all(instance["params"]["simM"] == 1 for instance in mtail_units))
                    self.assertTrue(all(instance["params"]["fingers"] == 1 for instance in mload_units))
                    self.assertTrue(all(instance["params"]["simM"] == 1 for instance in mload_units))
                    mtail = mtail_units[0]
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
                    local_drc = stages["final_with_pins"]["local_drc"]
                    self.assertTrue(local_drc["passed"])
                    self.assertEqual(local_drc["rule_issues"], [])
                    self.assertEqual(local_drc["via_landing_issues"], [])
                    self.assertEqual(
                        set(local_drc["checked_rules"]),
                        {"min_width", "min_area", "different_net_spacing", "via_landing_enclosure"},
                    )
                    self.assertIn("density_and_fill", local_drc["unverified_rule_classes"])


if __name__ == "__main__":
    unittest.main()
